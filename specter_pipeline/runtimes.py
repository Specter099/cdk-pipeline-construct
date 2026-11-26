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
from collections.abc import Sequence

from aws_cdk import aws_codebuild as codebuild

#: Node used for ``Runtime.PYTHON_NODE``. Must satisfy the frontends' engines
#: field (cloud-buddy requires >=22.22).
DEFAULT_NODE_VERSION = "v22.22.0"

#: Pinned so every run uses the same CLI (an unpinned CLI deploys with whatever
#: shipped that day).
DEFAULT_CDK_CLI_VERSION = "2.1149.0"

_VENV = "/tmp/venv"
_NODE_HOME = "/tmp/nodejs"
#: Where the pinned Node and CDK CLI are installed; the CLI is ``f"{NODE_HOME}/bin/cdk"``.
NODE_HOME = _NODE_HOME
_JSII_CACHE = "/tmp/jsii-cache"


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


#: The Python every step's venv is built from. Lambda's python3.12 image provides it as
#: ``python3``; on EC2 the standard image carries several, so the venv is built from this exact
#: interpreter and the build stops if the image lacks it.
PYTHON_VERSION = "3.12"


def python_prelude(*, ec2: bool = False) -> list[str]:
    """Create ``/tmp/venv`` and activate it, with the image's pip config off. On EC2 the venv is
    built from ``python3.12`` explicitly (fails closed if the image has none)."""
    interpreter = f"python{PYTHON_VERSION}" if ec2 else "python3"
    missing = f"this image has no {interpreter}; EC2 steps need it"
    check = [f'command -v {interpreter} >/dev/null || {{ echo "{missing}"; exit 1; }}'] if ec2 else []
    return [
        *check,
        # Image-level pip settings override the venv; drop every PIP_* variable.
        'for v in $(env | grep -oE "^PIP_[A-Z_]+"); do unset "$v"; done',
        "unset PYTHONPATH PYTHONUSERBASE",
        "export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1",
        # On Lambda compute HOME is /tmp, so jsii (the CDK library's Node bridge)
        # defaults its package cache to /tmp/.cache, whose permissions it rejects
        # ("other users may be able to modify it"), failing `import aws_cdk`.
        # Found by running real builds on Lambda compute. Give it a private dir.
        f"mkdir -m 700 -p {_JSII_CACHE}",
        f"export JSII_RUNTIME_PACKAGE_CACHE_ROOT={_JSII_CACHE}",
        f"{interpreter} -m venv {_VENV}",
        f". {_VENV}/bin/activate",
    ]


#: SHA-256 of ``node-<version>-linux-x64.tar.gz``, taken from the published
#: ``SHASUMS256.txt`` and committed here. The checksum must NOT be downloaded
#: from the same origin as the tarball: that detects corruption but not a
#: compromised origin or mirror, and this binary runs in jobs that hold deploy
#: credentials. To add a version, copy its hash from
#: https://nodejs.org/dist/<version>/SHASUMS256.txt in a reviewed change.
NODE_LINUX_X64_SHA256 = {
    "v22.22.0": "c33c39ed9c80deddde77c960d00119918b9e352426fd604ba41638d6526a4744",
}


def node_tarball_prelude(node_version: str = DEFAULT_NODE_VERSION) -> list[str]:
    """Fetch a pinned Node into /tmp, verify it against a committed hash, put it on PATH."""
    try:
        sha256 = NODE_LINUX_X64_SHA256[node_version]
    except KeyError:
        raise ValueError(
            f"no committed SHA-256 for Node {node_version!r}; add it to "
            f"NODE_LINUX_X64_SHA256 (known: {sorted(NODE_LINUX_X64_SHA256)})"
        ) from None
    tarball = f"node-{node_version}-linux-x64.tar.gz"
    return [
        f"curl -fsSL -o /tmp/node.tgz https://nodejs.org/dist/{node_version}/{tarball}",
        # Non-zero exit (build fails) on any mismatch.
        f'echo "{sha256}  /tmp/node.tgz" | sha256sum -c -',
        f"mkdir -p {_NODE_HOME} && tar -xzf /tmp/node.tgz -C {_NODE_HOME} --strip-components=1",
        f"export PATH={_NODE_HOME}/bin:$PATH",
        "node --version",
    ]


def cdk_cli_install(version: str = DEFAULT_CDK_CLI_VERSION) -> list[str]:
    """Install the pinned CDK CLI under /tmp so it works without root.

    Also puts ``/tmp/nodejs/bin`` on PATH: on the Node image Node is already on
    PATH but the CLI lands under /tmp, and on the Python+Node runtime the PATH
    export is already done (repeating it is harmless).
    """
    return [
        f"export PATH={_NODE_HOME}/bin:$PATH",
        f"npm install -g --prefix {_NODE_HOME} aws-cdk@{version}",
        "cdk --version",
    ]


def prelude(
    runtime: Runtime, node_version: str = DEFAULT_NODE_VERSION, *, ec2: bool = False
) -> list[str]:
    """Commands that make ``runtime`` ready, run before a step's own commands.

    On EC2 the standard image's own Node and Python are not used: Node is the same pinned,
    hash-checked tarball as on Lambda (also for ``Runtime.NODE``), and the venv is built from
    ``python3.12``. So a step gets the same toolchain on both computes."""
    if runtime is Runtime.PYTHON:
        return python_prelude(ec2=ec2)
    if runtime is Runtime.NODE:
        return node_tarball_prelude(node_version) if ec2 else []
    return [*node_tarball_prelude(node_version), *python_prelude(ec2=ec2)]


def join(*groups: Sequence[str]) -> list[str]:
    return [command for group in groups for command in group]
