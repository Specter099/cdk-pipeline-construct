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

import json
import re
from collections.abc import Iterable, Sequence

from aws_cdk import aws_codebuild as codebuild
from aws_cdk.assertions import Template

from .compute import EC2_IMAGE
from .gate import (
    Assembly,
    DeployStep,
    Gate,
    mentions_cdk_cli,
    names_path,
    owns_deploy_flag,
    snapshot_commands,
    split_deploy_command,
)
from .pipeline import artifact_bucket_purpose
from .runtimes import (
    NODE_LINUX_X64_SHA256,
    PYTHON_VERSION,
    Runtime,
    cdk_cli_install,
    ec2_python_select,
    node_tarball_prelude,
)

#: The exact actions a PR-check role may hold (measured on a synthesized pipeline): its own
#: logs, the report permissions CodeBuild adds for the project's own report group, and the
#: connection token. Anything else, including a wider action under an allowed prefix, fails.
_PR_ROLE_ALLOWED_ACTIONS = frozenset(
    {
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "codebuild:CreateReportGroup",
        "codebuild:CreateReport",
        "codebuild:UpdateReport",
        "codebuild:BatchPutTestCases",
        "codebuild:BatchPutCodeCoverages",
        "codeconnections:GetConnectionToken",
        "codeconnections:GetConnection",
    }
)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _is_wildcard_resource(resource) -> bool:
    """'*' itself, or an ARN whose resource field is exactly '*' (for example arn:aws:s3:::*).
    An intrinsic Fn::Join is flattened with '_' standing in for its non-literal parts."""
    if isinstance(resource, dict):
        join = resource.get("Fn::Join")
        if not join:
            return False
        resource = join[0].join(p if isinstance(p, str) else "_" for p in join[1])
    if not isinstance(resource, str):
        return False
    if resource == "*":
        return True
    parts = resource.split(":", 5)
    return resource.startswith("arn:") and len(parts) == 6 and parts[5] == "*"


def _role_policy_statements(template: Template, role_ids: set[str]) -> list[tuple[str, dict]]:
    """(role id, statement) for every statement that reaches one of ``role_ids``: standalone
    AWS::IAM::Policy resources and the role's own inline policies. A role with managed policies
    attached is itself a failure here, so it never reaches the caller silently."""
    out: list[tuple[str, dict]] = []
    roles = template.find_resources("AWS::IAM::Role")
    for rid in role_ids:
        props = roles[rid]["Properties"]
        assert not props.get("ManagedPolicyArns"), f"{rid} has managed policies attached"
        for inline in props.get("Policies", []):
            out += [(rid, st) for st in _as_list(inline["PolicyDocument"]["Statement"])]
    for pol in template.find_resources("AWS::IAM::Policy").values():
        attached = {r["Ref"] for r in pol["Properties"].get("Roles", []) if "Ref" in r}
        for rid in attached & role_ids:
            out += [(rid, st) for st in _as_list(pol["Properties"]["PolicyDocument"]["Statement"])]
    for rid, st in out:
        assert "NotAction" not in st and "NotResource" not in st, (
            f"{rid} uses NotAction/NotResource: {st}"
        )
    return out


def pipeline(template: Template) -> dict:
    """Properties of the single CodePipeline in ``template``."""
    found = template.find_resources("AWS::CodePipeline::Pipeline")
    assert len(found) == 1, f"expected exactly one pipeline, found {len(found)}"
    return next(iter(found.values()))["Properties"]


def projects(template: Template) -> dict:
    return template.find_resources("AWS::CodeBuild::Project")


def _step_buildspec(template: Template, step_name: str) -> dict:
    for res in projects(template).values():
        if str(res["Properties"].get("Description", "")).endswith(f"/{step_name}"):
            return json.loads(res["Properties"]["Source"]["BuildSpec"])
    raise AssertionError(f"no CodeBuild project for pipeline step {step_name!r}")


def step_commands(template: Template, step_name: str) -> list[str]:
    """The buildspec commands of the CodeBuild project behind pipeline step ``step_name``
    (for example ``"Deploy-app"`` or ``"Gate-tests"``)."""
    return _step_buildspec(template, step_name)["phases"]["build"]["commands"]


_MOUNT = re.compile(r"""ln -s -- "\$CODEBUILD_SRC_DIR_([A-Za-z0-9_]+)" '([^']+)'""")


def step_mounts(template: Template, step_name: str) -> dict[str, str]:
    """{artifact name: directory} for the extra artifacts CDK Pipelines links into a step's
    working directory (``additional_inputs``)."""
    spec = _step_buildspec(template, step_name)
    install = spec.get("phases", {}).get("install", {}).get("commands", [])
    return {m.group(1): m.group(2) for cmd in install for m in _MOUNT.finditer(cmd)}


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


def _project_step_name(props: dict) -> str:
    """A project's pipeline step name ("Gate-x", "Deploy-app") or, for a PR check, its name."""
    desc = str(props.get("Description", ""))
    if desc.startswith("Pipeline step "):
        return desc.rsplit("/", 1)[-1]
    return str(props.get("Name", ""))


def assert_lambda_compute(template: Template, ec2_steps: Iterable[str] = ()) -> None:
    """Every CodeBuild project, including CDK Pipelines' own, runs on Lambda compute, except the
    steps named in ``ec2_steps`` (pipeline step names such as ``Gate-tf-plan`` or PR-check
    project names such as ``my-repo-pr-tf-plan``), which must run on EC2: the pinned standard
    image, never privileged, with an explicit timeout. A step on EC2 that is not declared fails,
    and so does a declared one that is missing or on Lambda."""
    found = projects(template)
    assert found, "no CodeBuild projects in the template"
    declared = set(ec2_steps)
    seen: list[str] = []
    for logical_id, res in found.items():
        props = res["Properties"]
        env = props["Environment"]
        step = _project_step_name(props)
        assert not env.get("PrivilegedMode"), f"{logical_id} is privileged"
        for key in ("VpcConfig", "FileSystemLocations"):
            assert key not in props, f"{logical_id} sets {key}"
        # NO_CACHE is CloudFormation's explicit default; anything else is real caching.
        assert props.get("Cache", {"Type": "NO_CACHE"}) == {"Type": "NO_CACHE"}, (
            f"{logical_id} enables caching"
        )
        if step in declared:
            seen.append(step)
            assert env["Type"] == "LINUX_CONTAINER", f"{step} is declared EC2 but is not on EC2"
            assert env["Image"] == EC2_IMAGE.image_id, (
                f"{step} runs on {env['Image']}, not the pinned EC2 image {EC2_IMAGE.image_id}"
            )
            assert 5 <= int(props.get("TimeoutInMinutes", 0)) <= 480, (
                f"{step} on EC2 has no explicit timeout"
            )
            assert env["ComputeType"].startswith("BUILD_GENERAL1_"), f"{step} on EC2 has a Lambda size"
            # The image's own Python is not 3.12: a venv must come from the pinned selection.
            cmds = json.loads(props["Source"]["BuildSpec"])["phases"]["build"]["commands"]
            venvs = [i for i, x in enumerate(cmds) if " -m venv " in x]
            select = ec2_python_select()
            for i in venvs:
                assert cmds[i].startswith(f"python{PYTHON_VERSION} -m venv") and any(
                    cmds[j : j + len(select)] == select for j in range(i)
                ), f"{step} on EC2 builds a venv from the image's Python: {cmds[i]!r}"
            continue
        assert env["Type"] == "LINUX_LAMBDA_CONTAINER", (
            f"{logical_id} ({step}) is not on Lambda compute and is not declared in ec2_steps"
        )
        for key in ("TimeoutInMinutes", "QueuedTimeoutInMinutes"):
            assert key not in props, (
                f"{logical_id} sets {key}, which Lambda compute does not support"
            )
    missing = declared - set(seen)
    assert not missing, f"no CodeBuild project for EC2 steps {sorted(missing)}"
    twice = sorted({n for n in seen if seen.count(n) > 1})
    assert not twice, f"EC2 step names {twice} match more than one project; they are ambiguous"


def assert_ci_env_on_repo_command_projects(template: Template) -> None:
    """CI=true is set wherever repo commands run (scripts then use the ambient interpreter)."""
    for logical_id, res in projects(template).items():
        if "SelfMutation" in logical_id or "FileAsset" in logical_id:
            continue  # CDK-managed: `cdk deploy` of the pipeline stack / asset publishing
        env = {
            v["Name"]: v["Value"]
            for v in res["Properties"]["Environment"].get("EnvironmentVariables", [])
        }
        assert env.get("CI") == "true", f"{logical_id} does not set CI=true"


def assert_stage_order(template: Template, deploy_stage: str = "Deploy") -> None:
    names = [s["Name"] for s in pipeline(template)["Stages"]]
    # CDK Pipelines adds an "Assets" stage between UpdatePipeline and the rest when a stage-deployed
    # application has file or Docker assets; it is the framework's and optional.
    if "Assets" in names:
        assert names.index("Assets") == 3, f"Assets in the wrong place: {names}"
        names.remove("Assets")
    assert names == ["Source", "Build", "UpdatePipeline", "Gates", deploy_stage], (
        f"unexpected stage order: {names}"
    )


def _assembly_actions(template: Template) -> list[dict]:
    """Actions in the Gates stage that produce an artifact: the build-once assembly step."""
    return [a for a in _stage(template, "Gates")["Actions"] if a.get("OutputArtifacts")]


def assert_gates_present(template: Template, gates: Sequence[Gate]) -> None:
    """Exactly these gates (plus at most one assembly step), in parallel: every gate that does
    not read the assembly shares one RunOrder, and so does every gate that does."""
    actions = _stage(template, "Gates")["Actions"]
    producers = _assembly_actions(template)
    assert len(producers) <= 1, f"more than one step produces an artifact in Gates: {producers}"
    by_name = {a["Name"]: a for a in actions if a not in producers}
    expected = {f"Gate-{g.name}": g for g in gates}
    assert set(by_name) == set(expected), (
        f"gates differ: missing={sorted(set(expected) - set(by_name))} "
        f"extra={sorted(set(by_name) - set(expected))}"
    )
    for reads in (False, True):
        orders = {
            by_name[n]["RunOrder"] for n, g in expected.items() if g.uses_assembly is reads
        }
        assert len(orders) <= 1, "gates should run in parallel"


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
    require_approval: bool = True,
) -> None:
    """Approval strictly precedes every other action, deploy strictly precedes post-deploy steps.

    Equal RunOrder means the actions run in parallel: in a wave with no stacks the approval
    and the deploy once shared a RunOrder, i.e. the deploy did not wait for approval. Every
    non-approval action is held to this, not only the ones recognised by name, so an extra
    action cannot slip in beside the approval."""
    actions = _stage(template, deploy_stage)["Actions"]
    approvals = [a for a in actions if a["ActionTypeId"]["Category"] == "Approval"]
    if require_approval:
        assert len(approvals) == 1, (
            f"expected exactly one approval in {deploy_stage!r}, found {len(approvals)}"
        )
        approval = approvals[0]["RunOrder"]
        for a in actions:
            if a in approvals:
                continue
            assert approval < a["RunOrder"], (
                f"{a['Name']} (RunOrder {a['RunOrder']}) does not wait for approval ({approval})"
            )
    else:
        assert not approvals, f"{deploy_stage!r} has an approval but require_approval is False"
    deploys = [
        a
        for a in actions
        if a["Name"].startswith(deploy_prefix)
        or a["ActionTypeId"]["Provider"] == "CloudFormation"
    ]
    assert deploys, f"no deploy action in {deploy_stage!r}"
    posts = [a for a in actions if a["Name"].startswith(post_prefix)]
    for a in posts:
        assert max(d["RunOrder"] for d in deploys) < a["RunOrder"], (
            f"{a['Name']} does not wait for the deploy"
        )


def _project_of(template: Template, step_name: str) -> dict:
    for res in projects(template).values():
        if str(res["Properties"].get("Description", "")).endswith(f"/{step_name}"):
            return res["Properties"]
    raise AssertionError(f"no CodeBuild project for pipeline step {step_name!r}")


def _assert_construct_deploy(template: Template, action: dict) -> None:
    """The deploy action runs exactly: snapshot the assembly, install the pinned CLI, deploy the
    snapshot with that CLI by absolute path from an empty directory. Nothing else: no other
    buildspec key, phase or phase setting, no environment beyond CI=true, the Node Lambda image
    with a Lambda size or the pinned EC2 image with an EC2 size."""
    step = action["Name"]
    assert not action.get("Configuration", {}).get("EnvironmentVariables"), (
        f"{step} sets action-level environment variables"
    )
    props = _project_of(template, step)
    env = props["Environment"]
    on_ec2 = env["Type"] == "LINUX_CONTAINER"
    if on_ec2:
        assert env["Image"] == EC2_IMAGE.image_id and env["ComputeType"].startswith("BUILD_GENERAL1_"), (
            f"{step} is on EC2 but not on the pinned EC2 image and an EC2 size: {env}"
        )
    else:
        assert env["Type"] == "LINUX_LAMBDA_CONTAINER" and env["ComputeType"].startswith("BUILD_LAMBDA_"), (
            f"{step} is neither on Lambda nor on EC2: {env['Type']}"
        )
        assert env["Image"] == codebuild.LinuxLambdaBuildImage.AMAZON_LINUX_2023_NODE_22.image_id, (
            f"{step} on Lambda does not run on the Node Lambda image: {env['Image']}"
        )
    variables = {
        (v["Name"], v.get("Type", "PLAINTEXT"), v.get("Value"))
        for v in env.get("EnvironmentVariables", [])
    }
    assert variables == {("CI", "PLAINTEXT", "true")}, (
        f"{step} has environment variables besides a plaintext CI=true: {variables}"
    )
    spec = json.loads(props["Source"]["BuildSpec"])
    assert set(spec) <= {"version", "phases"}, f"{step} buildspec has extra keys: {sorted(spec)}"
    assert set(spec.get("phases", {})) == {"build"}, (
        f"{step} has phases besides build: {sorted(spec.get('phases', {}))}"
    )
    assert set(spec["phases"]["build"]) == {"commands"}, (
        f"{step} build phase has settings besides commands: {sorted(spec['phases']['build'])}"
    )
    cmds = spec["phases"]["build"]["commands"]
    versions = [
        m.group(1)
        for c in cmds
        if (m := re.fullmatch(r"npm install -g --prefix \S+ aws-cdk@(\d+\.\d+\.\d+)", c))
    ]
    assert len(versions) == 1, f"{step} does not install a pinned CDK CLI version exactly once"
    node: list[str] = []
    if on_ec2:  # the pinned Node tarball runs the CLI, not the image's Node
        heads = [node_tarball_prelude(v) for v in NODE_LINUX_X64_SHA256]
        node = next((h for h in heads if cmds[: len(h)] == h), [])
        assert node, f"{step} on EC2 does not install the pinned, hash-checked Node first"
    expected_head = [*node, *snapshot_commands("."), *cdk_cli_install(versions[0])]
    assert cmds[:-1] == expected_head, (
        f"{step} runs commands besides the snapshot, the CLI install and the deploy: {cmds[:-1]}"
    )
    tokens = split_deploy_command(cmds[-1])
    assert tokens is not None, (
        f"{step} does not deploy the assembly snapshot with the pinned CLI: {cmds[-1]!r}"
    )
    assert not any(owns_deploy_flag(t) for t in tokens[4:]), (
        f"{step} deploy overrides --app, --build or --output: {cmds[-1]!r}"
    )


def assert_builds_once(
    template: Template, gates: Sequence[Gate], deploy_stage: str = "Deploy"
) -> None:
    """The templates the gates inspect are the templates that are deployed.

    ``deploy_step`` pipelines: one assembly step in Gates; every gate with
    ``uses_assembly=True`` read its artifact and name the directory it is linked at in their
    commands; the
    deploy action reads only that artifact and runs exactly the construct's snapshot, pinned CLI
    install and deploy; any ``PreDeploy-`` action runs before it and never uses the CDK CLI.
    ``application_stage`` pipelines: those gates read the Synth output, which is what the
    stage's CloudFormation actions deploy."""
    actions = {a["Name"]: a for a in _stage(template, "Gates")["Actions"]}
    producers = _assembly_actions(template)
    deploy_actions = _stage(template, deploy_stage)["Actions"]
    cfn = [a for a in deploy_actions if a["ActionTypeId"]["Provider"] == "CloudFormation"]

    if cfn:
        assert not producers, "a stage-deployed pipeline must not build a second assembly"
        deployed = {i["Name"] for a in cfn for i in a.get("InputArtifacts", [])}
        assert len(deployed) == 1, f"stage deploys from {sorted(deployed)}, expected one assembly"
        artifact = deployed.pop()
    else:
        assert len(producers) == 1, (
            "no assembly step in Gates: the deploy would synthesize its own templates"
        )
        artifact = producers[0]["OutputArtifacts"][0]["Name"]
        (deploy,) = [a for a in deploy_actions if a["Name"].startswith("Deploy-")]
        reads = [i["Name"] for i in deploy.get("InputArtifacts", [])]
        assert artifact in reads, f"{deploy['Name']} does not read the assembly {artifact!r}"
        assert reads == [artifact], (
            f"{deploy['Name']} reads {reads}: the deploy must read the assembly and nothing else"
        )
        _assert_construct_deploy(template, deploy)
        for a in deploy_actions:
            if a["Name"].startswith("PreDeploy-"):
                assert a["RunOrder"] < deploy["RunOrder"], f"{a['Name']} does not finish before the deploy"
                for cmd in step_commands(template, a["Name"]):
                    assert not mentions_cdk_cli(cmd), f"{a['Name']} uses the CDK CLI: {cmd!r}"
        for g in gates:
            if g.uses_assembly:
                assert actions[f"Gate-{g.name}"]["RunOrder"] > producers[0]["RunOrder"], (
                    f"Gate-{g.name} does not wait for the assembly"
                )

    for g in gates:
        reads = {i["Name"] for i in actions[f"Gate-{g.name}"].get("InputArtifacts", [])}
        if g.uses_assembly:
            assert artifact in reads, f"Gate-{g.name} does not read the deployed assembly {artifact!r}"
            mount = step_mounts(template, f"Gate-{g.name}").get(artifact)
            assert mount, f"Gate-{g.name} does not link the assembly into its directory"
            assert any(names_path(cmd, mount) for cmd in g.commands), (
                f"Gate-{g.name} never refers to {mount!r}, where the assembly is linked: it may "
                "be reading some other directory"
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
    """PR-check projects run PR-head code: logs, the connection token and report permissions only.

    Exact actions, no wildcard resources, no managed or inline policies on the role, and at least
    one PR-check role must exist (a wrong prefix would otherwise pass vacuously)."""
    found = pr_projects(template, name_prefix)
    assert found, f"no PR-check projects with prefix {name_prefix!r}"
    roles = {res["Properties"]["ServiceRole"]["Fn::GetAtt"][0] for res in found.values()}
    for rid, stmt in _role_policy_statements(template, roles):
        assert stmt.get("Effect", "Allow") == "Allow", f"{rid}: unexpected effect {stmt}"
        for action in _as_list(stmt["Action"]):
            assert action in _PR_ROLE_ALLOWED_ACTIONS, f"PR-check role has {action}"
        for resource in _as_list(stmt["Resource"]):
            assert not _is_wildcard_resource(resource), (
                f"PR-check role has a wildcard resource: {resource}"
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
    """No IAM statement grants on a wildcard resource except for actions that only support it.

    A wildcard is '*' anywhere in the Resource list (not only as the sole value) or an ARN whose
    resource part is '*', and role-inline policies are scanned as well as AWS::IAM::Policy. The
    two CDK-internal wildcards are honoured only on the self-mutation role."""
    allow = set(allowed_actions)
    roles = template.find_resources("AWS::IAM::Role")
    statements: list[tuple[str, dict]] = []
    for lid, role in roles.items():
        for inline in role["Properties"].get("Policies", []):
            statements += [(lid, st) for st in _as_list(inline["PolicyDocument"]["Statement"])]
    for lid, pol in template.find_resources("AWS::IAM::Policy").items():
        attached = [r["Ref"] for r in pol["Properties"].get("Roles", []) if "Ref" in r]
        owner = attached[0] if attached else lid
        statements += [(owner, st) for st in _as_list(pol["Properties"]["PolicyDocument"]["Statement"])]
    for owner, stmt in statements:
        assert "NotAction" not in stmt and "NotResource" not in stmt, (
            f"{owner}: NotAction/NotResource is not allowed: {stmt}"
        )
        if not any(_is_wildcard_resource(r) for r in _as_list(stmt["Resource"])):
            continue
        for action in _as_list(stmt["Action"]):
            permitted = action in allow or (
                action in CDK_INTERNAL_WILDCARDS and "SelfMutat" in owner
            )
            assert permitted, f"{owner}: wildcard resource for {action}"


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
    require_approval: bool = True,
    builds_once: bool | None = None,
    ec2_steps: Iterable[str] = (),
    assembly: Assembly | None = None,
    deploy_step: DeployStep | None = None,
) -> None:
    """Every invariant a StandardPipeline must satisfy. Repo-specific checks go alongside.

    ``repo`` is ``owner/name``; ``node_gates`` names the gates that run the CDK CLI or import
    aws_cdk and therefore need Node. ``builds_once`` (default: on when the pipeline has an
    assembly step or any gate reads the assembly) adds ``assert_builds_once``.

    EC2 compute is inferred from the steps: gates with ``ec2=`` (and their PR checks), and, when
    passed, an ``assembly`` with ``ec2=`` (``Assemble`` and the PR checks of ``uses_assembly``
    gates, which synthesize first) and a ``deploy_step`` with ``ec2=`` (``Deploy-<name>`` and
    ``PreDeploy-<name>``). ``ec2_steps`` names any other EC2 project."""
    name = repo.partition("/")[2]
    assert name, f"repo must be 'owner/name', got {repo!r}"
    expected_ec2 = set(ec2_steps)
    assembly_on_ec2 = assembly is not None and assembly.ec2 is not None
    if assembly_on_ec2:
        expected_ec2.add(assembly.name)
    for g in gates:
        if g.ec2 is not None:
            expected_ec2.add(f"Gate-{g.name}")
        pr_on_ec2 = g.ec2 is not None or (g.uses_assembly and assembly_on_ec2)
        if pr_on_ec2 and pull_request_checks and g.runs_on_pull_requests:
            expected_ec2.add(f"{name}-pr-{g.name}")
    if deploy_step is not None and deploy_step.ec2 is not None:
        expected_ec2.add(f"Deploy-{deploy_step.name}")
        pre = f"PreDeploy-{deploy_step.name}"
        if any(_project_step_name(r["Properties"]) == pre for r in projects(template).values()):
            expected_ec2.add(pre)
    assert_lambda_compute(template, expected_ec2)
    assert_ci_env_on_repo_command_projects(template)
    assert_stage_order(template, deploy_stage)
    assert_gates_present(template, gates)
    assert_deploy_ordering(template, deploy_stage, require_approval=require_approval)
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
    if builds_once is None:
        builds_once = bool(_assembly_actions(template)) or any(g.uses_assembly for g in gates)
    if builds_once:
        assert_builds_once(template, gates, deploy_stage)


__all__ = [
    "CDK_INTERNAL_WILDCARDS",
    "assert_artifact_bucket",
    "assert_builds_once",
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
    "step_commands",
    "step_mounts",
]
