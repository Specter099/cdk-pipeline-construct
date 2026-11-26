"""This repo's own CI: the library's PullRequestChecks, pointed at itself.

Every pull request to Specter099/cdk-pipeline-construct gets a commit status per check, from the
same webhook path that consuming repos use. Nothing here deploys anything, and the PR-check roles
hold only CloudWatch Logs and the GitHub connection token.

The ``ec2-smoke`` check doubles as the live test of EC2 compute: on the Amazon Linux 2023 standard
6.0 image it must find Python 3.12 (selected through pyenv) and the pinned Node 22.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from specter_pipeline import Ec2, Gate, PullRequestChecks, Runtime
from specter_pipeline.presets import PIP_AUDIT

REPO = "Specter099/cdk-pipeline-construct"
NAME_PREFIX = "cdk-pipeline-construct"
DEV_INSTALL = ["pip install -r requirements-dev.txt -c constraints-dev.txt"]

CI_GATES = [
    Gate("lint", ["ruff check ."], Runtime.PYTHON, install=DEV_INSTALL),
    # The tests import aws_cdk, whose jsii runtime needs Node. On EC2: the suite synthesizes
    # hundreds of pipelines and takes minutes; Lambda compute would kill it at 15.
    Gate("tests", ["pytest -q"], Runtime.PYTHON_NODE, install=DEV_INSTALL, ec2=Ec2("medium", 30)),
    Gate(
        "dependency-audit",
        ["pip-audit --skip-editable"],
        Runtime.PYTHON,
        # pip-audit audits the whole venv, pip included: the pip that venv bundles has known
        # advisories, so bring it up to date first.
        install=["python -m pip install --upgrade pip", *DEV_INSTALL, f"pip install {PIP_AUDIT}"],
    ),
    Gate(
        "ec2-smoke",
        [
            'python --version 2>&1 | grep -E "^Python 3\\.12\\." || { echo "not Python 3.12"; exit 1; }',
            'node --version | grep -E "^v22\\." || { echo "not the pinned Node 22"; exit 1; }',
            "command -v node | grep -q '^/tmp/nodejs/bin/node$' || { echo \"node is not the pinned tarball\"; exit 1; }",
            "python -c 'import sys; print(sys.executable)'",
        ],
        Runtime.PYTHON_NODE,
        ec2=Ec2("small", 15),
    ),
]


class LibraryCiStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, connection_arn: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.checks = PullRequestChecks(
            self,
            "PullRequestChecks",
            repo=REPO,
            base_branch="main",
            connection_arn=connection_arn,
            gates=CI_GATES,
            name_prefix=NAME_PREFIX,
        )
