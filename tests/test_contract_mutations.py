"""Every contract helper is proven against a template that violates it.

Each test takes a pipeline that satisfies the contract, changes ONE thing in its synthesized
template (``to_json`` then ``Template.from_json``) and asserts the named helper rejects it. That
way a helper that silently stops checking fails here, instead of passing on good pipelines only.
"""

import copy

import pytest
from aws_cdk.assertions import Template
from test_pipeline import GATES, _step_build

from specter_pipeline import Gate
from specter_pipeline import testing as c

PREFIX = "demo"


def mutated(change, **build_kwargs) -> Template:
    doc = copy.deepcopy(_step_build(**build_kwargs).to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def pr_role_id(res) -> str:
    project = next(
        r for r in res.values() if r["Type"] == "AWS::CodeBuild::Project" and str(r["Properties"].get("Name", "")).startswith("demo-pr-")
    )
    return project["Properties"]["ServiceRole"]["Fn::GetAtt"][0]


def pr_policy(res) -> dict:
    role = pr_role_id(res)
    return next(
        r["Properties"]["PolicyDocument"]["Statement"]
        for r in res.values()
        if r["Type"] == "AWS::IAM::Policy" and {x["Ref"] for x in r["Properties"]["Roles"]} == {role}
    )


def stage(res, name):
    pipe = next(r for r in res.values() if r["Type"] == "AWS::CodePipeline::Pipeline")
    return next(s for s in pipe["Properties"]["Stages"] if s["Name"] == name)


def pipe_props(res):
    return next(r for r in res.values() if r["Type"] == "AWS::CodePipeline::Pipeline")["Properties"]


# --- PR-check role least privilege -----------------------------------------------------------


def test_the_unmodified_template_passes():
    c.assert_pr_roles_least_privilege(_step_build(), PREFIX)


@pytest.mark.parametrize(
    "extra",
    [
        {"Effect": "Allow", "Action": "logs:*", "Resource": "*"},
        {"Effect": "Allow", "Action": "codeconnections:*", "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:GetLogEvents", "logs:DeleteLogGroup"], "Resource": "*"},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"},
        {"Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "*"},
        {"Effect": "Allow", "NotAction": "iam:*", "Resource": "arn:aws:logs:::x"},
    ],
)
def test_pr_role_rejects_extra_statements(extra):
    t = mutated(lambda res: pr_policy(res).append(extra))
    with pytest.raises(AssertionError, match="PR-check role|NotAction"):
        c.assert_pr_roles_least_privilege(t, PREFIX)


def test_pr_role_rejects_a_managed_policy_on_the_role():
    def change(res):
        res[pr_role_id(res)]["Properties"]["ManagedPolicyArns"] = ["arn:aws:iam::aws:policy/AdministratorAccess"]

    with pytest.raises(AssertionError, match="managed policies"):
        c.assert_pr_roles_least_privilege(mutated(change), PREFIX)


def test_pr_role_rejects_an_inline_policy_on_the_role():
    def change(res):
        res[pr_role_id(res)]["Properties"]["Policies"] = [
            {"PolicyName": "x", "PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "iam:*", "Resource": "arn:aws:iam::1:role/x"}]}}
        ]

    with pytest.raises(AssertionError, match="PR-check role has iam"):
        c.assert_pr_roles_least_privilege(mutated(change), PREFIX)


def test_pr_role_check_is_not_vacuous_for_a_wrong_prefix():
    with pytest.raises(AssertionError, match="no PR-check projects"):
        c.assert_pr_roles_least_privilege(_step_build(), "wrong")


# --- wildcard resources ------------------------------------------------------------------------


def _some_policy(res):
    return next(r for r in res.values() if r["Type"] == "AWS::IAM::Policy")["Properties"]["PolicyDocument"]["Statement"]


@pytest.mark.parametrize(
    "resource",
    [["*", "arn:aws:s3:::b"], "arn:aws:s3:::*", "*"],
)
def test_wildcard_check_catches_a_star_in_any_position(resource):
    t = mutated(lambda res: _some_policy(res).append({"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": resource}))
    with pytest.raises(AssertionError, match="wildcard resource for sqs:SendMessage"):
        c.assert_no_wildcard_resources(t)


def test_wildcard_check_scans_role_inline_policies():
    def change(res):
        rid = next(k for k, r in res.items() if r["Type"] == "AWS::IAM::Role")
        res[rid]["Properties"]["Policies"] = [
            {"PolicyName": "x", "PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "ec2:*", "Resource": "*"}]}}
        ]

    with pytest.raises(AssertionError, match="wildcard resource for ec2"):
        c.assert_no_wildcard_resources(mutated(change))


def test_cdk_internal_wildcards_are_only_allowed_on_the_self_mutation_role():
    t = _step_build()
    c.assert_no_wildcard_resources(t, allowed_actions=c.CDK_INTERNAL_WILDCARDS)  # as built

    def change(res):
        pol = next(
            r for k, r in res.items() if r["Type"] == "AWS::IAM::Policy" and "SelfMutat" not in str(r["Properties"].get("Roles"))
        )
        pol["Properties"]["PolicyDocument"]["Statement"].append(
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "*"}
        )

    with pytest.raises(AssertionError, match="wildcard resource for s3:ListBucket"):
        c.assert_no_wildcard_resources(mutated(change), allowed_actions=set())


def test_wildcard_check_rejects_not_action():
    t = mutated(lambda res: _some_policy(res).append({"Effect": "Allow", "NotAction": "iam:*", "Resource": "arn:aws:s3:::b"}))
    with pytest.raises(AssertionError, match="NotAction"):
        c.assert_no_wildcard_resources(t)


# --- ordering ------------------------------------------------------------------------------------


def test_ordering_catches_deploy_sharing_the_approvals_run_order():
    def change(res):
        acts = stage(res, "Deploy")["Actions"]
        next(a for a in acts if a["Name"] == "Deploy-app")["RunOrder"] = 1

    with pytest.raises(AssertionError, match="does not wait for approval"):
        c.assert_deploy_ordering(mutated(change))


def test_ordering_catches_an_unrecognised_action_beside_the_approval():
    def change(res):
        acts = stage(res, "Deploy")["Actions"]
        extra = copy.deepcopy(next(a for a in acts if a["Name"] == "Deploy-app"))
        extra["Name"] = "Sneaky"
        extra["RunOrder"] = 1
        acts.append(extra)

    with pytest.raises(AssertionError, match="Sneaky"):
        c.assert_deploy_ordering(mutated(change))


def test_ordering_catches_a_post_step_that_does_not_wait_for_the_deploy():
    def change(res):
        acts = stage(res, "Deploy")["Actions"]
        next(a for a in acts if a["Name"] == "Post-frontend")["RunOrder"] = 2

    with pytest.raises(AssertionError, match="does not wait for the deploy"):
        c.assert_deploy_ordering(mutated(change))


def test_ordering_without_an_approval_is_supported_only_when_declared():
    t = _step_build(require_approval=False)
    c.assert_deploy_ordering(t, require_approval=False)
    with pytest.raises(AssertionError, match="exactly one approval"):
        c.assert_deploy_ordering(t)
    with pytest.raises(AssertionError, match="require_approval is False"):
        c.assert_deploy_ordering(_step_build(), require_approval=False)


# --- compute, env, triggers, restart ------------------------------------------------------------


def _projects(res):
    return [r["Properties"] for r in res.values() if r["Type"] == "AWS::CodeBuild::Project"]


def test_lambda_compute_catches_a_container_project():
    with pytest.raises(AssertionError, match="not on Lambda compute"):
        c.assert_lambda_compute(mutated(lambda res: _projects(res)[0]["Environment"].update(Type="LINUX_CONTAINER")))


@pytest.mark.parametrize("key,value", [("TimeoutInMinutes", 30), ("VpcConfig", {"VpcId": "v"}), ("Cache", {"Type": "S3"})])
def test_lambda_compute_catches_unsupported_settings(key, value):
    with pytest.raises(AssertionError, match=key if key != "Cache" else "caching"):
        c.assert_lambda_compute(mutated(lambda res: _projects(res)[0].update({key: value})))


def test_lambda_compute_catches_privileged_mode():
    with pytest.raises(AssertionError, match="privileged"):
        c.assert_lambda_compute(mutated(lambda res: _projects(res)[0]["Environment"].update(PrivilegedMode=True)))


def test_ci_env_catches_a_project_without_ci_true():
    def change(res):
        p = next(p for p in _projects(res) if str(p.get("Name", "")).startswith("demo-pr-"))
        p["Environment"]["EnvironmentVariables"] = []

    with pytest.raises(AssertionError, match="does not set CI=true"):
        c.assert_ci_env_on_repo_command_projects(mutated(change))


def test_no_triggers_catches_a_pipeline_trigger():
    with pytest.raises(AssertionError, match="has triggers"):
        c.assert_no_pipeline_triggers(mutated(lambda res: pipe_props(res).update(Triggers=[{"ProviderType": "CodeStarSourceConnection"}])))


def test_restart_on_update_catches_it_being_off():
    with pytest.raises(AssertionError, match="RestartExecutionOnUpdate"):
        c.assert_restart_on_update(mutated(lambda res: pipe_props(res).update(RestartExecutionOnUpdate=False)))


# --- pull-request checks -----------------------------------------------------------------------


def _pr_triggers(res):
    return next(p for p in _projects(res) if str(p.get("Name", "")).startswith("demo-pr-"))["Triggers"]


def test_pr_checks_catch_an_unanchored_base_ref_filter():
    def change(res):
        for f in _pr_triggers(res)["FilterGroups"][0]:
            if f["Type"] == "BASE_REF":
                f["Pattern"] = "refs/heads/main"

    with pytest.raises(AssertionError, match="not anchored"):
        c.assert_pr_checks(mutated(change), GATES, PREFIX)


def test_pr_checks_catch_a_push_trigger():
    def change(res):
        for f in _pr_triggers(res)["FilterGroups"][0]:
            if f["Type"] == "EVENT":
                f["Pattern"] += ", PUSH"

    with pytest.raises(AssertionError, match="must not fire on push"):
        c.assert_pr_checks(mutated(change), GATES, PREFIX)


def test_pr_checks_catch_fork_prs_without_approval():
    with pytest.raises(AssertionError, match="fork PRs"):
        c.assert_pr_checks(mutated(lambda res: _pr_triggers(res)["PullRequestBuildPolicy"].update(RequiresCommentApproval="DISABLED")), GATES, PREFIX)


def test_pr_checks_catch_a_base_ref_filter_that_cannot_match():
    def change(res):
        for f in _pr_triggers(res)["FilterGroups"][0]:
            if f["Type"] == "BASE_REF":
                f["Pattern"] = "^refs/heads/release$"

    with pytest.raises(AssertionError, match="cannot match"):
        c.assert_pr_checks(mutated(change), GATES, PREFIX)


# --- artifact bucket ---------------------------------------------------------------------------


def _bucket(res):
    return next(r["Properties"] for r in res.values() if r["Type"] == "AWS::S3::Bucket" and "BucketNamePrefix" in r["Properties"])


def test_bucket_catches_a_literal_name():
    with pytest.raises(AssertionError, match="literal BucketName"):
        c.assert_artifact_bucket(mutated(lambda res: _bucket(res).update(BucketName="demo-x")), PREFIX)


def test_bucket_catches_the_global_namespace():
    with pytest.raises(AssertionError, match="account-regional"):
        c.assert_artifact_bucket(mutated(lambda res: _bucket(res).update(BucketNamespace="global")), PREFIX)


def test_bucket_catches_unversioned_and_public():
    with pytest.raises(AssertionError, match="not versioned"):
        c.assert_artifact_bucket(mutated(lambda res: _bucket(res).update(VersioningConfiguration={"Status": "Suspended"})), PREFIX)
    with pytest.raises(AssertionError, match="allows public access"):
        c.assert_artifact_bucket(mutated(lambda res: _bucket(res)["PublicAccessBlockConfiguration"].update(BlockPublicAcls=False)), PREFIX)


# --- stage order with CDK's optional Assets stage ----------------------------------------------


def test_stage_order_accepts_cdks_optional_assets_stage_but_only_in_its_place():
    def add_assets(res):
        stages = pipe_props(res)["Stages"]
        stages.insert(3, {"Name": "Assets", "Actions": []})

    c.assert_stage_order(mutated(add_assets), "Deploy")

    def misplace(res):
        stages = pipe_props(res)["Stages"]
        stages.insert(1, {"Name": "Assets", "Actions": []})

    with pytest.raises(AssertionError, match="Assets in the wrong place"):
        c.assert_stage_order(mutated(misplace), "Deploy")


# --- Gate cannot be changed after validation ----------------------------------------------------


def test_gate_copies_its_sequences_so_a_later_append_cannot_add_permissions():
    from aws_cdk import aws_iam as iam

    stmts: list = []
    g = Gate("g", ["true"], policy_statements=stmts)
    stmts.append(iam.PolicyStatement(actions=["s3:*"], resources=["*"]))
    assert g.policy_statements == ()
    assert g.runs_on_pull_requests is True


def test_gate_pull_request_must_be_a_real_bool():
    with pytest.raises(TypeError, match="pull_request"):
        Gate("g", ["true"], pull_request="false")  # type: ignore[arg-type]


def test_pull_request_checks_never_read_gate_policy_statements():
    """The structural guarantee behind the PR-role contract: a gate with its own IAM is not given a
    PR project at all."""
    from specter_pipeline import StandardPipeline as _  # noqa: F401  (import check)

    t = _step_build()
    names = {p["Name"] for p in (r["Properties"] for r in t.find_resources("AWS::CodeBuild::Project").values()) if "Name" in p}
    assert not any(n.endswith("-pr-access-analyzer") for n in names)
