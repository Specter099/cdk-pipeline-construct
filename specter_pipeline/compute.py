"""Where a step runs: CodeBuild Lambda compute (the default) or EC2 compute (opt-in per step).

Lambda compute starts fast and is cheap, but has a hard 15-minute limit, no Docker, no caching and
no custom timeout. A step that can outlive that limit -- a Terraform plan or apply, a large deploy
-- must not run there: if it is killed mid-apply, the state is left locked and half-applied. Such a
step opts in with ``ec2=Ec2(...)``. It then runs on the Amazon Linux 2023 standard 6.0 image with
an explicit timeout, never privileged. The toolchain is the same as on Lambda: the pinned,
hash-checked Node tarball and a venv built from python3.12, not the image's defaults. What does
differ: EC2 builds run as root with HOME=/root (Lambda: a non-root user, HOME=/tmp), and the image
tag is AWS-maintained, so its OS packages are patched under the same tag.
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

#: Memory in GB of each EC2 size, for choosing one that is at least a Lambda size.
_EC2_MEMORY_GB = {"small": 3, "medium": 7, "large": 15, "xlarge": 72, "2xlarge": 144}

#: Lambda compute's hard limit, the time a Lambda step can take.
LAMBDA_LIMIT_MINUTES = 15

#: The image every EC2 step runs on: one major version. AWS republishes the tag with patches, so
#: the toolchain steps rely on is installed by the step itself (see runtimes.prelude).
EC2_IMAGE = codebuild.LinuxBuildImage.AMAZON_LINUX_2023_6


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

    ``timeout_minutes`` is the CodeBuild build timeout (5 to 480; 480 is CodePipeline's limit
    for a CodeBuild action), default 60. Pick it from the step's worst case, not its usual run:
    a timeout during an apply has the same effect as Lambda's limit. ``memory_gb`` is ignored
    for an EC2 step; the size sets memory.
    """

    size: str = "small"
    timeout_minutes: int = 60

    def __post_init__(self) -> None:
        if not isinstance(self.size, str) or self.size not in _EC2:
            raise ValueError(f"EC2 size must be one of {sorted(_EC2)}, got {self.size!r}")
        if (
            not isinstance(self.timeout_minutes, int)
            or isinstance(self.timeout_minutes, bool)
            or not 5 <= self.timeout_minutes <= 480
        ):
            raise ValueError(
                f"EC2 timeout_minutes must be an integer from 5 to 480, got {self.timeout_minutes!r}"
            )

    def larger(self, other: Ec2 | None) -> Ec2:
        """The bigger of two sizes, with room for both timeouts (capped at 480): for a step
        that does another step's work first, as an assembly gate's PR check does."""
        if other is None:
            return self
        order = list(_EC2)
        size = max(self.size, other.size, key=order.index)
        return Ec2(size, min(480, self.timeout_minutes + other.timeout_minutes))

    @classmethod
    def at_least(cls, memory_gb: int, timeout_minutes: int = LAMBDA_LIMIT_MINUTES) -> Ec2:
        """The smallest EC2 size with at least ``memory_gb``: the EC2 equal of a Lambda step."""
        size = next(n for n, gb in _EC2_MEMORY_GB.items() if gb >= memory_gb)
        return cls(size, max(5, timeout_minutes))

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
