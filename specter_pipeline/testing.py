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

import fnmatch
import json
import re
from collections.abc import Iterable, Mapping, Sequence

from aws_cdk import aws_codebuild as codebuild
from aws_cdk.assertions import Template

from .compute import EC2_IMAGE
from .gate import (
    Assembly,
    DeployStep,
    Gate,
    PostDeployStep,
    mentions_cdk_cli,
    names_path,
    owns_deploy_flag,
    snapshot_commands,
    split_deploy_command,
)
from .pipeline import artifact_bucket_purpose
from .runtimes import (
    NODE_LINUX_X64_SHA256,
    TERRAFORM_LINUX_SHA256,
    Runtime,
    cdk_cli_install,
    node_tarball_prelude,
    prelude,
    python_prelude,
    terraform_prelude,
)
from .terraform import CHECKOV as TF_CHECKOV
from .terraform import TerraformRoot, apply_commands, plan_check_commands, plan_commands

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


def _read_buildspec(props: dict) -> dict | None:
    """A project's inline buildspec as a dict, or None if it has none we can read. A buildspec
    holding CDK tokens is an Fn::Join; its non-literal parts read as TOKEN."""
    source = props.get("Source")
    spec = source.get("BuildSpec") if isinstance(source, dict) else None
    try:
        if isinstance(spec, dict) and "Fn::Join" in spec:
            sep, parts = spec["Fn::Join"]
            spec = sep.join(p if isinstance(p, str) else "TOKEN" for p in parts)
        if not isinstance(spec, str):
            return None
        parsed = json.loads(spec)
    except (ValueError, TypeError, AttributeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _step_buildspec(template: Template, step_name: str) -> dict:
    for res in projects(template).values():
        if str(res["Properties"].get("Description", "")).endswith(f"/{step_name}"):
            spec = _read_buildspec(res["Properties"])
            assert spec is not None, f"cannot read the buildspec of {step_name!r}"
            return spec
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


def _construct_generated(props: dict, pr_prefix: str | None = None) -> bool:
    """CDK Pipelines' step projects (by description) and the PR checks (by their
    ``{repo}-pr-`` name, which a reworded description cannot hide)."""
    if str(props.get("Description", "")).startswith("Pipeline step "):
        return True
    if pr_prefix is not None:
        return str(props.get("Name", "")).startswith(f"{pr_prefix}-pr-")
    return str(props.get("Description", "")).startswith("PR check ")


def _buildspec_commands(props: dict) -> list[str] | None:
    """The build-phase commands of a project's inline buildspec, or None."""
    spec = _read_buildspec(props)
    try:
        cmds = spec["phases"]["build"]["commands"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    return cmds if isinstance(cmds, list) else None


def _ec2_preludes(
    runtimes: Iterable[Runtime] = tuple(Runtime), *, ec2: bool = True
) -> list[list[str]]:
    return [
        prelude(rt, v, tf, ec2=ec2)
        for rt in runtimes
        for v in NODE_LINUX_X64_SHA256
        for tf in TERRAFORM_LINUX_SHA256
    ]


#: Toolchain set-up that must only appear in a prelude, never after it. (A step's own venv for
#: its tools is fine; selecting another interpreter or fetching another runtime is not.)
_TOOLCHAIN_MARKERS = ("PYENV_VERSION", "/tmp/node.tgz", "/tmp/terraform.zip")


def _assert_ec2_toolchain(step: str, cmds: list[str], runtime: Runtime | None) -> None:
    """Known runtime: exactly that runtime's EC2 prelude first. Unknown: the longest matching
    prelude first, and no toolchain set-up after it (so half of a two-runtime prelude cannot
    pass as a one-runtime one with the rest done by hand)."""
    if runtime is not None:
        assert any(cmds[: len(p)] == p for p in _ec2_preludes([runtime])), (
            f"{step} on EC2 does not start with the pinned {runtime.name} toolchain set-up: {cmds[:3]}"
        )
        return
    matches = [p for p in _ec2_preludes() if cmds[: len(p)] == p]
    assert matches, f"{step} on EC2 does not start with the pinned toolchain set-up: {cmds[:3]}"
    rest = cmds[max(len(p) for p in matches):]
    stray = [c for c in rest if any(m in c for m in _TOOLCHAIN_MARKERS)]
    assert not stray, f"{step} on EC2 sets up a toolchain outside the pinned prelude: {stray}"


def assert_lambda_compute(
    template: Template,
    ec2_steps: Iterable[str] = (),
    runtimes: Mapping[str, Runtime] | None = None,
    pr_prefix: str | None = None,
) -> None:
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
            # A project the construct generated must start with one of its own EC2 toolchain
            # set-ups (pinned Node; Python 3.12 selected through pyenv): never the image's own.
            if _construct_generated(props, pr_prefix):
                cmds = _buildspec_commands(props)
                assert cmds is not None, f"{step}: cannot read its buildspec commands"
                _assert_ec2_toolchain(step, cmds, (runtimes or {}).get(step))
            continue
        assert env["Type"] == "LINUX_LAMBDA_CONTAINER", (
            f"{logical_id} ({step}) is not on Lambda compute and is not declared in ec2_steps"
        )
        for key in ("TimeoutInMinutes", "QueuedTimeoutInMinutes"):
            assert key not in props, (
                f"{logical_id} sets {key}, which Lambda compute does not support"
            )
        # A step whose runtime is known gets exactly that runtime's pinned set-up on Lambda too
        # (the Lambda images carry no Terraform, and only Python or Node).
        runtime = (runtimes or {}).get(step)
        if runtime is not None and _construct_generated(props, pr_prefix):
            cmds = _buildspec_commands(props)
            assert cmds is not None, f"{step}: cannot read its buildspec commands"
            assert any(cmds[: len(p)] == p for p in _ec2_preludes([runtime], ec2=False)), (
                f"{step} on Lambda does not start with the pinned {runtime.name} toolchain set-up: {cmds[:3]}"
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
    spec = _read_buildspec(props)
    assert spec is not None, f"{step}: cannot read its buildspec"
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


def _role_of(template: Template, step: str) -> str:
    return _project_of(template, step)["ServiceRole"]["Fn::GetAtt"][0]


_PSEUDO = ("AWS::Partition", "AWS::Region", "AWS::AccountId", "AWS::URLSuffix")


class _Ctx:
    """What the IAM matcher knows about the template: its buckets (whose Ref/GetAtt stay
    placeholders) and its parameters (replaced by their default, or ``*`` without one)."""

    def __init__(self, template: Template) -> None:
        doc = template.to_json()
        self.distinct = frozenset(template.find_resources("AWS::S3::Bucket"))
        self.params = {
            k: (v.get("Default") if isinstance(v, dict) and isinstance(v.get("Default"), str) else "*")
            for k, v in doc.get("Parameters", {}).items()
        }


def _sub_text(body: str, variables: dict, ctx: _Ctx) -> str:
    def one(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("!"):
            return "${" + name[1:] + "}"
        if name in variables:
            return _resource_text(variables[name], ctx)
        lid, _, attr = name.partition(".")
        if lid in ctx.distinct:
            return f"<{lid}.{attr}>" if attr else f"<{lid}>"
        if not attr and lid in ctx.params:
            return ctx.params[lid]
        return "*"

    return re.sub(r"\$\{([^}]*)\}", one, body)


def _resource_text(resource, ctx: _Ctx | None = None) -> str:
    """An IAM resource as a pattern. A Ref/GetAtt of a known bucket and an ``Fn::ImportValue``
    (another stack's export, never this stack's state bucket) stay ``<...>`` placeholders that
    match only themselves; a template parameter becomes its default (``*`` without one);
    ``Fn::Join`` and ``Fn::Sub`` are rendered; pseudo parameters and any other intrinsic become
    ``*``, so an unrecognised construction fails closed (counts as a match)."""
    ctx = ctx or _EMPTY_CTX
    if isinstance(resource, str):
        return resource
    if isinstance(resource, dict) and len(resource) == 1:
        if "Fn::Join" in resource:
            sep, parts = resource["Fn::Join"]
            if isinstance(sep, str) and isinstance(parts, list):
                return sep.join(_resource_text(p, ctx) for p in parts)
        if "Fn::Sub" in resource:
            sub = resource["Fn::Sub"]
            if isinstance(sub, str):
                return _sub_text(sub, {}, ctx)
            if isinstance(sub, list) and len(sub) == 2 and isinstance(sub[0], str) and isinstance(sub[1], dict):
                return _sub_text(sub[0], sub[1], ctx)
        if "Fn::GetAtt" in resource and resource["Fn::GetAtt"][0] in ctx.distinct:
            return "<" + ".".join(resource["Fn::GetAtt"]) + ">"
        if "Ref" in resource and resource["Ref"] in ctx.distinct:
            return "<" + resource["Ref"] + ">"
        if "Ref" in resource and resource["Ref"] in ctx.params:
            return ctx.params[resource["Ref"]]
        if "Fn::ImportValue" in resource:
            return "<import:" + json.dumps(resource["Fn::ImportValue"], sort_keys=True) + ">"
    return "*"


class _EmptyCtx(_Ctx):
    def __init__(self) -> None:
        self.distinct, self.params = frozenset(), {}


_EMPTY_CTX = _EmptyCtx()


#: In a target form: any run of characters within one ARN segment (no ":" or "/").
_SEG = "\x01"


def _globs_meet(a: str, b: str) -> bool:
    """Whether some string matches both patterns: ``*`` any run, ``?`` one character, and
    ``_SEG`` any run without ":" or "/" (IAM patterns use the first two; target forms all three)."""

    def can(tok: str, c: str) -> bool:  # whether a non-star token can produce character c
        return tok in ("?", c)

    seen: set[tuple[int, int]] = set()
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == len(a) and j == len(b):
            return True
        sa = i < len(a) and a[i] in ("*", _SEG)
        sb = j < len(b) and b[j] in ("*", _SEG)
        if sa:
            stack.append((i + 1, j))  # a's run ends
        if sb:
            stack.append((i, j + 1))  # b's run ends
        if i == len(a) or j == len(b):
            continue
        # produce one character both sides accept
        lits = [t for t in (a[i], b[j]) if t not in ("*", _SEG, "?")]
        c = lits[0] if lits else "x"
        ok_a = (a[i] == "*") or (a[i] == _SEG and c not in ":/") or can(a[i], c)
        ok_b = (b[j] == "*") or (b[j] == _SEG and c not in ":/") or can(b[j], c)
        if ok_a and ok_b:
            stack.append((i if sa else i + 1, j if sb else j + 1))
    return False


def _covers(pattern: str, forms: list[str]) -> bool:
    """Whether ``pattern`` surely matches the resource (any of its forms, read literally)."""
    return any(fnmatch.fnmatchcase(f, pattern) for f in forms)


def _matches_action(action: str, values) -> bool:
    # an intrinsic in place of an action name is unknown: it may be anything
    return any(not isinstance(v, str) or fnmatch.fnmatchcase(action, v.lower()) for v in _as_list(values))


def _statement_allows(stmt, action: str, forms: list[str], ctx: _Ctx) -> bool:
    """Whether one statement allows ``action`` on the resource with these ``forms``, as IAM
    would: actions case-insensitive, wildcards, NotAction/NotResource honoured. Conditions are
    ignored (they may hold). Anything unreadable (an ``Fn::If``, a non-string effect) allows."""
    if not isinstance(stmt, dict) or any(k.startswith("Fn::") for k in stmt):
        return True
    effect = stmt.get("Effect", "Allow")
    if effect == "Deny":
        return False
    if effect != "Allow" and isinstance(effect, str):
        return False
    a = action.lower()
    if "NotAction" in stmt:
        if _matches_action(a, stmt["NotAction"]) and all(isinstance(v, str) for v in _as_list(stmt["NotAction"])):
            return False
    elif not _matches_action(a, stmt.get("Action", [])):
        return False
    if "NotResource" in stmt:
        # excluded only if an exclusion surely covers the resource
        return not any(_covers(_resource_text(r, ctx), forms) for r in _as_list(stmt["NotResource"])
                       if isinstance(r, str | dict))
    return any(_globs_meet(f, _resource_text(r, ctx)) for r in _as_list(stmt.get("Resource", [])) for f in forms)


def _statement_denies(stmt, action: str, forms: list[str], ctx: _Ctx) -> bool:
    """Whether an unconditional Deny surely covers ``action`` on the resource."""
    if not isinstance(stmt, dict) or stmt.get("Effect") != "Deny" or stmt.get("Condition"):
        return False
    if "NotAction" in stmt or "NotResource" in stmt:
        return False
    actions = _as_list(stmt.get("Action", []))
    if not all(isinstance(v, str) for v in actions) or not _matches_action(action.lower(), actions):
        return False
    return any(isinstance(r, str | dict) and _covers(_resource_text(r, ctx), forms)
               for r in _as_list(stmt.get("Resource", [])))


def _allows(stmts: list, action: str, resources: list[list[str]], ctx: _Ctx) -> bool:
    """Whether these statements allow ``action`` on any of ``resources`` (each a list of forms
    of one real resource) and no unconditional Deny among them takes it back."""
    return any(
        any(_statement_allows(s, action, forms, ctx) for s in stmts)
        and not any(_statement_denies(s, action, forms, ctx) for s in stmts)
        for forms in resources
    )


def _resources(targets) -> list[list[str]]:
    if isinstance(targets, str):
        return [[targets]]
    return [[t] if isinstance(t, str) else list(t) for t in targets]


def _grants(template: Template, role: str, action: str, targets) -> bool:
    """Whether a construct role (strict: no managed policies, no NotAction/NotResource) is
    allowed ``action`` on any of ``targets`` (a resource, or a list of resources each given as a
    list of its forms)."""
    stmts = [st for _, st in _role_policy_statements(template, {role})]
    return _allows(stmts, action, _resources(targets), _Ctx(template))


_PRINCIPALS = {"Roles": ("AWS::IAM::Role", "RoleName"), "Users": ("AWS::IAM::User", "UserName"),
               "Groups": ("AWS::IAM::Group", "GroupName")}
_SINGLE = {"AWS::IAM::RolePolicy": ("Roles", "RoleName"), "AWS::IAM::UserPolicy": ("Users", "UserName"),
           "AWS::IAM::GroupPolicy": ("Groups", "GroupName")}


def _doc_statements(doc) -> list:
    """A policy document's statements; an unreadable document is one unknown statement."""
    if not isinstance(doc, dict):
        return [None]
    stmts = doc.get("Statement")
    return [None] if stmts is None or not isinstance(stmts, dict | list) else _as_list(stmts)


def _policy_principals(template: Template) -> list[tuple[str, list]]:
    """(principal, statements) for every principal any policy in the template reaches. A
    principal is a role, user or group of the template, or - when a policy's attachment cannot
    be tied to one (a literal name from elsewhere, an intrinsic) - the policy itself."""
    by_name: dict[tuple[str, str], str] = {}
    found: dict[str, list] = {}
    for kind, (cfn, name_prop) in _PRINCIPALS.items():
        for pid, res in template.find_resources(cfn).items():
            props = res.get("Properties", {}) or {}
            found[pid] = []
            if isinstance(props.get(name_prop), str):
                by_name[(kind, props[name_prop])] = pid
            for inline in _as_list(props.get("Policies", [])):
                found[pid] += _doc_statements(inline.get("PolicyDocument") if isinstance(inline, dict) else None)

    def targets(kind: str, ref) -> list[str] | None:
        """The principals an attachment names, or None if it cannot be resolved."""
        if isinstance(ref, str):
            return [by_name[(kind, ref)]] if (kind, ref) in by_name else None
        if isinstance(ref, dict) and set(ref) == {"Ref"} and ref["Ref"] in found:
            return [ref["Ref"]]
        if isinstance(ref, dict) and set(ref) == {"Fn::Sub"} and isinstance(ref["Fn::Sub"], str):
            m = re.fullmatch(r"\$\{([A-Za-z0-9]+)\}", ref["Fn::Sub"])
            if m and m.group(1) in found:
                return [m.group(1)]
        return None

    def attach(lid: str, kind: str, refs, stmts: list) -> None:
        for ref in _as_list(refs):
            hit = targets(kind, ref)
            if hit is None:
                found.setdefault(lid, []).extend(stmts)  # reaches someone we cannot name
            else:
                for pid in hit:
                    found[pid].extend(stmts)

    managed: dict[str, list] = {}
    for cfn in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for lid, res in template.find_resources(cfn).items():
            props = res.get("Properties", {}) or {}
            stmts = _doc_statements(props.get("PolicyDocument"))
            if cfn == "AWS::IAM::ManagedPolicy":
                managed[lid] = stmts
            for kind in _PRINCIPALS:
                if kind in props:
                    attach(lid, kind, props[kind], stmts)
    for cfn, (kind, name_prop) in _SINGLE.items():
        for lid, res in template.find_resources(cfn).items():
            props = res.get("Properties", {}) or {}
            attach(lid, kind, props.get(name_prop), _doc_statements(props.get("PolicyDocument")))
    # managed policies a principal lists: Ref, GetAtt PolicyArn or Sub of a template policy;
    # any other intrinsic may be any of them; a literal ARN is from elsewhere and unreadable
    for kind, (cfn, _) in _PRINCIPALS.items():
        for pid, res in template.find_resources(cfn).items():
            for arn in _as_list((res.get("Properties", {}) or {}).get("ManagedPolicyArns", [])):
                if isinstance(arn, str):
                    continue
                mid = None
                if isinstance(arn, dict) and set(arn) == {"Ref"}:
                    mid = arn["Ref"]
                elif isinstance(arn, dict) and set(arn) == {"Fn::GetAtt"}:
                    mid = arn["Fn::GetAtt"][0]
                elif isinstance(arn, dict) and set(arn) == {"Fn::Sub"} and isinstance(arn["Fn::Sub"], str):
                    m = re.fullmatch(r"\$\{([A-Za-z0-9]+)(?:\.PolicyArn)?\}", arn["Fn::Sub"])
                    mid = m.group(1) if m else None
                if mid in managed:
                    found[pid].extend(managed[mid])
                else:
                    for stmts in managed.values():
                        found[pid].extend(stmts)
                    if not managed:
                        found[pid].append(None)
    return sorted(found.items())


def _principals_granting(template: Template, action: str, targets) -> list[str]:
    """Every principal (see ``_policy_principals``) allowed ``action`` on any of ``targets``,
    read leniently: whatever the template's IAM resources attach. Managed policies named by a
    literal ARN (AWS-managed or from another stack) cannot be read and are not counted."""
    ctx, resources = _Ctx(template), _resources(targets)
    return [pid for pid, stmts in _policy_principals(template) if _allows(stmts, action, resources, ctx)]


def _state_targets(bucket_lid: str, props: dict, key: str | None) -> list[str]:
    """The forms of the state bucket (or an object in it): its GetAtt and Ref placeholders and
    its house name with the account and region left open, so neither a GetAtt/Ref-built nor a
    hand-written ARN in any partition, account or region escapes the match."""
    prefix = props.get("BucketNamePrefix", "state")
    bases = [
        f"<{bucket_lid}.Arn>",
        f"arn:{_SEG}:s3:::<{bucket_lid}>",
        f"arn:{_SEG}:s3:::{prefix}-????????????-{_SEG}-an",
    ]
    return [b if key is None else f"{b}/{key}" for b in bases]


def _state_bucket(template: Template, name: str | None) -> tuple[str, dict]:
    """The state bucket: the construct's ``{repo}-tfstate`` prefix (lowercase, dots as hyphens),
    or with no name the one ``*-tfstate`` bucket."""
    want = artifact_bucket_purpose(name).removesuffix("-pipeline") + "-tfstate" if name else None
    found = [
        (lid, r)
        for lid, r in template.find_resources("AWS::S3::Bucket").items()
        if (
            (r.get("Properties") or {}).get("BucketNamePrefix") == want
            if name
            else str((r.get("Properties") or {}).get("BucketNamePrefix", "")).endswith("-tfstate")
        )
    ]
    assert len(found) == 1, f"expected one Terraform state bucket ({want or '*-tfstate'}), found {len(found)}"
    return found[0]


def assert_state_bucket(template: Template, name: str) -> None:
    """The state bucket: house name in the account-regional namespace, versioned, encrypted,
    retained, private, state never expired, and a bucket policy that only denies."""
    lid, res = _state_bucket(template, name)
    props = res.get("Properties", {})
    assert res.get("DeletionPolicy") == "Retain", "state bucket is not retained"
    assert res.get("UpdateReplacePolicy") == "Retain", "state bucket is not retained on replacement"
    assert props.get("BucketNamespace") == "account-regional", "state bucket is not account-regional"
    assert props.get("VersioningConfiguration", {}).get("Status") == "Enabled", "state bucket is not versioned"
    assert props.get("BucketEncryption"), "state bucket is not encrypted"
    block = props.get("PublicAccessBlockConfiguration", {})
    for flag in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"):
        assert block.get(flag) in (True, "true"), f"state bucket does not set {flag}"
    for rule in props.get("LifecycleConfiguration", {}).get("Rules", []):
        assert "ExpirationInDays" not in rule and "ExpirationDate" not in rule, (
            "a lifecycle rule expires current state"
        )
        for days in (
            rule.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays"),
            rule.get("NoncurrentVersionExpirationInDays"),  # the legacy spelling
        ):
            assert days is None or int(days) >= 30, (
                f"old state versions expire after {days} days; keep at least 30 for recovery"
            )
    others = {k for k in template.find_resources("AWS::S3::Bucket") if k != lid}
    ctx = _Ctx(template)
    tls_deny = False
    for pol in template.find_resources("AWS::S3::BucketPolicy").values():
        pp = pol.get("Properties", {})
        bucket = pp.get("Bucket")
        if isinstance(bucket, dict) and bucket.get("Ref") in others:
            continue  # another bucket's policy
        # The state bucket's, or one we cannot tie to a bucket: it may only deny.
        ours = bucket == {"Ref": lid}
        for st in _as_list(pp.get("PolicyDocument", {}).get("Statement", [])):
            assert st.get("Effect") == "Deny", f"a policy that may apply to the state bucket allows: {st}"
            # only an exact deny of every action without TLS, on the state bucket's own policy
            covered = {_resource_text(r, ctx) for r in _as_list(st.get("Resource", []))}
            tls_deny |= ours and (
                st.get("Condition") in ({"Bool": {"aws:SecureTransport": "false"}},
                                        {"Bool": {"aws:SecureTransport": False}})
                and _as_list(st.get("Action")) in (["s3:*"], ["*"])
                and st.get("Principal") in ({"AWS": "*"}, "*")
                # the bucket and every object in it
                and ("*" in covered or {f"<{lid}.Arn>", f"<{lid}.Arn>/*"} <= covered)
            )
    assert tls_deny, "state bucket has no TLS-only deny policy"
    # An access point is a second door with its own policy: none may allow anything.
    for ap_lid, ap in template.find_resources("AWS::S3::AccessPoint").items():
        app = ap.get("Properties") or {}
        target = app.get("Bucket")
        if isinstance(target, dict) and target.get("Ref") in others:
            continue
        if isinstance(target, str) and not target.startswith(str(props.get("BucketNamePrefix"))):
            continue
        for st in _doc_statements(app.get("Policy", {"Statement": []})):
            assert isinstance(st, dict) and st.get("Effect") == "Deny", (
                f"access point {ap_lid} may open the state bucket: {st}"
            )


def _env_of(template: Template, step: str) -> dict:
    env = _project_of(template, step)["Environment"].get("EnvironmentVariables", [])
    return {v["Name"]: (v.get("Type", "PLAINTEXT"), v.get("Value")) for v in env}


def _step_of_role(template: Template, role: str) -> str:
    for res in projects(template).values():
        if res["Properties"].get("ServiceRole", {}).get("Fn::GetAtt", [None])[0] == role:
            return _project_step_name(res["Properties"]) or role
    return role


def assert_applies_plan(template: Template, root: TerraformRoot, repo_name: str | None = None) -> None:
    """Plan once, check that plan, apply exactly that plan.

    ``Plan-<name>`` (EC2) produces the plan artifact. ``Gate-tf-plan-check-<name>`` and the
    construct-only ``Deploy-<name>`` read that artifact and nothing else, after the plan. Plan,
    plan check and deploy run exactly the construct's command lists; Plan and Deploy see only the
    root's env plus the state bucket. Only the deploy role may write or delete the state; the plan
    role may read it and hold its lock; the plan-check role cannot reach the state bucket at all.

    Other principals are read leniently (see ``_policy_principals``); managed policies attached
    by a literal ARN (e.g. AWS-managed ``AdministratorAccess``) cannot be read and are not counted.

    Note the plan artifact itself (``tfplan``, ``tfplan.json``) carries the prior state and any
    sensitive values in clear text, and the pipeline's artifact bucket is readable by every step.
    """
    gates = {a["Name"]: a for a in _stage(template, "Gates")["Actions"]}
    plan_name, check_name, deploy_name = (
        f"Plan-{root.name}", f"Gate-tf-plan-check-{root.name}", f"Deploy-{root.name}"
    )
    assert plan_name in gates, f"no {plan_name} action in Gates"
    outs = gates[plan_name].get("OutputArtifacts") or []
    assert len(outs) == 1, f"{plan_name} produces no artifact"
    artifact = outs[0]["Name"]
    assert check_name in gates, f"no {check_name} action in Gates"
    check = gates[check_name]
    assert [i["Name"] for i in check.get("InputArtifacts", [])] == [artifact], (
        f"{check_name} does not read exactly the plan artifact"
    )
    assert check["RunOrder"] > gates[plan_name]["RunOrder"], f"{check_name} does not wait for the plan"

    source_artifact = _stage(template, "Source")["Actions"][0]["OutputArtifacts"][0]["Name"]
    plan_reads = [i["Name"] for i in gates[plan_name].get("InputArtifacts", [])]
    assert plan_reads == [source_artifact], f"{plan_name} reads {plan_reads}, not the source checkout"

    deploys = {a["Name"]: a for a in _stage(template, "Deploy")["Actions"]}
    assert deploy_name in deploys, f"no {deploy_name} action in Deploy"
    for name_, action_ in ((plan_name, gates[plan_name]), (check_name, check), (deploy_name, deploys[deploy_name])):
        assert not action_.get("Configuration", {}).get("EnvironmentVariables"), (
            f"{name_} sets action-level environment variables"
        )
    reads = [i["Name"] for i in deploys[deploy_name].get("InputArtifacts", [])]
    assert reads == [artifact], f"{deploy_name} reads {reads}: it must read the plan artifact only"

    for step in (plan_name, deploy_name):
        env = _project_of(template, step)["Environment"]
        assert env["Type"] == "LINUX_CONTAINER" and env["Image"] == EC2_IMAGE.image_id, (
            f"{step} is not on EC2: Terraform must not be killed by Lambda's 15-minute limit"
        )

    # Exact command lists: nothing added, removed or reordered.
    def exact(step: str, bodies: list[list[str]], artifacts: dict | None = None) -> None:
        spec = _step_buildspec(template, step)
        rest = {k: v for k, v in spec.items() if k not in ("version", "phases")}
        assert rest == ({"artifacts": artifacts} if artifacts else {}), (
            f"{step} buildspec has more than the build commands: {sorted(rest)}"
        )
        assert set(spec["phases"]) == {"build"}, f"{step} buildspec has phases besides build"
        assert set(spec["phases"]["build"]) == {"commands"}, f"{step} build phase has extra settings"
        assert spec["phases"]["build"]["commands"] in bodies, (
            f"{step} runs more or other than the construct's commands"
        )

    tf_preludes = [
        [*python_prelude(ec2=True), *terraform_prelude(v)] for v in TERRAFORM_LINUX_SHA256
    ]
    # The plan's output is the whole checkout (relative module paths) plus the saved plan.
    exact(plan_name, [[*p, *plan_commands(root)] for p in tf_preludes],
          artifacts={"base-directory": ".", "files": "**/*"})
    exact(deploy_name, [[*p, *apply_commands(root)] for p in tf_preludes])
    exact(check_name, [[*python_prelude(), f"pip install {TF_CHECKOV}",
                        *plan_check_commands(root, root.path)]])

    # Environment: the root's env plus the state bucket, nothing else.
    bucket_lid, _ = _state_bucket(template, repo_name)
    for step in (plan_name, deploy_name):
        env = _env_of(template, step)
        expected = {k: ("PLAINTEXT", v) for k, v in root.environment.items()}
        bucket = env.pop("TF_STATE_BUCKET", None)
        assert bucket is not None, f"{step} has no TF_STATE_BUCKET"
        assert bucket[1] == {"Ref": bucket_lid}, f"{step} points TF_STATE_BUCKET elsewhere"
        assert env == expected, f"{step} environment differs from the root's: {env}"
    assert _env_of(template, check_name) == {"CI": ("PLAINTEXT", "true")}, (
        f"{check_name} has environment variables besides CI=true"
    )

    # IAM, as IAM evaluates it (case-insensitive actions, wildcards, literal or GetAtt ARNs).
    bucket_lid, bucket = _state_bucket(template, repo_name)
    props = bucket.get("Properties") or {}
    the_bucket = _state_targets(bucket_lid, props, None)
    state = [_state_targets(bucket_lid, props, root.state_key)]
    # The bucket, the state object and its lock: a pattern like bucket/main/* must count as
    # reaching state, so concrete objects are the targets, never a "bucket/*" pattern.
    whole = [the_bucket, *state, _state_targets(bucket_lid, props, root.state_key + ".tflock")]
    roles = {step: _role_of(template, step) for step in (plan_name, check_name, deploy_name)}
    assert _grants(template, roles[deploy_name], "s3:PutObject", state), (
        f"{deploy_name} cannot write the state"
    )
    # Only the deploy role may write state or change the bucket. Its own role is read strictly
    # above; every other principal the template's IAM reaches is read leniently.
    for action in ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"):
        for pid in _principals_granting(template, action, state):
            assert pid == roles[deploy_name], (
                f"{_step_of_role(template, pid)} can write the Terraform state ({action})"
            )
    for action in _BUCKET_ADMIN:
        for pid in _principals_granting(template, action, [the_bucket]):
            assert pid == roles[deploy_name], (
                f"{_step_of_role(template, pid)} can change the Terraform state bucket ({action})"
            )
    for role in (roles[plan_name], roles[check_name]):
        _role_policy_statements(template, {role})  # strict: no managed policies, no Not*
    for action in ("s3:GetObject", "s3:ListBucket"):
        assert not _grants(template, roles[check_name], action, whole), (
            f"{check_name} can reach the state bucket"
        )


#: Bucket-level actions that could weaken the state bucket (open it, stop versioning, expire
#: state, delete it); no role but the deploy role may hold them.
_BUCKET_ADMIN = (
    "s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning",
    "s3:PutLifecycleConfiguration", "s3:PutBucketPublicAccessBlock", "s3:PutBucketAcl",
    "s3:PutEncryptionConfiguration", "s3:DeleteBucket",
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
    prefixed = [
        r["Properties"]
        for r in template.find_resources("AWS::S3::Bucket").values()
        if "BucketNamePrefix" in (r.get("Properties") or {})
    ]
    # Every house-named bucket (the artifact bucket, a Terraform state bucket) must be in the
    # account-regional namespace and private.
    for b in prefixed:
        assert "BucketName" not in b, f"{b['BucketNamePrefix']} uses a literal BucketName"
        assert b.get("BucketNamespace") == "account-regional", (
            f"{b['BucketNamePrefix']} is not in the account-regional namespace"
        )
        assert b.get("PublicAccessBlockConfiguration", {}).get("BlockPublicAcls") in (True, "true"), (
            f"{b['BucketNamePrefix']} allows public access"
        )
    expected = artifact_bucket_purpose(name)
    buckets = [b for b in prefixed if b["BucketNamePrefix"] == expected]
    assert len(buckets) == 1, (
        f"expected one artifact bucket with prefix {expected!r}, found {len(buckets)}"
    )
    props = buckets[0]
    assert "BucketName" not in props, "artifact bucket uses a literal BucketName"
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
        assert "ConcurrentBuildLimit" not in props, (
            "a PR check has a concurrency limit: CodeBuild drops builds over it, leaving a PR "
            "without its status"
        )
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
    post_deploy: Sequence[PostDeployStep] = (),
    terraform: TerraformRoot | None = None,
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
    runtimes: dict[str, Runtime] = {}
    assembly_on_ec2 = assembly is not None and assembly.ec2 is not None
    if assembly is not None:
        runtimes[assembly.name] = Runtime.PYTHON_NODE
    if assembly_on_ec2:
        expected_ec2.add(assembly.name)
    for g in gates:
        runtimes[f"Gate-{g.name}"] = g.runtime
        # a uses_assembly gate's PR check synthesizes first, on Python+Node
        runtimes[f"{name}-pr-{g.name}"] = Runtime.PYTHON_NODE if g.uses_assembly else g.runtime
        if g.ec2 is not None:
            expected_ec2.add(f"Gate-{g.name}")
        pr_on_ec2 = g.ec2 is not None or (g.uses_assembly and assembly_on_ec2)
        if pr_on_ec2 and pull_request_checks and g.runs_on_pull_requests:
            expected_ec2.add(f"{name}-pr-{g.name}")
    declared = {f"Post-{step.name}" for step in post_deploy}
    # Pipeline steps only (a PR check of a repo named "Post-..." is not one), found both by the
    # project's description and by the pipeline's own action names, which a description edit
    # cannot hide.
    present = {
        n
        for r in projects(template).values()
        if str(r["Properties"].get("Description", "")).startswith("Pipeline step ")
        and (n := _project_step_name(r["Properties"])).startswith("Post-")
    } | {
        a["Name"]
        for st in pipeline(template).get("Stages", [])
        for a in st.get("Actions", [])
        if a["Name"].startswith("Post-")
    }
    # an undeclared post-deploy step would skip its runtime and compute checks
    assert present <= declared, (
        f"post-deploy steps {sorted(present - declared)} are in the pipeline but not passed as "
        "post_deploy=; pass them so their runtime and compute are checked"
    )
    for step in post_deploy:
        runtimes[f"Post-{step.name}"] = step.runtime
        if step.ec2 is not None:
            expected_ec2.add(f"Post-{step.name}")
    if deploy_step is not None:
        builds_from_assembly = assembly is not None or bool(_assembly_actions(template))
        # the construct-only deploy runs only the pinned Node; a legacy deploy its own runtime
        runtimes[f"Deploy-{deploy_step.name}"] = (
            Runtime.NODE if builds_from_assembly else deploy_step.runtime
        )
        runtimes[f"PreDeploy-{deploy_step.name}"] = deploy_step.runtime
    if terraform is not None:
        expected_ec2 |= {f"Plan-{terraform.name}", f"Deploy-{terraform.name}"}
        runtimes[f"Plan-{terraform.name}"] = Runtime.TERRAFORM
        runtimes[f"Deploy-{terraform.name}"] = Runtime.TERRAFORM
    if deploy_step is not None and deploy_step.ec2 is not None:
        expected_ec2.add(f"Deploy-{deploy_step.name}")
        pre = f"PreDeploy-{deploy_step.name}"
        if any(_project_step_name(r["Properties"]) == pre for r in projects(template).values()):
            expected_ec2.add(pre)
    assert_lambda_compute(template, expected_ec2, runtimes, pr_prefix=name)
    assert_ci_env_on_repo_command_projects(template)
    assert_stage_order(template, deploy_stage)
    checked_gates = list(gates)
    if terraform is not None:
        # The construct's plan check reads the plan artifact, like an assembly gate.
        checked_gates.append(
            Gate(f"tf-plan-check-{terraform.name}", ["true"], uses_assembly=True)
        )
    assert_gates_present(template, checked_gates)
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
    if terraform is not None:
        assert_applies_plan(template, terraform, name)
        assert_state_bucket(template, name)
        builds_once = False if builds_once is None else builds_once
    if builds_once is None:
        builds_once = bool(_assembly_actions(template)) or any(g.uses_assembly for g in gates)
    if builds_once:
        assert_builds_once(template, gates, deploy_stage)


__all__ = [
    "CDK_INTERNAL_WILDCARDS",
    "assert_applies_plan",
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
    "assert_state_bucket",
    "pipeline",
    "pr_projects",
    "projects",
    "step_commands",
    "step_mounts",
]
