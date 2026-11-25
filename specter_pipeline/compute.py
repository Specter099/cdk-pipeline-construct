"""Where a step runs: CodeBuild Lambda compute (the default) or EC2 compute (opt-in per step).

Lambda compute starts fast and is cheap, but has a hard 15-minute limit, no Docker, no caching and
no custom timeout. A step that can outlive that limit -- a Terraform plan or apply, a large deploy
-- must not run there: if it is killed mid-apply, the state is left locked and half-applied. Such a
step opts in with ``ec2=Ec2(...)``. It then runs on the Amazon Linux 2023 standard image with an
explicit timeout, never privileged. The runtime set-up (pinned Node, the /tmp venv) is the same as
on Lambda, so a step's commands do not change when it moves.
"""

from __future__ import annotations

import dataclasses

from aws_cdk import Duration
from aws_cdk import aws_codebuild as codebuild

from .runtimes import Runtime

_LAMBDA = {
    1: codebuild.ComputeType.LAMBDA_1GB,
    2: codebuild.ComputeType.LAMBDA_2GB,
    4: codebuild.ComputeType.LAMBDA_4GB,
    8: codebuild.ComputeType.LAMBDA_8GB,
    10: codebuild.ComputeType.LAMBDA_10GB,
}

#: EC2 sizes by CodeBuild's names: small 2 vCPU/3 GB, medium 4/7, large 8/15, xlarge 36/72,
#: 2xlarge 72/144.
_EC2 = {
    "small": codebuild.ComputeType.SMALL,
    "medium": codebuild.ComputeType.MEDIUM,
    "large": codebuild.ComputeType.LARGE,
    "xlarge": codebuild.ComputeType.X_LARGE,
    "2xlarge": codebuild.ComputeType.X2_LARGE,
}

#: The image every EC2 step runs on. Pinned to one major version so a rebuild cannot change it.
EC2_IMAGE = codebuild.LinuxBuildImage.AMAZON_LINUX_2023_5


def compute_type(memory_gb: int) -> codebuild.ComputeType:
    """The Lambda compute type for ``memory_gb``."""
    try:
        return _LAMBDA[memory_gb]
    except KeyError:
        raise ValueError(
            f"Lambda compute memory must be one of {sorted(_LAMBDA)} GB, got {memory_gb}"
        ) from None


@dataclasses.dataclass(frozen=True)
class Ec2:
    """Run a step on EC2 compute.

    ``timeout_minutes`` is the CodeBuild build timeout (5 to 480). Pick it from the step's worst
    case, not its usual run: a timeout during an apply has the same effect as Lambda's limit.
    """

    size: str = "small"
    timeout_minutes: int = 60

    def __post_init__(self) -> None:
        if self.size not in _EC2:
            raise ValueError(f"EC2 size must be one of {sorted(_EC2)}, got {self.size!r}")
        if (
            not isinstance(self.timeout_minutes, int)
            or isinstance(self.timeout_minutes, bool)
            or not 5 <= self.timeout_minutes <= 480
        ):
            raise ValueError(
                f"EC2 timeout_minutes must be an integer from 5 to 480, got {self.timeout_minutes!r}"
            )

    @property
    def compute_type(self) -> codebuild.ComputeType:
        return _EC2[self.size]

    @property
    def timeout(self) -> Duration:
        return Duration.minutes(self.timeout_minutes)


def build_environment(runtime: Runtime, memory_gb: int, ec2: Ec2 | None) -> codebuild.BuildEnvironment:
    """The CodeBuild environment of a step: Lambda unless it opted into EC2."""
    if ec2 is None:
        return codebuild.BuildEnvironment(
            build_image=runtime.image, compute_type=compute_type(memory_gb)
        )
    return codebuild.BuildEnvironment(
        build_image=EC2_IMAGE, compute_type=ec2.compute_type, privileged=False
    )


def build_timeout(ec2: Ec2 | None) -> Duration | None:
    """CodeBuild rejects a timeout on Lambda compute; EC2 steps always set theirs."""
    return ec2.timeout if ec2 is not None else None
