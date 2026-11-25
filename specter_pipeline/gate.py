"""Declarative description of a pipeline check and a post-deploy step.

A repo supplies *data* (``Gate`` / ``PostDeployStep`` objects) and the shared
construct turns it into CodeBuild buildspecs. This is the per-repo
customisation point: runtime, compute size, install commands, environment
variables and the commands themselves. Repo-specific logic should live in the
repo's own scripts (e.g. ``scripts/ci-local.sh``) and be *called* from here.

Lambda compute has no instance, so there is no user data. ``install`` is the
equivalent hook.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import ClassVar

from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam

from .runtimes import NODE_HOME, Runtime

_COMPUTE = {
    1: codebuild.ComputeType.LAMBDA_1GB,
    2: codebuild.ComputeType.LAMBDA_2GB,
    4: codebuild.ComputeType.LAMBDA_4GB,
    8: codebuild.ComputeType.LAMBDA_8GB,
    10: codebuild.ComputeType.LAMBDA_10GB,
}

#: Environment every step gets. CI=true makes scripts such as ci-local.sh use
#: the ambient interpreter instead of building their own .venv.
BASE_ENV = {"CI": "true"}


def validate_name(kind: str, name: str) -> None:
    """Step names become CodePipeline action and CodeBuild project names."""
    if not name or not all(c.isascii() and (c.isalnum() or c in "-_") for c in name):
        raise ValueError(
            f"{kind} name must be ASCII alphanumeric, '-' or '_': {name!r}"
        )


def compute_type(memory_gb: int) -> codebuild.ComputeType:
    try:
        return _COMPUTE[memory_gb]
    except KeyError:
        raise ValueError(
            f"Lambda compute memory must be one of {sorted(_COMPUTE)} GB, got {memory_gb}"
        ) from None


@dataclasses.dataclass(frozen=True)
class _Step:
    """Fields and validation every step shares. Subclasses change defaults only."""

    name: str
    commands: Sequence[str]
    runtime: Runtime = Runtime.PYTHON
    install: Sequence[str] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    memory_gb: int = 2
    policy_statements: Sequence[iam.PolicyStatement] = ()

    _kind: ClassVar[str] = "step"

    def __post_init__(self) -> None:
        validate_name(self._kind, self.name)
        if isinstance(self.commands, str) or isinstance(self.install, str):
            raise TypeError(f"{self._kind} {self.name!r}: commands and install are lists of strings")
        # Frozen only guards attribute assignment; copy the sequences so a caller's later
        # list.append() cannot change a step (or add permissions) after validation.
        object.__setattr__(self, "commands", list(self.commands))
        object.__setattr__(self, "install", list(self.install))
        object.__setattr__(self, "policy_statements", tuple(self.policy_statements))
        object.__setattr__(self, "env", dict(self.env))
        if not self.commands and self._needs_commands():
            raise ValueError(f"{self._kind} {self.name!r} has no commands")
        compute_type(self.memory_gb)  # validates

    def _needs_commands(self) -> bool:
        return True

    @property
    def environment(self) -> dict[str, str]:
        return {**BASE_ENV, **self.env}


@dataclasses.dataclass(frozen=True)
class Gate(_Step):
    """A check that must pass before deploy (and, by default, on pull requests).

    ``policy_statements`` grant the step's role extra AWS permissions (for example an
    analyzer API). The PR-check path runs the code of the PR head, so a gate that has its
    own AWS permissions is NOT run there unless you say so, and saying so is refused:
    ``pull_request`` defaults to "yes if the gate has no policy statements".

    ``uses_assembly`` mounts the cloud assembly the pipeline deploys (``cdk.out``) into the
    gate's working directory, so the gate inspects the exact templates that ship instead of
    synthesizing its own. On a pull request there is no pipeline assembly: the PR project
    builds one first with the pipeline's assembly commands.
    """

    pull_request: bool | None = None
    uses_assembly: bool = False

    _kind: ClassVar[str] = "gate"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.pull_request is not None and not isinstance(self.pull_request, bool):
            raise TypeError(f"gate {self.name!r}: pull_request must be True, False or None")
        if self.pull_request and self.policy_statements:
            raise ValueError(
                f"gate {self.name!r} has its own IAM permissions and cannot run on pull requests: "
                "that path executes the PR head's code"
            )

    @property
    def runs_on_pull_requests(self) -> bool:
        if self.pull_request is None:
            return not self.policy_statements
        return self.pull_request


@dataclasses.dataclass(frozen=True)
class Assembly(_Step):
    """Synthesize the application once; gates and the deploy consume the result.

    Only for ``deploy_step`` pipelines: with ``application_stage`` the pipeline's own Synth
    step already builds the assembly that is deployed. The step gets Node and the pinned CDK
    CLI. Its ``output_dir`` (default ``cdk.out``) becomes an artifact that is linked at the
    same path for every gate with ``uses_assembly=True``, and is the only input of the
    construct-only deploy action, which snapshots it and deploys the snapshot
    (``DeployStep(stacks=[...])``).
    """

    name: str = "Assemble"
    commands: Sequence[str] = ("cdk synth",)
    runtime: Runtime = Runtime.PYTHON_NODE
    memory_gb: int = 4
    output_dir: str = "cdk.out"

    _kind: ClassVar[str] = "assembly"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.policy_statements:
            raise ValueError(
                "the assembly step takes no IAM permissions: synthesis must not need AWS "
                "(commit cdk.context.json instead of looking values up)"
            )
        if self.runtime is not Runtime.PYTHON_NODE:
            raise ValueError("the assembly step runs the CDK CLI and needs Runtime.PYTHON_NODE")
        raw = self.output_dir
        norm = posixpath.normpath(raw) if raw else ""
        # Stored normalised: the value becomes a CodeBuild artifact directory and the target of
        # CDK Pipelines' `ln -s`, which fails on "cdk.out/" or "./cdk.out".
        if (
            not re.fullmatch(r"(\./)?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*/?", raw or "")
            or ".." in raw.split("/")
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_.-]+)*", norm)
        ):
            raise ValueError(
                f"assembly output_dir must be a relative path below the source, got {raw!r}"
            )
        object.__setattr__(self, "output_dir", norm)
        if self.name.startswith(("Gate-", "Deploy-", "Post-", "Approve-")):
            raise ValueError(f"assembly name {self.name!r} collides with the pipeline's step names")


@dataclasses.dataclass(frozen=True)
class DeployStep(_Step):
    """Deploy the application by running the repo's own deploy command.

    Use this instead of ``application_stage`` for a stack that is already live.
    Wrapping an existing stack in a CDK Pipelines ``Stage`` changes the
    construct path that Lambda permissions and event-source mappings embed in
    their logical IDs, so CloudFormation would delete and recreate them on the
    first deploy. Running ``cdk deploy <stack>`` from the repo's own app keeps
    every logical ID exactly as it is.

    The step's role must be allowed to assume the CDK bootstrap roles (the
    construct grants ``sts:AssumeRole`` on ``cdk-*`` roles for this account).

    With an ``Assembly`` (build once) give ``stacks`` instead of a deploy command. The deploy
    is then a construct-only action: its only input is the assembly artifact (no source, so no
    repo code or ``cdk.json`` runs there), and it runs the pinned CLI by absolute path:
    ``cdk deploy --app <snapshot> <stacks> <deploy_args>`` (``--require-approval never`` is
    added unless ``deploy_args`` sets ``--require-approval``). The deploy may assume the CDK
    bootstrap roles of this pipeline's account and region, plus those of each
    ``(account, region)`` in ``deploy_environments`` (for cross-account or cross-region stacks,
    whose environments must be bootstrapped to trust this account). ``install``/``commands``, if any,
    run before it in a separate ``PreDeploy-<name>`` action that holds ``policy_statements``
    but cannot assume the CDK deploy roles; they may not use the CDK CLI.
    """

    commands: Sequence[str] = ()
    runtime: Runtime = Runtime.PYTHON_NODE
    memory_gb: int = 4
    stacks: Sequence[str] = ()
    deploy_args: Sequence[str] = ()
    deploy_environments: Sequence[tuple[str, str]] = ()

    _kind: ClassVar[str] = "deploy step"

    def __post_init__(self) -> None:
        for field in ("stacks", "deploy_args"):
            if isinstance(getattr(self, field), str):
                raise TypeError(f"deploy step {self.name!r}: {field} is a list of strings")
            object.__setattr__(self, field, tuple(getattr(self, field)))
        super().__post_init__()
        object.__setattr__(
            self, "deploy_environments", tuple(tuple(e) for e in self.deploy_environments)
        )
        for env in self.deploy_environments:
            if (
                len(env) != 2
                or not re.fullmatch(r"\d{12}", env[0])
                or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", env[1])
            ):
                raise ValueError(
                    f"deploy step {self.name!r}: deploy_environments entries are "
                    f"(12-digit account, region), got {env!r}"
                )
        for stack in self.stacks:
            if not re.fullmatch(r"[A-Za-z0-9*][A-Za-z0-9_*/.-]*", stack):
                raise ValueError(f"deploy step {self.name!r}: invalid stack name {stack!r}")
        for arg in self.deploy_args:
            if owns_deploy_flag(arg):
                raise ValueError(
                    f"deploy step {self.name!r}: {arg!r} sets --app, --build or --output, "
                    "which the construct sets, not deploy_args"
                )

    def _needs_commands(self) -> bool:
        return not self.stacks


@dataclasses.dataclass(frozen=True)
class PostDeployStep(_Step):
    """A step that runs after the application stage deploys successfully."""

    runtime: Runtime = Runtime.NODE
    memory_gb: int = 4

    _kind: ClassVar[str] = "post-deploy step"


# --- build once: the deploy step deploys a snapshot of the assembly --------------------------

#: Where the deploy step copies the assembly before running any repo command.
SNAPSHOT_DIR = "/tmp/assembly"

#: The pinned CDK CLI, run by absolute path so a PATH change cannot swap it.
CDK_BIN = f"{NODE_HOME}/bin/cdk"

#: Flags the construct sets on the deploy itself. --build and --output would run or redirect a
#: synthesis; --app is the snapshot.
_OWNED_LONG = ("--app", "--build", "--output")

#: Where the CLI runs: a fresh empty directory, so no ./cdk.json (and its "build" hook) is read.
DEPLOY_WORKDIR = "/tmp/deploy"


def _long_flag(arg: str) -> str:
    """The canonical spelling of a long option as the CLI parses it: --requireApproval and
    --require-approval are one flag; --no-app negates --app."""
    flag = arg.split("=", 1)[0]
    if not flag.startswith("--"):
        return flag
    flag = "--" + re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", flag[2:]).lower()
    return "--" + flag[5:] if flag.startswith("--no-") else flag


def owns_deploy_flag(arg: str) -> bool:
    """``--app``/``--build``/``--output`` in any spelling the CLI accepts (``--app=x``,
    ``--no-app``, ``--outputDir``-style camelCase) or a short-flag cluster holding ``-a`` or
    ``-o`` (``-va``)."""
    if _long_flag(arg) in _OWNED_LONG:
        return True
    return bool(re.fullmatch(r"-[A-Za-z]*[ao][A-Za-z]*(=.*)?", arg))


#: The CDK CLI in any spelling a shell could run: cdk, aws-cdk (also as a path such as
#: node_modules/aws-cdk/bin/cdk.js), cdk@2, .../bin/cdk, in a loop, subshell, backticks or
#: wrapper. Not cdk.out, cdk.json, aws-cdk-lib, cdk_nag, cdk8s or a cdk/ directory in a path.
#: A bare word "cdk" (`cd cdk`, `--prefix cdk`) is still refused.
_CDK_CLI = re.compile(
    r"(?<![\w.-])aws-cdk(@[\w.^~*-]*)?(?![\w.-])"
    r"|(?<![\w.-])cdk(@[\w.^~*-]*)?(?![\w./-])"
)


def mentions_cdk_cli(command: str) -> bool:
    """True when ``command`` could run the CDK CLI. A plain text match, not a shell parse, so
    nothing a shell can express hides it; it cannot see into scripts or variables."""
    return bool(_CDK_CLI.search(command))


def snapshot_commands(mount: str) -> list[str]:
    """Copy the assembly at ``mount`` to SNAPSHOT_DIR and refuse one that carries a cdk.json
    (whose "build" hook the CLI would run before loading the snapshot). Fails if the assembly is
    missing or empty."""
    src = shlex.quote(f"{mount}/.")
    manifest = shlex.quote(f"{mount}/manifest.json")
    return [
        f'test -f {manifest} || {{ echo "no cloud assembly at {mount}"; exit 1; }}',
        f"rm -rf {SNAPSHOT_DIR} && mkdir -p {SNAPSHOT_DIR} && cp -R {src} {SNAPSHOT_DIR}/",
        f'test ! -e {SNAPSHOT_DIR}/cdk.json || {{ echo "the assembly contains a cdk.json; refusing"; exit 1; }}',
    ]


def deploy_command(stacks: Sequence[str], deploy_args: Sequence[str]) -> str:
    """Run the pinned CLI from a fresh empty directory against the snapshot."""
    args = list(deploy_args)
    if not any(_long_flag(a) == "--require-approval" for a in args):
        args += ["--require-approval", "never"]  # no TTY: a prompt would fail the deploy
    cli = shlex.join([CDK_BIN, "deploy", "--app", SNAPSHOT_DIR, *stacks, *args])
    return f"rm -rf {DEPLOY_WORKDIR} && mkdir -p {DEPLOY_WORKDIR} && cd {DEPLOY_WORKDIR} && {cli}"


def split_deploy_command(command: str) -> list[str] | None:
    """The CLI tokens of a ``deploy_command`` string, or None if it is not one."""
    prefix = f"rm -rf {DEPLOY_WORKDIR} && mkdir -p {DEPLOY_WORKDIR} && cd {DEPLOY_WORKDIR} && "
    if not command.startswith(prefix):
        return None
    cli = command[len(prefix):]
    try:
        tokens = shlex.split(cli)
    except ValueError:
        return None
    if shlex.join(tokens) != cli or tokens[:4] != [CDK_BIN, "deploy", "--app", SNAPSHOT_DIR]:
        return None
    return tokens


def names_path(command: str, path: str) -> bool:
    """True when ``command`` names ``path`` (or ``./path``) as a whole path: alone, or as the
    leading directory of a longer one, not as part of another name (cdk.out is not named by
    infra/cdk.out or cdk.outdated)."""
    p = re.escape(posixpath.normpath(path))
    return bool(re.search(rf"(?<![\w.~/$-])(\./)?{p}(?![\w.-])", command))
