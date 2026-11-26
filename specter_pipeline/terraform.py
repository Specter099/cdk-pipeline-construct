"""Terraform in a StandardPipeline: plan once, check the plan, apply exactly that plan.

The pipeline itself is still CDK (it updates itself from the repo's ``pipeline_app.py``); the
infrastructure it deploys is a Terraform root module::

    StandardPipeline(..., gates=terraform_gates(roots=["infra"]),
                     terraform=TerraformRoot("infra", plan_policy_statements=[...],
                                             apply_policy_statements=[...]))

Shape, mirroring the CDK build-once design:

* ``Plan-<name>`` (Gates stage, EC2): ``init`` against the construct's state bucket, then
  ``plan -out=tfplan``, ``show -json``. Its output is the checkout plus the saved plan (without
  ``.terraform``). Plan can read state and hold the lock; it cannot write state.
* ``Gate-tf-plan-check-<name>`` (Gates stage, Lambda, no AWS access): reads the plan JSON. Fails
  on any delete or replace unless ``allow_destroy=True``; runs Checkov's plan checks.
* ``Deploy-<name>`` (Deploy stage, after approval, EC2): construct-only. Its only input is the
  plan artifact. It re-initialises against the committed lock file and runs exactly
  ``terraform apply tfplan``; Terraform refuses a saved plan whose state has moved on.

What the construct cannot promise: Terraform itself runs the configuration's code during plan and
apply (provider plugins, ``external`` data sources, ``local-exec`` provisioners). That code comes
from the reviewed main branch, as the CDK app's does.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence

from aws_cdk import aws_iam as iam

from .compute import Ec2
from .gate import BASE_ENV, validate_name
from .presets import (
    checkov_commands,  # the same hardened Checkov run as the static gate
)

#: The saved plan and its renderings, written next to the root's configuration.
PLAN_FILE = "tfplan"
PLAN_JSON = "tfplan.json"
PLAN_TEXT = "tfplan.txt"

#: Checkov for the plan checks; the same pin as the static gate.
CHECKOV = "checkov==3.4.4"

#: A complete secret ARN (name plus Secrets Manager's 6-character suffix), optionally followed by
#: CodeBuild's :json-key:version-stage:version-id selectors.
_SECRET_ARN = re.compile(
    r"(arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6})"
    r"(:[A-Za-z0-9/_+=.@-]*){0,3}"
)


def secret_base_arn(arn: str) -> str:
    """The secret's own ARN, without CodeBuild's json-key/version selectors."""
    m = _SECRET_ARN.fullmatch(arn)
    assert m, arn
    return m.group(1)


#: Environment variables a root may not set: each redirects init, plan, apply or the state.
_RESERVED_ENV = frozenset(
    {"TF_WORKSPACE", "TF_DATA_DIR", "TF_STATE_BUCKET", "TF_CLI_CONFIG_FILE", "TF_PLUGIN_CACHE_DIR",
     "TF_IN_AUTOMATION", "TF_INPUT", "TF_LOG_PATH", "TF_REGISTRY_DISCOVERY_RETRY",
     # debug logs can print credentials and secret values into the build log
     "TF_LOG", "TF_LOG_CORE", "TF_LOG_PROVIDER",
     # these redirect the shell, the AWS SDK (the backend and the AWS provider) or CodeBuild
     "PATH", "HOME", "CI"}
)
#: Prefixes a root may not set: AWS SDK settings and credentials, and CodeBuild's own variables
#: (which CodeBuild refuses at deploy time anyway).
_RESERVED_PREFIXES = ("TF_CLI_ARGS", "AWS_", "CODEBUILD_")
#: AWS SDK settings that only tune it (region and retries), not credentials or endpoints.
_ALLOWED_AWS = frozenset(
    {"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE"}
)


def _root_path(path: str) -> str:
    norm = posixpath.normpath(path)
    if (
        not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", path or "")
        or path.startswith("/")
        or ".." in path.split("/")
        or norm.startswith("-")
    ):
        raise ValueError(f"terraform root must be a relative path below the source, got {path!r}")
    return norm


@dataclasses.dataclass(frozen=True)
class TerraformRoot:
    """One Terraform root module deployed by the pipeline.

    The root must declare a partial S3 backend::

        terraform {
          backend "s3" {}
        }

    The construct supplies bucket, key, region, ``use_lockfile`` and ``encrypt`` at ``init``, and
    both Plan and Deploy stop if ``init`` did not end up on that backend (a root without the
    block would otherwise use local state and re-create everything on every run). It must commit
    ``.terraform.lock.hcl`` (an empty file if it has no providers). ``plan_policy_statements`` are
    what refresh and plan need (read access); ``apply_policy_statements`` what apply needs. State
    access is added by the construct.

    The state key is ``<name>/terraform.tfstate``: renaming a root starts it on empty state, so
    the next plan would create everything again.
    """

    path: str = "."
    name: str = "main"
    plan_policy_statements: Sequence[iam.PolicyStatement] = ()
    apply_policy_statements: Sequence[iam.PolicyStatement] = ()
    var_files: Sequence[str] = ()
    allow_destroy: bool = False
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: Credentials for non-AWS providers (GitHub, Equinix, ...): environment variable name ->
    #: Secrets Manager secret ARN (complete, with its random suffix; CodeBuild's
    #: ``:json-key:version-stage:version-id`` suffixes are allowed). Exposed to this root's Plan
    #: and Deploy only; nothing else in the pipeline can read them.
    secrets: Mapping[str, str] = dataclasses.field(default_factory=dict)
    plan_ec2: Ec2 = dataclasses.field(default_factory=lambda: Ec2("small", 60))
    apply_ec2: Ec2 = dataclasses.field(default_factory=lambda: Ec2("small", 120))

    def __post_init__(self) -> None:
        validate_name("terraform root", self.name)
        object.__setattr__(self, "path", _root_path(self.path))
        for field in ("var_files", "plan_policy_statements", "apply_policy_statements"):
            value = getattr(self, field)
            if isinstance(value, str):
                raise TypeError(f"terraform root {self.name!r}: {field} is a list")
            object.__setattr__(self, field, tuple(value))
        object.__setattr__(self, "var_files", tuple(_root_path(v) for v in self.var_files))
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "secrets", dict(self.secrets))
        for key, arn in self.secrets.items():
            # upper-case, or Terraform's TF_TOKEN_<host> credential (host in lower case)
            named = re.fullmatch(r"[A-Z_][A-Z0-9_]*|TF_TOKEN_[A-Za-z0-9_]+", key)
            if not named or key in self.env or key in BASE_ENV:
                raise ValueError(
                    f"terraform root {self.name!r}: secret name {key!r} must be an upper-case "
                    "variable name not also in env"
                )
            if key.startswith("TF_VAR_"):
                raise ValueError(
                    f"terraform root {self.name!r}: secret {key!r} would be a Terraform input "
                    "variable, and Terraform writes input variable values into the saved plan, "
                    "which every pipeline step can read. Pass provider credentials through the "
                    "provider's own environment variable instead"
                )
            if not _SECRET_ARN.fullmatch(arn):
                raise ValueError(
                    f"terraform root {self.name!r}: secret {key!r} must be a complete Secrets "
                    f"Manager secret ARN, got {arn!r}"
                )
        for field, keys in (("env", self.env), ("secrets", self.secrets)):
            steering = sorted(
                k for k in keys
                if (k.startswith(_RESERVED_PREFIXES) and k not in _ALLOWED_AWS) or k in _RESERVED_ENV
            )
            if steering:
                raise ValueError(
                    f"terraform root {self.name!r}: {field} may not set {steering}: they change "
                    "what init, plan or apply do, where state goes, or what the build log shows"
                )
        for field in ("plan_ec2", "apply_ec2"):
            if not isinstance(getattr(self, field), Ec2):
                raise TypeError(f"terraform root {self.name!r}: {field} must be an Ec2(...)")
        if not isinstance(self.allow_destroy, bool):
            raise TypeError(f"terraform root {self.name!r}: allow_destroy must be True or False")

    @property
    def state_key(self) -> str:
        return f"{self.name}/terraform.tfstate"

    @property
    def environment(self) -> dict[str, str]:
        return {**BASE_ENV, **self.env}


def init_command(root: TerraformRoot) -> str:
    """``init`` against the construct's state bucket and the committed provider lock file."""
    return " ".join(
        [
            "terraform init -input=false -no-color -lockfile=readonly",
            '-backend-config="bucket=$TF_STATE_BUCKET"',
            f"-backend-config={shlex.quote('key=' + root.state_key)}",
            '-backend-config="region=$AWS_REGION"',
            "-backend-config=use_lockfile=true",
            "-backend-config=encrypt=true",
        ]
    )


def backend_check(root: TerraformRoot) -> str:
    """Fail unless init configured the S3 backend on the pipeline's bucket and this root's key."""
    script = (
        "import json, os, sys\n"
        "hint = 'declare an empty S3 backend in the root: terraform {\\n  backend \"s3\" {}\\n}'\n"
        "try:\n"
        "    b = json.load(open('.terraform/terraform.tfstate')).get('backend') or {}\n"
        "except FileNotFoundError:\n"
        "    print('init configured no backend, so state would stay on this machine. ' + hint); sys.exit(1)\n"
        "c = b.get('config') or {}\n"
        "ok = (b.get('type') == 's3' and c.get('bucket') == os.environ['TF_STATE_BUCKET']\n"
        f"      and c.get('key') == {root.state_key!r} and not c.get('workspace_key_prefix'))\n"
        "if not ok:\n"
        "    print('init did not configure the S3 backend on the pipeline state bucket. ' + hint); sys.exit(1)\n"
    )
    return f"python3 -I -c {shlex.quote('exec(' + repr(script) + ')')}"


def plan_commands(root: TerraformRoot) -> list[str]:
    var_files = " ".join(f"-var-file={shlex.quote(v)}" for v in root.var_files)
    lock = shlex.quote(
        f"{root.path}: commit .terraform.lock.hcl (an empty file if the root has no providers)"
    )
    return [
        f"cd {shlex.quote(root.path)}",
        f"test -f .terraform.lock.hcl || {{ echo {lock}; exit 1; }}",
        # A committed errored.tfstate would be pushed to the backend by Deploy's failure handling.
        "test ! -e errored.tfstate || { echo 'errored.tfstate is committed; remove it'; exit 1; }",
        init_command(root),
        backend_check(root),
        f"terraform plan -input=false -no-color -lock-timeout=5m {var_files} -out={PLAN_FILE}".replace("  ", " "),
        f"terraform show -json {PLAN_FILE} > {PLAN_JSON}",
        # Not `| tee`: a pipe would hide a failing show.
        f"terraform show -no-color {PLAN_FILE} > {PLAN_TEXT}",
        f"cat {PLAN_TEXT}",
        # Providers are re-fetched at apply against the lock file; keep the artifact small.
        "rm -rf .terraform",
    ]


def apply_commands(root: TerraformRoot) -> list[str]:
    """Exactly: into the root, re-initialise, apply the saved plan."""
    stale = shlex.quote(
        "apply failed. 'Saved plan is stale' means another run changed the state since this plan: "
        "release again to plan afresh"
    )
    return [
        f"cd {shlex.quote(root.path)}",
        f"test -f {PLAN_FILE} || {{ echo 'no saved plan in the artifact'; exit 1; }}",
        init_command(root),
        backend_check(root),
        # Only an errored.tfstate written by this apply may be pushed.
        "rm -f errored.tfstate",
        (
            f"rc=0; terraform apply -input=false -no-color -lock-timeout=5m {PLAN_FILE} || rc=$?; "
            # A failed state write leaves the real state in errored.tfstate, on this machine only.
            'if [ "$rc" -ne 0 ] && [ -f errored.tfstate ]; then '
            "terraform state push errored.tfstate && echo 'pushed errored.tfstate to the backend'; fi; "
            f'if [ "$rc" -ne 0 ]; then echo {stale}; fi; '
            '[ "$rc" -eq 0 ]'
        ),
    ]


#: Reads the plan JSON; prints a summary; fails on deletes/replaces unless allowed.
_DESTROY_CHECK = r"""
import json, sys
plan = json.load(open(sys.argv[1]))
allow = sys.argv[2] == "allow"
if "format_version" not in plan or not isinstance(plan.get("resource_changes", []), list):
    print("not a Terraform plan JSON")
    sys.exit(1)
counts, destroys, forgets = {}, [], []
for rc in plan.get("resource_changes", []):
    actions = rc.get("change", {}).get("actions", [])
    key = "replace" if set(actions) >= {"delete", "create"} else "+".join(actions)
    counts[key] = counts.get(key, 0) + 1
    if "delete" in actions:
        destroys.append(f"{key}: {rc.get('address')}")
    if "forget" in actions:
        forgets.append(f"FORGET (removed from state, left running): {rc.get('address')}")
print("plan summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes")
for d in destroys + forgets:
    print("  " + d)
if destroys and not allow:
    print(f"{len(destroys)} resource(s) would be deleted or replaced; set allow_destroy=True to permit")
    sys.exit(1)
""".strip()


def plan_check_commands(root: TerraformRoot, plan_dir: str) -> list[str]:
    plan_json = shlex.quote(posixpath.join(plan_dir, PLAN_JSON))
    allow = "allow" if root.allow_destroy else "deny"
    return [
        # First: Checkov exits 0 on a missing or unreadable file.
        f"python3 -I -c {shlex.quote('exec(' + repr(_DESTROY_CHECK) + ')')} {plan_json} {allow}",
        # --repo-root-for-plan-enrichment: the checkout is in the plan artifact, so reviewed
        # inline `#checkov:skip=` comments apply to the plan as they do to the static gate.
        *checkov_commands(
            plan_dir,
            shlex.quote(plan_dir),
            f"-f {plan_json} --framework terraform_plan "
            f"--repo-root-for-plan-enrichment {shlex.quote(plan_dir)}",
        ),
    ]


def secrets_buildspec(root: TerraformRoot) -> dict:
    """The buildspec ``env`` block that hands the root's secrets to the build. Given here, not as
    CodeBuild project variables: for those, CDK adds its own grant on ``<arn>*`` (and KMS
    decrypt on every key of another account), wider than ``secrets_policy``."""
    return {"env": {"secrets-manager": dict(sorted(root.secrets.items()))}} if root.secrets else {}


def secrets_policy(root: TerraformRoot) -> list[iam.PolicyStatement]:
    """Read access to exactly this root's provider secrets."""
    if not root.secrets:
        return []
    return [
        iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=sorted({secret_base_arn(a) for a in root.secrets.values()}),
        )
    ]


def state_policy(bucket_arn: str, root: TerraformRoot, *, write_state: bool) -> list[iam.PolicyStatement]:
    """Plan: read state, hold the lock. Apply: also write state. Nothing else in the bucket."""
    key = f"{bucket_arn}/{root.state_key}"
    statements = [
        # No prefix condition: Terraform lists with prefix=env:/, and a HeadObject on a missing
        # state key needs ListBucket to get 404 (first run) instead of 403. The bucket holds only
        # this pipeline's state.
        iam.PolicyStatement(actions=["s3:ListBucket"], resources=[bucket_arn]),
        iam.PolicyStatement(actions=["s3:GetObject"], resources=[key]),
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], resources=[f"{key}.tflock"]
        ),
    ]
    if write_state:
        statements.append(iam.PolicyStatement(actions=["s3:PutObject"], resources=[key]))
    return statements
