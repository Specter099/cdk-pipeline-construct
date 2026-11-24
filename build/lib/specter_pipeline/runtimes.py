"""Build runtimes for CodeBuild *Lambda compute*.

Lambda compute runs each build in a curated container image that carries ONE
language runtime, runs as a non-root user, writes only to ``/tmp`` and has no
Docker. Verified empirically against ``us-east-1``:

* ``python3.12`` image: Python 3.12, git, AWS CLI -- no Node.
* ``nodejs22`` image: Node v22.22.0 + npm (global installs work) -- Python 3.9.
* The python image sets pip configuration that breaks build isolation
  (``Cannot set --home and --prefix together``), so every Python prelude
  neutralises it.

A repo whose check needs both (e.g. ``cdk synth`` of a Python app) uses
``Runtime.PYTHON_NODE``: the Python image plus a pinned, checksum-verified Node
tarball unpacked into ``/tmp``. No custom ECR image is required.
"""

from __future__ import annotations

import enum
from typing import Sequence

from aws_cdk import aws_codebuild as codebuild

#: Node used for ``Runtime.PYTHON_NODE``. Must satisfy the frontends' engines
#: field (cloud-buddy requires >=22.22).
DEFAULT_NODE_VERSION = "v22.22.0"

#: Pinned so every run uses the same CLI (an unpinned CLI deploys with whatever
#: shipped that day).
DEFAULT_CDK_CLI_VERSION = "2.1149.0"

_VENV = "/tmp/venv"
_NODE_HOME = "/tmp/nodejs"


class Runtime(enum.Enum):
    """Which toolchain a step needs."""

    PYTHON = "python"
    NODE = "node"
    PYTHON_NODE = "python+node"

    @property
    def image(self) -> codebuild.IBuildImage:
        if self is Runtime.NODE:
            return codebuild.LinuxLambdaBuildImage.AMAZON_LINUX_2023_NODE_22
        return codebuild.LinuxLambdaBuildImage.AMAZON_LINUX_2023_PYTHON_3_12


def python_prelude() -> list[str]:
    """Create ``/tmp/venv`` and activate it, with the image's pip config off."""
    return [
        # Image-level pip settings override the venv; drop every PIP_* variable.
        'for v in $(env | grep -oE "^PIP_[A-Z_]+"); do unset "$v"; done',
        "unset PYTHONPATH PYTHONUSERBASE",
        "export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1",
        f"python3 -m venv {_VENV}",
        f". {_VENV}/bin/activate",
    ]


def node_tarball_prelude(node_version: str = DEFAULT_NODE_VERSION) -> list[str]:
    """Fetch a pinned Node into /tmp, verify its checksum, put it on PATH."""
    tarball = f"node-{node_version}-linux-x64.tar.gz"
    base = f"https://nodejs.org/dist/{node_version}"
    return [
        f"curl -fsSL -o /tmp/SHASUMS256.txt {base}/SHASUMS256.txt",
        f"curl -fsSL -o /tmp/node.tgz {base}/{tarball}",
        # Fail the build (non-zero) on a checksum mismatch.
        f'(cd /tmp && grep " {tarball}$" SHASUMS256.txt | sed "s/ {tarball}/ node.tgz/" | sha256sum -c -)',
        f"mkdir -p {_NODE_HOME} && tar -xzf /tmp/node.tgz -C {_NODE_HOME} --strip-components=1",
        f"export PATH={_NODE_HOME}/bin:$PATH",
        "node --version",
    ]


def cdk_cli_install(version: str = DEFAULT_CDK_CLI_VERSION) -> list[str]:
    """Install the pinned CDK CLI under /tmp so it works without root."""
    return [f"npm install -g --prefix {_NODE_HOME} aws-cdk@{version}", "cdk --version"]


def prelude(runtime: Runtime, node_version: str = DEFAULT_NODE_VERSION) -> list[str]:
    """Commands that make ``runtime`` ready, run before a step's own commands."""
    if runtime is Runtime.PYTHON:
        return python_prelude()
    if runtime is Runtime.NODE:
        return []
    return [*node_tarball_prelude(node_version), *python_prelude()]


def join(*groups: Sequence[str]) -> list[str]:
    return [command for group in groups for command in group]
