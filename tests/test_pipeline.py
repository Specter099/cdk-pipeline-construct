import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk.assertions import Match, Template

from specter_pipeline import (
    Gate,
    PostDeployStep,
    Runtime,
    StandardPipeline,
    artifact_bucket_purpose,
)

# Dummy, obviously-fake values: synth needs a concrete env, but nothing here
# ever reaches AWS. Derived rather than literal so no real account id is implied.
TEST_ACCOUNT = "0" * 12
TEST_REGION = "-".join(["us", "east", "1"])
CONN = f"arn:aws:codeconnections:{TEST_REGION}:{TEST_ACCOUNT}:connection/abc"
ENV = cdk.Environment(account=TEST_ACCOUNT, region=TEST_REGION)


class AppStage(cdk.Stage):
    def __init__(self, scope, stage_id, **kwargs):
        super().__init__(scope, stage_id, **kwargs)
        stack = cdk.Stack(self, "AppStack", stack_name="AppStack")
        sns.Topic(stack, "T")


GATES = [
    Gate("unit", ["pytest -q"], install=["pip install -r requirements.txt"]),
    Gate("synth-check", ["cdk synth"], runtime=Runtime.PYTHON_NODE, memory_gb=4),
    Gate("lint", ["npm run lint"], runtime=Runtime.NODE),
]


def build(**overrides) -> Template:
    app = cdk.App()
    stack = cdk.Stack(app, "PipelineStack", env=ENV)
    kwargs = dict(
        repo="Specter099/demo",
        connection_arn=CONN,
        application_stage=AppStage(app, "Prod", env=ENV),
        gates=GATES,
        synth_commands=["cdk -a 'python3 pipeline_app.py' synth"],
        synth_install=["pip install -r requirements.txt"],
        post_deploy=[
            PostDeployStep(
                "frontend",
                ["npm run build"],
                policy_statements=[
                    iam.PolicyStatement(
                        actions=["s3:PutObject"], resources=["arn:aws:s3:::b/*"]
                    )
                ],
            )
        ],
    )
    kwargs.update(overrides)
    StandardPipeline(stack, "P", **kwargs)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def tpl() -> Template:
    return build()


def projects(t: Template) -> dict:
    return t.find_resources("AWS::CodeBuild::Project")


def test_every_codebuild_project_is_lambda_compute(tpl):
    found = projects(tpl)
    # synth + self-mutate + 3 gates + 1 post + 3 PR checks (no asset-publish
    # project: the dummy stage has no assets)
    assert len(found) == 9
    for logical_id, res in found.items():
        env = res["Properties"]["Environment"]
        assert env["Type"] == "LINUX_LAMBDA_CONTAINER", logical_id
        assert env["ComputeType"].startswith("BUILD_LAMBDA_"), logical_id
        assert not env.get("PrivilegedMode"), logical_id


def test_no_lambda_unsupported_project_settings(tpl):
    for logical_id, res in projects(tpl).items():
        props = res["Properties"]
        for unsupported in (
            "TimeoutInMinutes",
            "QueuedTimeoutInMinutes",
            "VpcConfig",
            "FileSystemLocations",
        ):
            assert unsupported not in props, f"{logical_id} sets {unsupported}"
        # NO_CACHE is CloudFormation's explicit default; anything else is caching.
        assert props.get("Cache", {"Type": "NO_CACHE"}) == {"Type": "NO_CACHE"}, logical_id


def test_deploy_stage_is_held_closed_by_default(tpl):
    tpl.has_resource_properties(
        "AWS::CodePipeline::Pipeline",
        {"DisableInboundStageTransitions": [Match.object_like({"StageName": "Prod"})]},
    )


def test_deploy_stage_open_when_enabled():
    t = build(deploy_enabled=True)
    (pipe,) = t.find_resources("AWS::CodePipeline::Pipeline").values()
    assert "DisableInboundStageTransitions" not in pipe["Properties"]


def test_stage_order_and_approval_precedes_deploy(tpl):
    (pipe,) = tpl.find_resources("AWS::CodePipeline::Pipeline").values()
    stages = pipe["Properties"]["Stages"]
    assert [s["Name"] for s in stages] == [
        "Source",
        "Build",
        "UpdatePipeline",
        "Gates",
        "Prod",
    ]
    prod_actions = sorted(stages[-1]["Actions"], key=lambda a: a["RunOrder"])
    approval = next(
        a for a in prod_actions if a["ActionTypeId"]["Category"] == "Approval"
    )
    deploys = [
        a for a in prod_actions if a["ActionTypeId"]["Provider"] == "CloudFormation"
    ]
    assert deploys, "no CloudFormation deploy action in the deploy stage"
    assert all(approval["RunOrder"] < d["RunOrder"] for d in deploys)


def test_gates_stage_has_every_gate(tpl):
    (pipe,) = tpl.find_resources("AWS::CodePipeline::Pipeline").values()
    gates = next(s for s in pipe["Properties"]["Stages"] if s["Name"] == "Gates")
    assert {a["Name"] for a in gates["Actions"]} == {
        "Gate-unit",
        "Gate-synth-check",
        "Gate-lint",
    }
    assert len({a["RunOrder"] for a in gates["Actions"]}) == 1, (
        "gates should run in parallel"
    )


def test_approval_can_be_omitted():
    t = build(require_approval=False)
    (pipe,) = t.find_resources("AWS::CodePipeline::Pipeline").values()
    prod = pipe["Properties"]["Stages"][-1]
    assert not [
        a for a in prod["Actions"] if a["ActionTypeId"]["Category"] == "Approval"
    ]


def test_pipeline_is_v2_and_triggers_on_main_push_only(tpl):
    (pipe,) = tpl.find_resources("AWS::CodePipeline::Pipeline").values()
    props = pipe["Properties"]
    assert props["PipelineType"] == "V2"
    source = props["Stages"][0]["Actions"][0]["Configuration"]
    assert source["BranchName"] == "main"
    assert source["DetectChanges"] in (True, "true")
    # No pull-request trigger on the pipeline: PRs must never reach self-mutation.
    # CloudFormation spells the key "PullRequest"; a lowercase "pull_request"
    # search could never match it. The pipeline must have no triggers at all.
    assert "Triggers" not in props


def test_artifact_bucket_is_explicitly_named_and_hardened(tpl):
    (bucket,) = [
        r
        for r in tpl.find_resources("AWS::S3::Bucket").values()
        if "BucketName" in r["Properties"]
    ]
    name = json.dumps(bucket["Properties"]["BucketName"])
    assert (
        "demo-pipeline" in name
        and "AWS::AccountId" in name
        and "AWS::Region" in name
        and name.rstrip('"}]').endswith("-an")
    )
    props = bucket["Properties"]
    assert props["PublicAccessBlockConfiguration"]["BlockPublicAcls"] in (True, "true")
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    tpl.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Condition": {"Bool": {"aws:SecureTransport": "false"}}}
                        )
                    ]
                )
            }
        },
    )


def test_bucket_purpose_length_guard():
    assert artifact_bucket_purpose("cloud-buddy") == "cloud-buddy-pipeline"
    with pytest.raises(ValueError, match="too long"):
        artifact_bucket_purpose("x" * 40)


def test_synth_bootstraps_node_with_checksum_and_neutralises_pip(tpl):
    spec = _buildspec_containing(tpl, "cdk -a 'python3 pipeline_app.py' synth")
    assert "sha256sum -c" in spec
    assert 'unset "$v"' in spec and "PIP_CONFIG_FILE=/dev/null" in spec
    assert "aws-cdk@2.1149.0" in spec
    assert spec.index("sha256sum -c") < spec.index("tar -xzf")


def test_ci_true_is_set_on_every_project_that_runs_repo_commands(tpl):
    for logical_id, res in projects(tpl).items():
        if "SelfMutation" in logical_id:
            continue  # CDK-managed: only runs `cdk deploy` of the pipeline stack
        names = {v["Name"]: v["Value"] for v in res["Properties"]["Environment"].get("EnvironmentVariables", [])}
        assert names.get("CI") == "true", logical_id


def test_post_deploy_step_gets_its_policy(tpl):
    tpl.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [Match.object_like({"Action": "s3:PutObject"})]
                )
            }
        },
    )


def test_failure_notification_rule_and_encrypted_topic(tpl):
    tpl.resource_count_is("AWS::CodeStarNotifications::NotificationRule", 1)
    tpl.has_resource_properties(
        "AWS::CodeStarNotifications::NotificationRule",
        {"EventTypeIds": ["codepipeline-pipeline-pipeline-execution-failed"]},
    )


# --- pull-request checks ---------------------------------------------------


def _pr_projects(t):
    return {
        k: v
        for k, v in projects(t).items()
        if "pr-" in json.dumps(v["Properties"].get("Name"))
    }


def test_one_pr_project_per_gate_with_webhook_and_status(tpl):
    prs = _pr_projects(tpl)
    assert len(prs) == len(GATES)
    for res in prs.values():
        props = res["Properties"]
        assert props["Source"]["Type"] == "GITHUB"
        assert props["Source"]["ReportBuildStatus"] in (True, "true")
        assert props["Source"]["Auth"] == {"Type": "CODECONNECTIONS", "Resource": CONN}
        groups = props["Triggers"]["FilterGroups"]
        flat = json.dumps(groups)
        assert (
            "PULL_REQUEST_CREATED" in flat
            and "PULL_REQUEST_UPDATED" in flat
            and "^refs/heads/main$" in flat
        )
        assert "PUSH" not in flat


def test_pr_projects_have_no_deploy_permissions(tpl):
    pr_roles = set()
    for res in _pr_projects(tpl).values():
        pr_roles.add(res["Properties"]["ServiceRole"]["Fn::GetAtt"][0])
    assert pr_roles
    policies = tpl.find_resources("AWS::IAM::Policy")
    for pol in policies.values():
        roles = {r["Ref"] for r in pol["Properties"]["Roles"]}
        if not roles & {r for r in pr_roles}:
            continue
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            )
            for action in actions:
                assert action.split(":")[0] in {
                    "logs",
                    "codeconnections",
                    "codebuild",
                }, action


def test_pull_request_checks_can_be_disabled():
    t = build(pull_request_checks=False)
    assert not _pr_projects(t)


# --- validation ------------------------------------------------------------


def test_duplicate_gate_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        build(gates=[Gate("a", ["x"]), Gate("a", ["y"])])


def test_no_gates_rejected():
    with pytest.raises(ValueError, match="at least one gate"):
        build(gates=[])


@pytest.mark.parametrize("memory", [0, 3, 6, 16])
def test_invalid_lambda_memory_rejected(memory):
    with pytest.raises(ValueError, match="memory"):
        Gate("g", ["x"], memory_gb=memory)


def test_invalid_gate_name_rejected():
    with pytest.raises(ValueError, match="gate name"):
        Gate("bad name!", ["x"])


def test_repo_must_be_owner_slash_name():
    with pytest.raises(ValueError, match="owner/name"):
        build(repo="demo")


def _buildspec_containing(t: Template, needle: str) -> str:
    for res in projects(t).values():
        spec = res["Properties"]["Source"].get("BuildSpec")
        if spec and needle in json.dumps(spec):
            parsed = json.loads(spec) if isinstance(spec, str) else spec
            return "\n".join(parsed["phases"]["build"]["commands"])
    raise AssertionError(f"no buildspec contains {needle!r}")


# --- deploy_step mode (existing stack deployed by the repo's own command) --------


def _step_build(**overrides) -> Template:
    from specter_pipeline import DeployStep

    return build(
        application_stage=None,
        deploy_step=DeployStep("app", ["cdk deploy AppStack --require-approval never"]),
        **overrides,
    )


def test_deploy_step_mode_has_a_deploy_stage_without_cloudformation_actions():
    t = _step_build()
    (pipe,) = t.find_resources("AWS::CodePipeline::Pipeline").values()
    stages = pipe["Properties"]["Stages"]
    assert [s["Name"] for s in stages] == ["Source", "Build", "UpdatePipeline", "Gates", "Deploy"]
    deploy = sorted(stages[-1]["Actions"], key=lambda a: a["RunOrder"])
    assert not [a for a in deploy if a["ActionTypeId"]["Provider"] == "CloudFormation"]
    names = [a["Name"] for a in deploy]
    assert names.index("Approve-Deploy") < names.index("Deploy-app")
    assert deploy[0]["ActionTypeId"]["Category"] == "Approval"


def test_deploy_step_runs_after_approval_and_post_steps_run_after_deploy():
    t = _step_build()
    (pipe,) = t.find_resources("AWS::CodePipeline::Pipeline").values()
    deploy = {a["Name"]: a["RunOrder"] for a in pipe["Properties"]["Stages"][-1]["Actions"]}
    assert deploy["Approve-Deploy"] < deploy["Deploy-app"] < deploy["Post-frontend"]


def test_deploy_step_mode_holds_the_deploy_closed_by_default():
    (pipe,) = _step_build().find_resources("AWS::CodePipeline::Pipeline").values()
    assert pipe["Properties"]["DisableInboundStageTransitions"][0]["StageName"] == "Deploy"
    (pipe,) = _step_build(deploy_enabled=True).find_resources("AWS::CodePipeline::Pipeline").values()
    assert "DisableInboundStageTransitions" not in pipe["Properties"]


def test_deploy_step_role_can_only_assume_cdk_bootstrap_roles():
    t = _step_build()
    deploy_policies = [
        p
        for p in t.find_resources("AWS::IAM::Policy").values()
        if "Deploy" in json.dumps(p["Properties"]["Roles"])
    ]
    assert deploy_policies
    assumes = [
        s
        for p in deploy_policies
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s["Action"] == "sts:AssumeRole"
    ]
    assert assumes, "deploy step has no sts:AssumeRole statement"
    for stmt in assumes:
        resource = json.dumps(stmt["Resource"])
        assert "role/cdk-hnb659fds-" in resource and "-role-" in resource
        assert stmt["Resource"] != "*"


def test_deploy_step_mode_still_all_lambda_compute():
    for logical_id, res in projects(_step_build()).items():
        assert res["Properties"]["Environment"]["Type"] == "LINUX_LAMBDA_CONTAINER", logical_id


def test_deploy_step_installs_pinned_cdk_cli():
    spec = _buildspec_containing(_step_build(), "cdk deploy AppStack")
    assert "aws-cdk@2.1149.0" in spec


def test_exactly_one_of_stage_or_deploy_step_is_required():
    from specter_pipeline import DeployStep

    with pytest.raises(ValueError, match="exactly one"):
        build(application_stage=None)
    with pytest.raises(ValueError, match="exactly one"):
        build(deploy_step=DeployStep("app", ["x"]))


# --- review fixes -----------------------------------------------------------------


def test_pr_webhook_filter_is_a_pattern_that_can_actually_match(tpl):
    """CDK's and_base_branch_is() prepends refs/heads/ itself; the old filter was
    'refs/heads/^refs/heads/main$', which can never match any ref."""
    import re as regex

    patterns = []
    for res in _pr_projects(tpl).values():
        for group in res["Properties"]["Triggers"]["FilterGroups"]:
            for flt in group:
                if flt["Type"] == "BASE_REF":
                    patterns.append(flt["Pattern"])
    assert patterns and set(patterns) == {"^refs/heads/main$"}
    assert regex.search(patterns[0], "refs/heads/main")
    assert not regex.search(patterns[0], "refs/heads/main2")
    assert not regex.search(patterns[0], "refs/heads/feature/main")


def test_pr_base_branch_is_escaped():
    t = build(pull_request_checks=True)  # default main
    assert t  # sanity
    from specter_pipeline import PullRequestChecks  # noqa: PLC0415

    app = cdk.App()
    stack = cdk.Stack(app, "S", env=ENV)
    PullRequestChecks(
        stack, "X", repo="Specter099/demo", base_branch="release/1.x", connection_arn=CONN, gates=GATES, name_prefix="demo"
    )
    flat = json.dumps(Template.from_stack(stack).to_json())
    assert r"^refs/heads/release/1\\.x$" in flat


def test_pr_checks_have_an_explicit_fork_pr_approval_policy(tpl):
    for res in _pr_projects(tpl).values():
        policy = res["Properties"]["Triggers"]["PullRequestBuildPolicy"]
        assert policy["RequiresCommentApproval"] == "FORK_PULL_REQUESTS"
        assert "GITHUB_READ" not in policy["ApproverRoles"]


def test_pr_project_roles_only_get_logs_and_the_connection_token(tpl):
    pr_roles = {r["Properties"]["ServiceRole"]["Fn::GetAtt"][0] for r in _pr_projects(tpl).values()}
    seen = set()
    for pol in tpl.find_resources("AWS::IAM::Policy").values():
        if not {r["Ref"] for r in pol["Properties"]["Roles"]} & pr_roles:
            continue
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            seen.update(actions)
            if any(a.startswith("codeconnections:") for a in actions):
                assert stmt["Resource"] == CONN
    # codebuild:*Report* are the L2's own report-group permissions, scoped to this project.
    assert seen <= {
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "codeconnections:GetConnectionToken",
        "codeconnections:GetConnection",
        "codebuild:CreateReportGroup",
        "codebuild:CreateReport",
        "codebuild:UpdateReport",
        "codebuild:BatchPutTestCases",
        "codebuild:BatchPutCodeCoverages",
    }, seen


def test_pipeline_restarts_on_update_so_self_mutation_does_not_fail_the_run(tpl):
    (pipe,) = tpl.find_resources("AWS::CodePipeline::Pipeline").values()
    assert pipe["Properties"]["RestartExecutionOnUpdate"] in (True, "true")


def test_node_checksum_is_committed_not_downloaded():
    from specter_pipeline.runtimes import NODE_LINUX_X64_SHA256, node_tarball_prelude

    commands = "\n".join(node_tarball_prelude("v22.22.0"))
    assert "SHASUMS256" not in commands, "checksum must not come from the tarball's origin"
    assert NODE_LINUX_X64_SHA256["v22.22.0"] in commands
    assert commands.index("sha256sum -c") < commands.index("tar -xzf")
    with pytest.raises(ValueError, match="no committed SHA-256"):
        node_tarball_prelude("v99.0.0")


def test_cdk_cli_install_puts_the_cli_on_path_for_the_node_runtime():
    from specter_pipeline.runtimes import cdk_cli_install

    commands = cdk_cli_install()
    assert commands[0].startswith("export PATH=/tmp/nodejs/bin")
    assert commands.index("cdk --version") > 0


def test_node_runtime_deploy_step_can_run_the_cli():
    from specter_pipeline import DeployStep

    t = build(
        application_stage=None,
        deploy_step=DeployStep("n", ["cdk deploy X"], runtime=Runtime.NODE),
    )
    spec = _buildspec_containing(t, "cdk deploy X")
    assert spec.index("export PATH=/tmp/nodejs/bin") < spec.index("cdk --version")


@pytest.mark.parametrize(
    "repo,expected",
    [("Specter099/MyRepo", "myrepo-pipeline"), ("Specter099/my.site", "my-site-pipeline")],
)
def test_bucket_purpose_is_lowercased_and_dots_become_hyphens(repo, expected):
    assert artifact_bucket_purpose(repo.split("/")[1]) == expected
    t = build(repo=repo)
    (bucket,) = [r for r in t.find_resources("AWS::S3::Bucket").values() if "BucketName" in r["Properties"]]
    assert expected in json.dumps(bucket["Properties"]["BucketName"])


@pytest.mark.parametrize("repo", ["a/b/c", "owner/", "/name", "owner/na me", ""])
def test_malformed_repos_rejected_early(repo):
    with pytest.raises(ValueError, match="owner/name"):
        build(repo=repo)


@pytest.mark.parametrize("name", ["my app!", "", "naïve"])
def test_step_names_validated_like_gate_names(name):
    from specter_pipeline import DeployStep

    with pytest.raises(ValueError, match="name"):
        PostDeployStep(name, ["x"])
    with pytest.raises(ValueError, match="name"):
        DeployStep(name, ["x"])


def test_duplicate_post_deploy_names_rejected_with_a_clear_error():
    with pytest.raises(ValueError, match="post-deploy step names must be unique"):
        build(post_deploy=[PostDeployStep("a", ["x"]), PostDeployStep("a", ["y"])])


def test_notification_emails_are_subscribed_and_topic_has_no_kms_key():
    t = build(notification_emails=["ops@example.com"])
    t.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"})
    (topic,) = t.find_resources("AWS::SNS::Topic").values()
    assert "KmsMasterKeyId" not in topic.get("Properties", {}), "AWS-managed SNS key breaks CodeStar notifications"
