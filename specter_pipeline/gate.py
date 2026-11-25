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
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import ClassVar

from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam

from .runtimes import Runtime

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
        if not self.commands:
            raise ValueError(f"{self._kind} {self.name!r} has no commands")
        compute_type(self.memory_gb)  # validates

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
    CLI. Its ``output_dir`` (default ``cdk.out``) becomes an artifact that is mounted at the
    same path for every gate with ``uses_assembly=True`` and for the deploy step, whose
    ``cdk deploy`` must then read it (``cdk deploy --app cdk.out ...``).
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
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", self.output_dir) or ".." in self.output_dir:
            raise ValueError(f"assembly output_dir must be a relative path, got {self.output_dir!r}")


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
    """

    runtime: Runtime = Runtime.PYTHON_NODE
    memory_gb: int = 4

    _kind: ClassVar[str] = "deploy step"


@dataclasses.dataclass(frozen=True)
class PostDeployStep(_Step):
    """A step that runs after the application stage deploys successfully."""

    runtime: Runtime = Runtime.NODE
    memory_gb: int = 4

    _kind: ClassVar[str] = "post-deploy step"


_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def cdk_deploy_apps(command: str) -> list[str | None]:
    """For each ``cdk deploy`` in a shell command, the ``--app``/``-a`` value it passes (None if
    it passes none). Each part of a command joined with ``&&``, ``||``, ``;`` or ``|`` is inspected
    on its own. A command that cannot be tokenised and mentions both words counts as one deploy
    with no app, so it fails closed."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return [None] if "cdk" in command and "deploy" in command else []
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    found: list[str | None] = []
    for seg in segments:
        cli = [i for i, t in enumerate(seg) if t.rsplit("/", 1)[-1] == "cdk"]
        if not cli or "deploy" not in seg[cli[0] + 1 :]:
            continue
        args = seg[cli[0] + 1 :]
        app: str | None = None
        for k, arg in enumerate(args):
            if arg in ("--app", "-a") and k + 1 < len(args):
                app = args[k + 1]
            elif arg.startswith("--app="):
                app = arg.split("=", 1)[1]
        found.append(app)
    return found
