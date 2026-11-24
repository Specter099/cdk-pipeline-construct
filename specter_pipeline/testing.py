"""Reusable contract assertions for pipelines built with ``StandardPipeline``.

Every consuming repo needs to prove the same things about its synthesized pipeline
stack. Those invariants live here so a repo's own test file is a handful of lines plus
whatever is specific to that repo (for example, that its deploy command matches its
CI workflow). Each function raises ``AssertionError`` with a message that names the
violated invariant; none depends on pytest.

    from specter_pipeline.testing import assert_standard_contract

    def test_pipeline_contract():
        assert_standard_contract(template, gates=GATES, name_prefix="my-repo")

Every invariant here was either found by a real defect or measured against a live
pipeline; the comments say which.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from aws_cdk.assertions import Template

from .gate import Gate
from .pipeline import artifact_bucket_purpose
from .runtimes import Runtime

#: Settings CodeBuild rejects or ignores on Lambda compute.
_UNSUPPORTED_ON_LAMBDA = (
    "TimeoutInMinutes",
    "QueuedTimeoutInMinutes",
    "VpcConfig",
    "FileSystemLocations",
)

#: Actions a PR-check role may hold: its logs, the connection token, and the report
#: permissions CodeBuild adds for the project's own report group.
_PR_ROLE_ALLOWED_PREFIXES = (
    "logs:",
    "codeconnections:",
    "codebuild:CreateReport",
    "codebuild:UpdateReport",
    "codebuild:BatchPut",
)


def pipeline(template: Template) -> dict:
    """Properties of the single CodePipeline in ``template``."""
    found = template.find_resources("AWS::CodePipeline::Pipeline")
    assert len(found) == 1, f"expected exactly one pipeline, found {len(found)}"
    return next(iter(found.values()))["Properties"]


def projects(template: Template) -> dict:
    return template.find_resources("AWS::CodeBuild::Project")


def pr_projects(template: Template, name_prefix: str) -> dict:
    return {
        lid: res
        for lid, res in projects(template).items()
        if str(res["Properties"].get("Name", "")).startswith(f"{name_prefix}-pr-")
    }


def _stage(template: Template, name: str) -> dict:
    stages = {s["Name"]: s for s in pipeline(template)["Stages"]}
    assert name in stages, f"pipeline has no stage {name!r} (stages: {list(stages)})"
    return stages[name]


# --- individual invariants ----------------------------------------------------------


def assert_lambda_compute(template: Template) -> None:
    """Every CodeBuild project, including CDK Pipelines' own, runs on Lambda compute."""
    found = projects(template)
    assert found, "no CodeBuild projects in the template"
    for logical_id, res in found.items():
        props = res["Properties"]
        assert props["Environment"]["Type"] == "LINUX_LAMBDA_CONTAINER", (
            f"{logical_id} is not on Lambda compute"
        )
        assert not props["Environment"].get("PrivilegedMode"), (
            f"{logical_id} is privileged (Docker is unsupported on Lambda compute)"
        )
        for key in _UNSUPPORTED_ON_LAMBDA:
            assert key not in props, (
                f"{logical_id} sets {key}, which Lambda compute does not support"
            )
        # NO_CACHE is CloudFormation's explicit default; anything else is real caching.
        assert props.get("Cache", {"Type": "NO_CACHE"}) == {"Type": "NO_CACHE"}, (
            f"{logical_id} enables caching"
        )


def assert_ci_env_on_repo_command_projects(template: Template) -> None:
    """CI=true is set wherever repo commands run (scripts then use the ambient interpreter)."""
    for logical_id, res in projects(template).items():
        if "SelfMutation" in logical_id:
            continue  # CDK-managed; only runs `cdk deploy` of the pipeline stack
        env = {
            v["Name"]: v["Value"]
            for v in res["Properties"]["Environment"].get("EnvironmentVariables", [])
        }
        assert env.get("CI") == "true", f"{logical_id} does not set CI=true"


def assert_stage_order(template: Template, deploy_stage: str = "Deploy") -> None:
    names = [s["Name"] for s in pipeline(template)["Stages"]]
    assert names == ["Source", "Build", "UpdatePipeline", "Gates", deploy_stage], (
        f"unexpected stage order: {names}"
    )


def assert_gates_present(template: Template, gates: Sequence[Gate]) -> None:
    present = {a["Name"] for a in _stage(template, "Gates")["Actions"]}
    expected = {f"Gate-{g.name}" for g in gates}
    assert present == expected, (
        f"gates differ: missing={sorted(expected - present)} extra={sorted(present - expected)}"
    )
    assert len({a["RunOrder"] for a in _stage(template, "Gates")["Actions"]}) == 1, (
        "gates should run in parallel"
    )


def assert_deploy_closed(template: Template, deploy_stage: str = "Deploy") -> None:
    """The first execution starts when the pipeline is created, so the deploy stage must be
    closed in the template itself (verified live: it never ran on the creation-time execution)."""
    closed = pipeline(template).get("DisableInboundStageTransitions", [])
    assert [c["StageName"] for c in closed] == [deploy_stage], (
        f"deploy stage {deploy_stage!r} is not closed: {closed}"
    )


def assert_deploy_open(template: Template) -> None:
    assert "DisableInboundStageTransitions" not in pipeline(template), (
        "deploy stage is closed but should be open"
    )


def assert_deploy_ordering(
    template: Template,
    deploy_stage: str = "Deploy",
    deploy_prefix: str = "Deploy-",
    post_prefix: str = "Post-",
) -> None:
    """Approval strictly precedes deploy, which strictly precedes post-deploy steps.

    Equal RunOrder means the actions run in parallel: in a wave with no stacks the approval
    and the deploy once shared a RunOrder, i.e. the deploy did not wait for approval."""
    actions = _stage(template, deploy_stage)["Actions"]
    approvals = [a for a in actions if a["ActionTypeId"]["Category"] == "Approval"]
    assert len(approvals) == 1, (
        f"expected exactly one approval in {deploy_stage!r}, found {len(approvals)}"
    )
    approval = approvals[0]["RunOrder"]
    deploys = [
        a
        for a in actions
        if a["Name"].startswith(deploy_prefix)
        or a["ActionTypeId"]["Provider"] == "CloudFormation"
    ]
    assert deploys, f"no deploy action in {deploy_stage!r}"
    for a in deploys:
        assert approval < a["RunOrder"], (
            f"{a['Name']} (RunOrder {a['RunOrder']}) does not wait for approval ({approval})"
        )
    posts = [a for a in actions if a["Name"].startswith(post_prefix)]
    for a in posts:
        assert max(d["RunOrder"] for d in deploys) < a["RunOrder"], (
            f"{a['Name']} does not wait for the deploy"
        )


def assert_no_pipeline_triggers(template: Template) -> None:
    """A pull-request trigger would run self-mutation from a PR branch before any approval.
    CloudFormation spells the key 'PullRequest', so check for no triggers at all."""
    assert "Triggers" not in pipeline(template), (
        "the pipeline has triggers; PRs must use the separate PR-check projects"
    )


def assert_restart_on_update(template: Template) -> None:
    """Without it an update (self-mutation) fails the running execution."""
    assert pipeline(template).get("RestartExecutionOnUpdate") in (True, "true"), (
        "RestartExecutionOnUpdate is not true"
    )


def assert_source(
    template: Template,
    repo: str,
    branch: str = "main",
    connection_arn: str | None = None,
) -> None:
    cfg = _stage(template, "Source")["Actions"][0]["Configuration"]
    assert cfg["FullRepositoryId"] == repo, (
        f"source repo is {cfg['FullRepositoryId']!r}, expected {repo!r}"
    )
    assert cfg["BranchName"] == branch, (
        f"source branch is {cfg['BranchName']!r}, expected {branch!r}"
    )
    if connection_arn is not None:
        assert cfg["ConnectionArn"] == connection_arn, (
            "source uses a different connection"
        )


def assert_artifact_bucket(template: Template, name: str) -> None:
    """A '...-{account}-{region}-an' name needs the account-regional namespace; a literal
    BucketName fails to create (found on a real deploy attempt)."""
    buckets = [
        r["Properties"]
        for r in template.find_resources("AWS::S3::Bucket").values()
        if "BucketNamePrefix" in r["Properties"]
    ]
    assert len(buckets) == 1, (
        f"expected one prefixed artifact bucket, found {len(buckets)}"
    )
    props = buckets[0]
    assert "BucketName" not in props, "artifact bucket uses a literal BucketName"
    expected = artifact_bucket_purpose(name)
    assert props["BucketNamePrefix"] == expected, (
        f"unexpected bucket prefix {props['BucketNamePrefix']!r}, expected {expected!r}"
    )
    assert props["BucketNamespace"] == "account-regional", (
        "artifact bucket is not in the account-regional namespace"
    )
    assert props["VersioningConfiguration"]["Status"] == "Enabled", (
        "artifact bucket is not versioned"
    )
    assert props["PublicAccessBlockConfiguration"]["BlockPublicAcls"] in (
        True,
        "true",
    ), "artifact bucket allows public access"


def assert_pr_checks(
    template: Template,
    gates: Sequence[Gate],
    name_prefix: str,
    base_branch: str = "main",
    connection_arn: str | None = None,
) -> None:
    """One webhook project per gate, matching PRs into ``base_branch`` only."""
    found = pr_projects(template, name_prefix)
    assert {r["Properties"]["Name"] for r in found.values()} == {
        f"{name_prefix}-pr-{g.name}" for g in gates if g.runs_on_pull_requests
    }, "PR check projects differ from the gates"
    for res in found.values():
        props = res["Properties"]
        assert props["Source"]["Type"] == "GITHUB"
        assert props["Source"]["ReportBuildStatus"] in (True, "true"), (
            "PR checks must report a commit status"
        )
        auth = props["Source"]["Auth"]
        assert auth["Type"] == "CODECONNECTIONS", (
            "PR checks must authenticate through the shared connection"
        )
        if connection_arn is not None:
            assert auth["Resource"] == connection_arn
        (group,) = props["Triggers"]["FilterGroups"]
        by_type = {f["Type"]: f["Pattern"] for f in group}
        # CDK's and_base_branch_is() prepends refs/heads/ itself; the old filter could never match.
        pattern = by_type["BASE_REF"]
        assert re.search(pattern, f"refs/heads/{base_branch}"), (
            f"PR filter {pattern!r} cannot match {base_branch!r}"
        )
        assert not re.search(pattern, f"refs/heads/{base_branch}-other"), (
            f"PR filter {pattern!r} is not anchored"
        )
        assert "PUSH" not in by_type["EVENT"], "PR checks must not fire on push"
        policy = props["Triggers"]["PullRequestBuildPolicy"]
        assert policy["RequiresCommentApproval"] == "FORK_PULL_REQUESTS", (
            "fork PRs must need an approval comment"
        )


def assert_pr_roles_least_privilege(template: Template, name_prefix: str) -> None:
    """PR-check projects run PR-head code: logs, the connection token and report permissions only."""
    roles = {
        res["Properties"]["ServiceRole"]["Fn::GetAtt"][0]
        for res in pr_projects(template, name_prefix).values()
    }
    for pol in template.find_resources("AWS::IAM::Policy").values():
        if not {r["Ref"] for r in pol["Properties"]["Roles"]} & roles:
            continue
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            )
            for action in actions:
                assert action.startswith(_PR_ROLE_ALLOWED_PREFIXES), (
                    f"PR-check role has {action}"
                )


def assert_gates_have_node(gates: Sequence[Gate], names: Iterable[str]) -> None:
    """On Lambda compute the Python image has no Node, and aws_cdk / the CDK CLI need it.
    (All backend shards failed with "PermissionError: 'node'" before this was enforced.)"""
    by_name = {g.name: g for g in gates}
    for name in names:
        assert name in by_name, f"no gate named {name!r}"
        assert by_name[name].runtime in (Runtime.PYTHON_NODE, Runtime.NODE), (
            f"gate {name!r} needs Node but runs on {by_name[name].runtime}"
        )


#: Wildcard grants CDK Pipelines itself puts on the self-mutation role (it must describe the
#: pipeline stack it deploys and list the bootstrap bucket). They are the framework's, not ours;
#: any OTHER wildcard still fails. Measured on a synthesized pipeline: exactly these two.
CDK_INTERNAL_WILDCARDS = frozenset({"cloudformation:DescribeStacks", "s3:ListBucket"})


def assert_no_wildcard_resources(
    template: Template, allowed_actions: Iterable[str] = ()
) -> None:
    """No IAM statement grants on Resource '*' except for actions that only support it."""
    allow = set(allowed_actions)
    for logical_id, pol in template.find_resources("AWS::IAM::Policy").items():
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Resource") != "*":
                continue
            actions = (
                stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            )
            assert all(a in allow for a in actions), (
                f"{logical_id}: wildcard resource for {actions}"
            )


# --- the whole contract -------------------------------------------------------------


def assert_standard_contract(
    template: Template,
    *,
    repo: str,
    gates: Sequence[Gate],
    deploy_stage: str = "Deploy",
    deploy_enabled: bool = False,
    pull_request_checks: bool = True,
    base_branch: str = "main",
    node_gates: Iterable[str] = (),
) -> None:
    """Every invariant a StandardPipeline must satisfy. Repo-specific checks go alongside.

    ``repo`` is ``owner/name``; ``node_gates`` names the gates that run the CDK CLI or import
    aws_cdk and therefore need Node."""
    name = repo.partition("/")[2]
    assert name, f"repo must be 'owner/name', got {repo!r}"
    assert_lambda_compute(template)
    assert_ci_env_on_repo_command_projects(template)
    assert_stage_order(template, deploy_stage)
    assert_gates_present(template, gates)
    assert_deploy_ordering(template, deploy_stage)
    if deploy_enabled:
        assert_deploy_open(template)
    else:
        assert_deploy_closed(template, deploy_stage)
    assert_no_pipeline_triggers(template)
    assert_restart_on_update(template)
    assert_artifact_bucket(template, name)
    assert_source(template, repo, base_branch)
    if pull_request_checks:
        assert_pr_checks(template, gates, name, base_branch)
        assert_pr_roles_least_privilege(template, name)
    else:
        assert not pr_projects(template, name), (
            "PR-check projects exist but pull_request_checks is off"
        )
    assert_gates_have_node(gates, node_gates)


__all__ = [
    "CDK_INTERNAL_WILDCARDS",
    "assert_artifact_bucket",
    "assert_ci_env_on_repo_command_projects",
    "assert_deploy_closed",
    "assert_deploy_open",
    "assert_deploy_ordering",
    "assert_gates_have_node",
    "assert_gates_present",
    "assert_lambda_compute",
    "assert_no_pipeline_triggers",
    "assert_no_wildcard_resources",
    "assert_pr_checks",
    "assert_pr_roles_least_privilege",
    "assert_restart_on_update",
    "assert_source",
    "assert_stage_order",
    "assert_standard_contract",
    "pipeline",
    "pr_projects",
    "projects",
]
