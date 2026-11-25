"""Gate presets that reproduce the org's shared CDK review workflow.

``Specter099/.github``'s ``cdk-review.yml`` runs the same checks for every CDK repo: ruff,
bandit, unit tests, pip-audit, ``cdk synth``, IAM Access Analyzer and CDK Nag. This module
is the CodePipeline equivalent, so a repo that leaves GitHub Actions gets the same checks
with one call instead of re-deriving them:

    GATES = python_cdk_gates()

Build once: with ``assembly=True`` the checks read the cloud assembly the pipeline deploys instead
of synthesizing their own. Pair it with ``python_cdk_assembly()`` on a ``deploy_step`` pipeline::

    StandardPipeline(..., assembly=python_cdk_assembly(), gates=python_cdk_gates(assembly=True),
                     deploy_step=DeployStep("app", stacks=["MyStack"]))

The ``cdk-synth`` gate then only confirms the shared assembly exists (the assembly step is that
synth; on a pull request its PR check synthesizes the PR, so a broken synth is still caught before
merge), and ``access-analyzer`` scans the shared templates on the plain Python runtime.

``access-analyzer`` scans the top-level templates and every nested ``assembly-*`` directory (where
an ``application_stage`` pipeline's application stacks live), and fails if it found none.

Tool versions (bandit, pip-audit, boto3, cfn-flip) match the shared workflow; the CDK CLI is
pinned by this package instead (``DEFAULT_CDK_CLI_VERSION``), because the CLI must support the
cloud-assembly schema of the repo's ``aws-cdk-lib``. Pin ``aws-cdk-lib`` in the repo and bump both
together. Not reproduced (and why):

* CDK Nag: the shared workflow runs ``CDK_NAG=true cdk synth`` and fails the job if that synth
  fails. A repo that wires Nag to that variable must add its own gate; for a repo that does not
  read ``CDK_NAG`` the step is a no-op, so nothing is lost.
* Credentials for synth: the shared workflow runs ``cdk synth`` with an OIDC role; these gates have
  none, so a repo with uncached context lookups (``from_lookup``) fails ``cdk-synth``,
  ``access-analyzer`` and ``tests``. Commit ``cdk.context.json`` or avoid lookups.
* Missing files: the shared workflow skips tests when ``tests/`` is absent and the dev install when
  ``requirements-dev.txt`` is absent. These gates fail in both cases; pass ``tests=False`` or provide
  the file.
* Lint is skipped when ``ruff`` is not installed (as in the shared workflow), so put a pinned ruff
  in the dev requirements or the lint gate is a no-op.
* The PR diff comment needs a GitHub write token; the PR checks report a commit status per
  gate instead.
* CI log shipping to S3/CloudWatch: CodeBuild already writes every build to CloudWatch Logs.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence

from aws_cdk import aws_iam as iam

from .gate import Assembly, Gate
from .runtimes import DEFAULT_CDK_CLI_VERSION, Runtime, cdk_cli_install

# Versions the shared workflow pins.
BANDIT = "bandit==1.9.4"
PIP_AUDIT = "pip-audit==2.10.1"
BOTO3 = "boto3==1.43.102"
CFN_FLIP = "cfn-flip==1.3.0"

#: The Access Analyzer scan script lives in the shared repo. It is fetched at this commit and
#: verified against a committed hash (a hash downloaded from the same origin would only detect
#: corruption). To update: pick a new commit of Specter099/.github, copy the file's sha256, and
#: change both in a reviewed commit.
ACCESS_ANALYZER_REPO = "Specter099/.github"
ACCESS_ANALYZER_SCRIPT = "scripts/check_no_public_access.py"
ACCESS_ANALYZER_COMMIT = "52d2eef9ca4b1cf31427b17a517b7f4c4f89dafd"
ACCESS_ANALYZER_SHA256 = (
    "c5694eac6f3f276a12ac3e6abb325a8d823c86c8a48047e5b54de2b9fca9d4bd"
)

DEFAULT_BANDIT_EXCLUDES = ("./.venv", "./cdk.out", "./lambda_layer")


def scan_every_assembly(template_dir: str, scan: str) -> list[str]:
    """Run ``scan --template-dir <dir>`` on ``template_dir`` and on every nested ``assembly-*``
    directory under it (an application_stage pipeline's stacks live in
    ``cdk.out/assembly-<Stage>/``, an app's own Stages likewise), skipping directories without
    templates. Fails if no directory had any, so the gate cannot pass having scanned nothing."""
    d = shlex.quote(template_dir)
    empty = shlex.quote(f"no CloudFormation templates under {template_dir}")
    return [
        f"{{ printf '%s\\n' {d}; find {d} -mindepth 1 -type d -name 'assembly-*'; }} | sort -u > /tmp/aa/dirs",
        (
            'found=0; while IFS= read -r dir; do ls "$dir"/*.template.json >/dev/null 2>&1 || continue; '
            f'found=1; {scan} --template-dir "$dir" || exit 1; done < /tmp/aa/dirs; '
            f'[ "$found" = 1 ] || {{ echo {empty}; exit 1; }}'
        ),
    ]


def access_analyzer_commands(
    aws_region: str = "us-east-1", *, synth: bool = True, template_dir: str = "cdk.out"
) -> list[str]:
    url = f"https://raw.githubusercontent.com/{ACCESS_ANALYZER_REPO}/{ACCESS_ANALYZER_COMMIT}/{ACCESS_ANALYZER_SCRIPT}"
    return [
        *(["cdk synth --quiet"] if synth else []),
        # A dedicated directory and `python3 -I` keep /tmp (HOME, holding everything else) off sys.path.
        "mkdir -p /tmp/aa",
        f"curl -fsSL -o /tmp/aa/check_no_public_access.py {url}",
        f'echo "{ACCESS_ANALYZER_SHA256}  /tmp/aa/check_no_public_access.py" | sha256sum -c -',
        *scan_every_assembly(
            template_dir,
            "python3 -I /tmp/aa/check_no_public_access.py "
            f"--aws-region {shlex.quote(aws_region)} --fail-on-public-access",
        ),
    ]


def python_cdk_gates(
    *,
    tests: bool = True,
    access_analyzer: bool = True,
    runtime_requirements: str = "requirements.txt",
    dev_requirements: str = "requirements-dev.txt",
    bandit_excludes: Sequence[str] = DEFAULT_BANDIT_EXCLUDES,
    cdk_cli_version: str = DEFAULT_CDK_CLI_VERSION,
    aws_region: str = "us-east-1",
    env: Mapping[str, str] | None = None,
    test_memory_gb: int = 2,
    assembly: bool = False,
    assembly_dir: str = "cdk.out",
) -> list[Gate]:
    """The shared review checks as pipeline gates.

    ``assembly=True`` builds once: ``cdk-synth`` and ``access-analyzer`` read the pipeline's
    assembly from ``assembly_dir`` (default ``cdk.out``, right for both deploy modes) instead of
    synthesizing. On a ``deploy_step`` pipeline that is ``cdk.out``;
    with ``application_stage`` the stage's templates are in ``cdk.out/assembly-<StageName>``.

    ``env`` is added to the steps that run CDK or the tests (for example
    ``JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION``). The Access Analyzer gate has its own IAM
    (``access-analyzer:CheckNoPublicAccess``, which supports no resource-level scoping) and
    therefore does not run on pull requests.
    """
    extra = dict(env or {})
    runtime_deps = f"pip install -r {runtime_requirements}"
    dev_deps = f"pip install -r {dev_requirements}"
    cdk_install = cdk_cli_install(cdk_cli_version)

    gates = [
        Gate(
            "lint",
            [
                'if command -v ruff >/dev/null 2>&1; then ruff check .; else echo "ruff not installed - skipping lint"; fi'
            ],
            Runtime.PYTHON,
            install=[dev_deps],
        ),
        Gate(
            "sast",
            [f"bandit -r . -ll --exclude {','.join(bandit_excludes)}"],
            Runtime.PYTHON,
            install=[f"pip install {BANDIT}"],
        ),
    ]
    if tests:
        gates.append(
            Gate(
                "tests",
                # pytest exits 5 when nothing is collected; the shared workflow treats that as a skip.
                ["pytest tests/ -v || test $? -eq 5"],
                # The tests import aws_cdk, which runs a Node process (the Python image has none).
                Runtime.PYTHON_NODE,
                install=[runtime_deps, dev_deps],
                env=extra,
                memory_gb=test_memory_gb,
            )
        )
    gates.append(
        Gate(
            "dependency-audit",
            [f"pip-audit -r {runtime_requirements}"],
            Runtime.PYTHON,
            install=[f"pip install {PIP_AUDIT}"],
        )
    )
    if assembly:
        # Free in the pipeline (the artifact already exists). On a pull request the PR check of a
        # uses_assembly gate synthesizes first, so this keeps a synth on every PR: without it no
        # PR check would synthesize (access-analyzer has IAM and never runs on PRs).
        gates.append(
            Gate(
                "cdk-synth",
                [f"test -f {shlex.quote(assembly_dir)}/manifest.json"],
                Runtime.PYTHON,
                env=extra,
                uses_assembly=True,
            )
        )
    else:
        gates.append(
            Gate(
                "cdk-synth",
                ["cdk synth"],
                Runtime.PYTHON_NODE,
                install=[runtime_deps, *cdk_install],
                env=extra,
            )
        )
    if access_analyzer:
        analyzer_tools = f"pip install {BOTO3} {CFN_FLIP}"
        gates.append(
            Gate(
                "access-analyzer",
                access_analyzer_commands(aws_region, synth=not assembly, template_dir=assembly_dir),
                # Reading templates needs only Python; synthesizing them needs Node and the app.
                Runtime.PYTHON if assembly else Runtime.PYTHON_NODE,
                install=[analyzer_tools] if assembly else [runtime_deps, *cdk_install, analyzer_tools],
                env=extra,
                uses_assembly=assembly,
                policy_statements=[
                    # Resource "*" is required, not lazy: the AWS Service Authorization Reference lists
                    # CheckNoPublicAccess (Read) with no resource types and no condition keys. It is a
                    # stateless validator that never reads your resources.
                    iam.PolicyStatement(
                        actions=["access-analyzer:CheckNoPublicAccess"], resources=["*"]
                    )
                ],
            )
        )
    return gates


def python_cdk_assembly(
    *,
    runtime_requirements: str = "requirements.txt",
    env: Mapping[str, str] | None = None,
    memory_gb: int = 4,
) -> Assembly:
    """The assembly step for a Python CDK app: install its requirements, ``cdk synth``."""
    return Assembly(
        install=[f"pip install -r {runtime_requirements}"],
        commands=["cdk synth"],
        env=dict(env or {}),
        memory_gb=memory_gb,
    )
