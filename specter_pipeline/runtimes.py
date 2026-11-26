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
    #: The Python image plus a pinned Terraform binary on PATH, checked against a committed hash
    #: (taken from the release's SHA256SUMS after its signature was verified once, by hand).
    TERRAFORM = "terraform"

    @property
    def image(self) -> codebuild.IBuildImage:
        if self is Runtime.NODE:
            return codebuild.LinuxLambdaBuildImage.AMAZON_LINUX_2023_NODE_22
        return codebuild.LinuxLambdaBuildImage.AMAZON_LINUX_2023_PYTHON_3_12


#: The Python every step's venv is built from. Lambda's python3.12 image provides it as
#: ``python3``. The EC2 standard image installs several through pyenv but activates only one
#: (3.14 on 6.0), and exports PYTHON_312_VERSION; a ``python3.12`` shim exists for the inactive
#: versions but fails when run. So on EC2 the step selects 3.12 through pyenv, runs it to prove
#: it is 3.12, and stops otherwise.
PYTHON_VERSION = "3.12"
_PYENV_VAR = "PYTHON_" + PYTHON_VERSION.replace(".", "") + "_VERSION"


def ec2_python_select() -> list[str]:
    """Make python3.12 the active interpreter on the EC2 standard image, or stop the build."""
    want = f"({PYTHON_VERSION.replace('.', ', ')})"
    return [
        f': "${{{_PYENV_VAR}:?this image does not provide Python {PYTHON_VERSION} ({_PYENV_VAR} is unset)}}"',
        f'export PYENV_VERSION="${_PYENV_VAR}"',
        (
            f"python{PYTHON_VERSION} -c 'import sys; assert sys.version_info[:2] == {want}, sys.version' "
            f'|| {{ echo "python{PYTHON_VERSION} does not run Python {PYTHON_VERSION} on this image"; exit 1; }}'
        ),
    ]


def python_prelude(*, ec2: bool = False) -> list[str]:
    """Create ``/tmp/venv`` and activate it, with the image's pip config off. On EC2 the venv is
    built from Python 3.12 selected through pyenv (``ec2_python_select``), or the build stops."""
    interpreter = f"python{PYTHON_VERSION}" if ec2 else "python3"
    return [
        *(ec2_python_select() if ec2 else []),
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
        f"mkdir -p {_NODE_HOME} && tar -xzf /tmp/node.tgz -C {_NODE_HOME} --strip-components=1 --no-same-owner",
        f"export PATH={_NODE_HOME}/bin:$PATH",
        "node --version",
    ]


#: Terraform used for ``Runtime.TERRAFORM``.
DEFAULT_TERRAFORM_VERSION = "1.17.2"

#: SHA-256 of ``terraform_<version>_linux_<arch>.zip``, copied from the release's
#: ``terraform_<version>_SHA256SUMS`` after verifying that file's signature
#: (``terraform_<version>_SHA256SUMS.sig``) with HashiCorp's release key, fingerprint
#: C874 011F 0AB4 0511 0D02 1055 3436 5D94 72D7 468F (signed by its subkey 374E...AB87).
#: Committed, not downloaded beside the zip: a hash from the same origin only detects
#: corruption. To add a version, repeat that verification in a reviewed change.
TERRAFORM_LINUX_SHA256 = {
    "1.17.2": {
        "amd64": "e6f16b2f0b137ee5729caa8c4e79a3bae4ee261acd198a1fcfa8584a6296605b",
        "arm64": "858e08c181a76b6b223e8c767a8e983922eebe2d062db949a2de66ce040c368c",
    },
}

_TERRAFORM_HOME = "/tmp/terraform"


def terraform_prelude(version: str = DEFAULT_TERRAFORM_VERSION, arch: str = "amd64") -> list[str]:
    """Fetch a pinned Terraform, verify it against a committed hash, put it on PATH.

    Unzipped with Python's zipfile (``unzip`` is not on every image). Also sets the variables
    Terraform reads in automation: no prompts, no update check, plain output."""
    try:
        sha256 = TERRAFORM_LINUX_SHA256[version][arch]
    except KeyError:
        raise ValueError(
            f"no committed SHA-256 for Terraform {version!r} ({arch}); add it to "
            f"TERRAFORM_LINUX_SHA256 (known: {sorted(TERRAFORM_LINUX_SHA256)})"
        ) from None
    zip_name = f"terraform_{version}_linux_{arch}.zip"
    return [
        f"curl -fsSL --proto '=https' --retry 3 -o /tmp/terraform.zip https://releases.hashicorp.com/terraform/{version}/{zip_name}",
        f'echo "{sha256}  /tmp/terraform.zip" | sha256sum -c -',
        f"mkdir -p {_TERRAFORM_HOME}/bin",
        f"python3 -I -c \"import zipfile; zipfile.ZipFile('/tmp/terraform.zip').extract('terraform', '{_TERRAFORM_HOME}/bin')\"",
        f"chmod 0755 {_TERRAFORM_HOME}/bin/terraform",
        f"export PATH={_TERRAFORM_HOME}/bin:$PATH",
        "export TF_IN_AUTOMATION=1 TF_INPUT=0 CHECKPOINT_DISABLE=1",
        "terraform version",
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
    runtime: Runtime,
    node_version: str = DEFAULT_NODE_VERSION,
    terraform_version: str = DEFAULT_TERRAFORM_VERSION,
    *,
    ec2: bool = False,
) -> list[str]:
    """Commands that make ``runtime`` ready, run before a step's own commands.

    On EC2 the standard image's own Node and Python are not used: Node is the same pinned,
    hash-checked tarball as on Lambda (also for ``Runtime.NODE``), and the venv is built from
    ``python3.12``. So a step gets the same toolchain on both computes."""
    if runtime is Runtime.PYTHON:
        return python_prelude(ec2=ec2)
    if runtime is Runtime.NODE:
        return node_tarball_prelude(node_version) if ec2 else []
    if runtime is Runtime.TERRAFORM:
        return [*python_prelude(ec2=ec2), *terraform_prelude(terraform_version)]
    return [*node_tarball_prelude(node_version), *python_prelude(ec2=ec2)]


def join(*groups: Sequence[str]) -> list[str]:
    return [command for group in groups for command in group]
