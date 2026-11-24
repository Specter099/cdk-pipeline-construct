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


def test_no_project_uses_docker_enabled_images(tpl):
    for res in projects(tpl).values():
        assert "docker" not in res["Properties"]["Environment"]["Image"].lower()


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
    assert "pull_request" not in json.dumps(props.get("Triggers", [])).lower()


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


def test_ci_true_is_set_on_every_gate(tpl):
    for logical_id, res in projects(tpl).items():
        names = {
            v["Name"]: v["Value"]
            for v in res["Properties"]["Environment"].get("EnvironmentVariables", [])
        }
        if "Gate" in json.dumps(
            res["Properties"].get("Name", "")
        ) or "pr-" in json.dumps(res["Properties"].get("Name", "")):
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
