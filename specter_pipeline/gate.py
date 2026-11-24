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
from collections.abc import Mapping, Sequence

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
class Gate:
    """A check that must pass before deploy (and, by default, on pull requests).

    ``policy_statements`` grant the step's role extra AWS permissions (for example an
    analyzer API). The PR-check path runs the code of the PR head, so a gate that has its
    own AWS permissions is NOT run there unless you say so, and saying so is refused:
    ``pull_request`` defaults to "yes if the gate has no policy statements".
    """

    name: str
    commands: Sequence[str]
    runtime: Runtime = Runtime.PYTHON
    install: Sequence[str] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    memory_gb: int = 2
    policy_statements: Sequence[iam.PolicyStatement] = ()
    pull_request: bool | None = None

    def __post_init__(self) -> None:
        validate_name("gate", self.name)
        if not self.commands:
            raise ValueError(f"gate {self.name!r} has no commands")
        compute_type(self.memory_gb)  # validates
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

    @property
    def environment(self) -> dict[str, str]:
        return {**BASE_ENV, **self.env}


@dataclasses.dataclass(frozen=True)
class DeployStep:
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

    name: str
    commands: Sequence[str]
    runtime: Runtime = Runtime.PYTHON_NODE
    install: Sequence[str] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    memory_gb: int = 4
    policy_statements: Sequence[iam.PolicyStatement] = ()

    def __post_init__(self) -> None:
        validate_name("deploy step", self.name)
        if not self.commands:
            raise ValueError(f"deploy step {self.name!r} has no commands")
        compute_type(self.memory_gb)

    @property
    def environment(self) -> dict[str, str]:
        return {**BASE_ENV, **self.env}


@dataclasses.dataclass(frozen=True)
class PostDeployStep:
    """A step that runs after the application stage deploys successfully."""

    name: str
    commands: Sequence[str]
    runtime: Runtime = Runtime.NODE
    install: Sequence[str] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    memory_gb: int = 4
    policy_statements: Sequence[iam.PolicyStatement] = ()

    def __post_init__(self) -> None:
        validate_name("post-deploy step", self.name)
        if not self.commands:
            raise ValueError(f"post-deploy step {self.name!r} has no commands")
        compute_type(self.memory_gb)

    @property
    def environment(self) -> dict[str, str]:
        return {**BASE_ENV, **self.env}
