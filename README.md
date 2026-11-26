# cdk-pipeline-construct

Reusable AWS CDK (Python) construct that gives every Specter099 repo the same
CI/CD pipeline: **CodePipeline + CodeBuild on Lambda compute**, source from
GitHub through a shared CodeConnections connection.

```python
from specter_pipeline import DeployStep, Gate, PostDeployStep, Runtime, StandardPipeline

StandardPipeline(
    self, "Pipeline",
    repo="Specter099/my-repo",
    connection_arn=connection_arn,
    gates=[
        Gate("unit", ["./scripts/ci-local.sh unit"], Runtime.PYTHON,
             install=["pip install -r requirements.txt"], memory_gb=4),
        Gate("lint", ["npm run lint"], Runtime.NODE, install=["npm ci"]),
    ],
    deploy_step=DeployStep("app", ["cdk deploy MyStack --require-approval never"],
                           runtime=Runtime.PYTHON_NODE, install=["pip install -r requirements.txt"]),
    synth_install=["pip install -r requirements-pipeline.txt"],
    synth_commands=['cdk -a "python3 pipeline_app.py" synth'],
    deploy_enabled=False,   # ship closed; flip in code via a reviewed PR
)
```

## What it builds

```
Source (main) -> Build (synth) -> UpdatePipeline (self-mutate)
   -> Gates (assembly step, then every check in parallel)
   -> Deploy (manual approval -> deploy -> post-deploy steps)
```

## Build once: the checked templates are the deployed templates

Without it, `cdk synth` runs separately in the synth gate, the Access Analyzer gate and the deploy,
each after its own `pip install`, so the template that passed the checks is not provably the one
that ships. With it, the application is synthesized once and every consumer reads that artifact.

```python
from specter_pipeline import DeployStep, StandardPipeline
from specter_pipeline.presets import python_cdk_assembly, python_cdk_gates

GATES = python_cdk_gates(assembly=True)          # cdk-synth and access-analyzer read cdk.out
StandardPipeline(
    self, "Pipeline", repo="Specter099/my-repo", connection_arn=connection_arn, gates=GATES,
    assembly=python_cdk_assembly(),                # pip install -r requirements.txt && cdk synth
    deploy_step=DeployStep("app", stacks=["MyStack"]),
    synth_commands=['cdk -a "python3 pipeline_app.py" synth'],
)
```

**What is guaranteed.** With an assembly, the deploy is an action only the construct writes:
* its only input is the assembly artifact, so there is no source checkout and no repo script in it;
* it copies the assembly to `/tmp/assembly`, and refuses an assembly that contains a `cdk.json`
  (the CLI would run that file's `build` hook before loading the snapshot);
* it installs the pinned CDK CLI and runs
  `/tmp/nodejs/bin/cdk deploy --app /tmp/assembly <stacks> <deploy_args>` from a fresh empty
  directory, by absolute path so a `PATH` change cannot swap the CLI;
* its only environment variable is `CI=true`. `DeployStep.env` goes to the `PreDeploy` action, so it
  cannot steer the CLI (for example with `NODE_OPTIONS`);
* it may assume the CDK bootstrap roles of the pipeline's own account and region, plus each
  `(account, region)` in `DeployStep(deploy_environments=[...])`, and nothing else;
* `--require-approval never` is added unless `deploy_args` sets `--require-approval`, and
  `deploy_args` may not set `--app`, `--build` or `--output` in any spelling the CLI accepts
  (`-va`, `--no-app`, camelCase).

Your `install` and `commands`, if any, run before it in a separate `PreDeploy-<name>` action, after
the approval. That action holds your `policy_statements` but cannot assume the CDK deploy roles, and
it cannot touch the deploy action's files. `assert_builds_once` checks the deploy action's inputs,
phases and exact command list. The standard contract runs it whenever the pipeline has an assembly
step.

**What is a guard, not a guarantee.** The `PreDeploy` commands may not mention the CDK CLI in any
spelling (`cdk`, `aws-cdk`, `cdk@2`, `.../bin/cdk`). This is a plain text match, so loops, comments,
backticks and wrappers cannot hide it. It does not see inside scripts or shell variables. A `cdk/`
directory in a path is allowed, but the bare word is refused (`cd cdk`, `npm ci --prefix cdk`); put
such work in a script. The guard is a second line of defence: without the deploy roles, a `cdk deploy`
there fails unless your own `policy_statements` grant CloudFormation access.

* `Assembly` runs first in the Gates stage on Python+Node with the pinned CDK CLI. Its `cdk.out` is
  linked into every gate with `Gate(uses_assembly=True)`, and is the deploy action's only input. A
  gate that reads it must name that directory in its commands, or the pipeline refuses it. Gates that do not
  read it start at the same time, so the stage is no slower.
* The assembly step takes no IAM permissions. Commit `cdk.context.json` instead of looking values up.
* With `application_stage` there is nothing to add: the Synth output is what the stage deploys, and
  `uses_assembly` gates read it.
* The preset's Access Analyzer gate scans the top-level templates and every nested `assembly-*`
  directory, so it covers a stage's stacks (`cdk.out/assembly-Prod/`) and an app's own stages. It
  follows the link CDK Pipelines puts at `cdk.out`, and fails if it finds no templates at all.
* A non-default assembly directory goes to both sides:
  `python_cdk_assembly(output_dir="infra/cdk.out")` with
  `python_cdk_gates(assembly=True, assembly_dir="infra/cdk.out")`. The contract fails when a gate
  never refers to the directory the assembly is linked at.
* On pull requests there is no pipeline assembly, so the PR project of a `uses_assembly` gate runs
  the assembly (or synth) commands first, with the assembly's (or synth's) memory. It also receives
  `Assembly.env` (or `synth_env`) beside the PR's code, so keep secrets out of both. The preset keeps
  a permission-free `cdk-synth` gate in assembly mode for this reason: every PR still synthesizes.
* Not yet proven on a live pipeline: deploying from the `/tmp/assembly` copy on Lambda compute.
  Try it once with `deploy_enabled=False`, then enable the deploy.

Pull requests are validated by **`PullRequestChecks`**: one CodeBuild project per
gate, triggered by a webhook, reporting a commit status per gate. This is a
separate path on purpose. A CodePipeline V2 pull-request trigger runs the whole
pipeline including self-mutation, which would let a PR branch rewrite the
production pipeline before any approval.

## Customising per repo (there is no user data)

Lambda compute has no instance, so there is no EC2 user data. The per-repo hooks are:

| Need | Where |
|---|---|
| Install tools or dependencies | `Gate(install=[...])` / `DeployStep(install=[...])` |
| Runtime and size | `runtime=Runtime.PYTHON \| NODE \| PYTHON_NODE`, `memory_gb=1/2/4/8/10` |
| Longer than 15 minutes | `ec2=Ec2(size="medium", timeout_minutes=90)` (see below) |
| Environment variables | `env={...}` (`CI=true` is always set) |
| Extra IAM for a step | `policy_statements=[...]` |
| Repo-specific logic | the repo's own script (e.g. `scripts/ci-local.sh`), called from `commands` |
| Inspect the deployed templates | `Gate(uses_assembly=True)` reads `cdk.out` |

## Runtimes (verified against live Lambda compute images)

| `Runtime` | Image | Notes |
|---|---|---|
| `PYTHON` | `python3.12` | Python 3.12, git, AWS CLI. No Node. A `/tmp/venv` is created and the image's pip config is neutralised (it breaks build isolation). |
| `NODE` | `nodejs22` | Node 22.22, npm. Python is 3.9, so don't use it for Python checks. |
| `PYTHON_NODE` | `python3.12` + Node tarball | Pinned Node downloaded to `/tmp`, SHA-256 verified. Use for `cdk synth` / `cdk deploy` of a Python app. |

## Deploying an application: stage vs `DeployStep`

* `application_stage=` deploys through a CDK Pipelines `Stage` (CloudFormation change sets).
  Fine for **new** stacks. **Do not** use it for a stack that is already live: wrapping it
  in a Stage changes the construct path embedded in Lambda permission and event-source
  mapping logical IDs, so CloudFormation replaces them on the first deploy.
* `deploy_step=` runs your own deploy command (`cdk deploy <stack>`) and keeps every
  logical ID. The step may only assume the CDK bootstrap roles.

Exactly one of the two is required.

## Presets: the shared CDK review checks as gates

`python_cdk_gates()` reproduces `Specter099/.github`'s `cdk-review.yml` for a repo that leaves
GitHub Actions: **ruff, bandit, unit tests, pip-audit, `cdk synth` and IAM Access Analyzer**, with
the same tool versions.

```python
from specter_pipeline.presets import python_cdk_gates
GATES = python_cdk_gates(env={"JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1"})
```

* The Access Analyzer gate fetches the shared script at a pinned commit and verifies a committed
  SHA-256 before running it. It has its own IAM (`access-analyzer:CheckNoPublicAccess`; the AWS
  reference lists no resource types, so `*` is required) and therefore **does not run on pull
  requests**: that path executes the PR head's code. `Gate(policy_statements=...)` followed by
  `pull_request=True` is refused.
* Not reproduced: CDK Nag (informational in the shared workflow), the PR diff comment (needs a
  GitHub write token; PR checks report a commit status per gate instead), CI log shipping
  (CodeBuild already writes every build to CloudWatch Logs).

## Terraform: static checks on every pull request

Step 2 of the Terraform pipeline. A Terraform repo's pipeline is still defined in CDK; these gates
check its root modules without AWS access, so they run on pull requests too.

```python
from specter_pipeline.presets import terraform_gates
GATES = terraform_gates(roots=["infra"])   # tf-fmt, tf-validate, tf-checkov
```

* `Runtime.TERRAFORM` is the Python image plus Terraform 1.17.2. The binary is downloaded from
  releases.hashicorp.com and checked against a committed SHA-256. That hash was copied from the
  release's `SHA256SUMS` after verifying the file's signature with HashiCorp's release key
  (C874 011F 0AB4 0511 0D02 1055 3436 5D94 72D7 468F). An unknown version is refused.
  `StandardPipeline(terraform_version=...)` picks among committed versions.
* `tf-fmt` runs `fmt -check -recursive`.
* `tf-validate` runs `init -backend=false -lockfile=readonly` and then `validate`, so it never reads
  state. The root must commit `.terraform.lock.hcl` (an empty file if it has no providers), and
  every provider in it must list hashes: `init` would otherwise install an unhashed provider as
  "unauthenticated". The lock file must keep the layout `terraform init` writes: unindented
  `provider "host[:port]/namespace/name" {` blocks, a `hashes = [` list with at least one entry,
  no comments inside a provider block and no other syntax. Regenerate it with `terraform providers lock` rather than
  editing it by hand; anything else is refused, so a comment cannot hide a missing hash. A committed `terraform.d/` or `.terraform/` is refused, because it could supply
  provider binaries.
* `tf-checkov` runs pinned Checkov with its built-in policies, `--skip-download` (no platform calls)
  and `--hard-fail-on 'CKV*'`. A `.checkov.yaml` or `.checkov.yml` in the root, the working
  directory or `$HOME` is refused, because such a file can switch the gate off. A missing root, a
  file Checkov cannot parse, or a `*.tf.json` file anywhere in the repository, symlinked
  directories included (Checkov 3.4.4 skips JSON configuration), fails the gate. Inline `#checkov:skip=` comments still work and show
  in the diff.
* TFLint is opt-in (`tflint=(version, sha256)`), because its releases are on GitHub and no hash has
  been verified for this package yet. It runs without `--init`, so only its bundled Terraform rules
  run; plugins named by the repo's `.tflint.hcl` would otherwise be downloaded unpinned. Trivy is
  not included: for Terraform it overlaps Checkov.
* What these gates can and cannot do. Their roles hold only what every PR check holds: its own logs,
  and the GitHub connection token used for the checkout. They never read Terraform state. But
  `tf-validate` and `tf-lint` execute provider plugins, and those come from the PR. A PR that
  changes providers has to change the lock file, so the change shows in its diff, but its plugins
  still run in the check. Keep the repo private, or require approval for PRs from forks (the
  default). Plan and apply, with state and credentials, come in step 3, on EC2 compute.

## Terraform: plan once, apply exactly that plan

Step 3. The pipeline is still CDK; the infrastructure is a Terraform root module.

```python
from specter_pipeline import StandardPipeline, TerraformRoot
from specter_pipeline.presets import terraform_gates

ROOT = TerraformRoot(
    "infra",
    plan_policy_statements=[...],    # what refresh and plan read
    apply_policy_statements=[...],   # what apply creates and changes
)
StandardPipeline(..., gates=terraform_gates(roots=["infra"]), terraform=ROOT,
                 synth_commands=['cdk -a "python3 pipeline_app.py" synth'])
```

```
Gates:  Plan-main (EC2)  ->  Gate-tf-plan-check-main      + the static gates, in parallel
Deploy: Approve -> Deploy-main (EC2: terraform apply tfplan) -> post-deploy steps
```

* The construct creates the state bucket: `{repo}-tfstate-{account}-{region}-an`, versioned,
  encrypted, retained, with state never expired. Each root gets its own key and lock file
  (`<name>/terraform.tfstate`), using Terraform's own S3 lock file (no DynamoDB). The root declares
  a partial backend, and the construct supplies the rest at `init`:

  ```hcl
  terraform {
    backend "s3" {}
  }
  ```

  Plan and Deploy both stop if `init` did not end up on that backend. A root without the block
  would otherwise use local state on the build machine and create everything again on every run.
  The check covers the backend type, bucket, key and workspace prefix. Other settings in the
  root's backend block, such as a custom endpoint, `assume_role` or a profile, are code on main
  and are not second-guessed.
* `Plan-<name>` runs `plan -out=tfplan` and saves `show -json` and a text rendering. It can read
  state and hold the lock, but not write or delete state. Its output is the checkout plus the saved
  plan.
* `Gate-tf-plan-check-<name>` reads only the plan, with no access to the state bucket. It fails on
  any delete or replace unless `allow_destroy=True`, prints `forget` entries (from `removed`
  blocks) prominently, and runs Checkov's plan checks hardened as in the static gate. Reviewed
  inline `#checkov:skip=` comments apply here too: the checkout travels with the plan, and Checkov
  reads them from it (`--repo-root-for-plan-enrichment`).
* `Deploy-<name>` belongs to the construct. Its only input is the plan artifact. It re-initialises
  against the committed lock file and runs exactly `terraform apply tfplan`. Only this role can
  write state.
* Plan and apply run on EC2 (`plan_ec2`, `apply_ec2`; defaults are small with 60 and 120 minutes),
  so a long apply is not killed half-way.
* `TerraformRoot.env` and `secrets` may not set variables that redirect Terraform, the AWS SDK or
  CodeBuild: `TF_CLI_ARGS*`, `TF_WORKSPACE`, `TF_DATA_DIR`, `TF_CLI_CONFIG_FILE` and similar,
  `TF_LOG*` (debug logs can print secrets), `AWS_*`, `CODEBUILD_*`, `PATH`, `HOME` and `CI`.
* `assert_standard_contract(..., terraform=ROOT)` checks all of this, via `assert_applies_plan` and
  `assert_state_bucket`: exact command lists, environment variables, artifact wiring, and IAM with
  wildcards taken into account.
  * The Plan, plan-check and Deploy roles are read strictly: no managed policies, no `NotAction`.
  * Every other role, user and group, and any policy attached to a principal outside the
    template, is read leniently. That covers inline and managed policies of the template,
    `RolePolicy`/`UserPolicy`/`GroupPolicy`, `NotAction`/`NotResource`, explicit denies, and
    `Fn::Sub`/`Fn::Join`/parameter defaults. A construction it cannot read (an `Fn::If`, an unknown
    intrinsic, a parameter without a default or read from SSM) counts as reaching the state, and
    never as a Deny or `NotResource` that excludes it. So a grant on
    `Bucket.from_bucket_arn(CfnParameter)` with no default fails the contract: give the parameter
    a default, or keep such grants out of the pipeline stack.
  * Only the Deploy role may write the state or change the bucket (policy, versioning,
    lifecycle, public access, encryption, deletion).
  * **Not covered:** managed policies attached by a literal ARN (AWS-managed ones such as
    `AdministratorAccess`, or another stack's) cannot be read from the template, and principals
    in other stacks or accounts are not visible. Keep those off the state bucket in your account.

**Operating notes.**
* **Secrets in the plan:** the saved plan and `tfplan.json` contain the prior state and any
  sensitive values in clear text, and the pipeline's artifact bucket is readable by every step.
  Keep secrets out of Terraform variables where you can, for example by reading them at apply time
  from Secrets Manager inside the resource that needs them.
* **Stale plans:** a plan is applied only against the state it was made from. If another run
  changes the state first (two releases close together), the later apply fails with "Saved plan
  is stale"; release again to plan afresh. Retrying the stage re-applies the same stale plan.
* **Failed state writes:** if apply fails after creating resources but before writing state,
  Terraform saves `errored.tfstate`, and the Deploy step pushes it to the backend before failing.
* **A stuck lock:** a cancelled Plan or Deploy can leave `<name>/terraform.tfstate.tflock`. Clear it
  with `terraform force-unlock <id>` from a machine with the deploy role, or delete the lock object
  once you are sure no run is in progress.
* **Renaming a root starts it on empty state:** the key is `<name>/terraform.tfstate`. Move the
  state object first.
* Not yet proven on a real account: the state bucket permissions, including the very first run with
  no state, and EC2 compute itself. Test both once with `deploy_enabled=False`.

## Terraform: several roots, and credentials for other providers

Step 4. `terraform=` also takes a list:

```python
GH = "arn:aws:secretsmanager:us-east-1:111122223333:secret:github-token-AbC123"
ROOTS = [
    TerraformRoot("network", name="network", plan_policy_statements=[...], apply_policy_statements=[...]),
    TerraformRoot("github", name="github", secrets={"GITHUB_TOKEN": GH}),
]
StandardPipeline(..., gates=terraform_gates(roots=["network", "github"]), terraform=ROOTS)
```

* Every root plans and is checked in the Gates stage. Then one approval covers all of them, and the
  applies run one at a time, in the order given. Each root has its own state key and lock in the
  repo's one state bucket, and its own Plan and Deploy roles.
* **Roots must be independent.** All plans are made before any apply. A root that reads another
  root's outputs (`terraform_remote_state`) would be planned against the outputs from before this
  release's applies, and Terraform would not flag it. Put dependent roots in separate pipelines,
  or release them in two steps.
* `secrets` maps environment variable names to Secrets Manager secret ARNs. Each ARN must be
  complete, with its random suffix; CodeBuild's `:json-key:version-stage:version-id` selectors are
  allowed. They go into the buildspec's `env.secrets-manager` of that root's Plan and Deploy only,
  and those two roles get `secretsmanager:GetSecretValue` on exactly those ARNs. (As CodeBuild
  project variables, CDK would add its own grant on `<arn>*`.) The static gates, the PR checks
  and the plan check never see a secret. Use it for providers such as GitHub (`GITHUB_TOKEN`) or
  Equinix (`METAL_AUTH_TOKEN`).
  * Check the ARN is copied whole. The construct cannot tell a real six-character suffix from a
    name that happens to end in a hyphen and six letters (`secret:github-tokens`), and IAM
    matches a grant on a suffix-less name against nothing.
  * `TF_VAR_*` names are refused: Terraform writes input variable values into the saved plan,
    which every pipeline step can read. Use the provider's own environment variable.
  * Several roots may share a secret; each one's Plan and Deploy may read it.
* The contract (`terraform=ROOTS`) checks each root as before, plus the apply order, that only a
  root's own Plan and Deploy can read its state or touch its lock, and that only the roots
  declaring a secret can read it, with no wider secret grant on those roles.

## Contract tests: `specter_pipeline.testing`

Every consumer must prove the same things about its pipeline. Instead of copying a test file:

```python
from specter_pipeline import testing as contract

def test_pipeline(template):
    contract.assert_standard_contract(template, repo="Specter099/my-repo", gates=GATES,
                                      node_gates=["tests", "cdk-synth"])
```

Pass `assembly=`, `deploy_step=` and `post_deploy=` too, so that each step's expected runtime and
compute are known and its pinned toolchain is checked exactly. `post_deploy=` is required whenever
the pipeline has post-deploy steps: an undeclared `Post-*` step fails the contract instead of
skipping its checks.

It checks Lambda compute everywhere, the stage order, the gates, build-once (whenever the pipeline has an
assembly step or a gate reads the assembly), the deploy closed (or open),
approval strictly before deploy before post-deploy, no pipeline triggers, restart on update, the
account-regional artifact bucket, the PR-check filter and least-privilege roles, and Node on the
gates that need it. Each helper also exists on its own, and each is tested against a pipeline that
violates it. Add repo-specific checks next to it (for example: the deploy command equals the CI
workflow's, flag for flag and `-c` for `-c`).

## Safety defaults

* `deploy_enabled=False` writes `DisableInboundStageTransitions` into the pipeline
  template, so the first execution (which starts when the pipeline is created) cannot
  deploy. Enabling is a code change, re-applied by self-mutation.
* Approval strictly precedes the deploy (explicit step dependencies; a test asserts the
  `RunOrder`).
* The artifact bucket is named `{repo}-pipeline-{account}-{region}-an`, encrypted,
  versioned, TLS-only, public access blocked, 30-day expiry.
* Pipeline failures publish to an SNS topic.

## Lambda compute limits to design around

No Docker, no root, writes only under `/tmp`, **15-minute hard limit**, no caching, no
custom timeouts, no VPC.

## EC2 compute for long steps

A step that can outlive 15 minutes opts into EC2 compute, one step at a time:

```python
from specter_pipeline import Ec2, Gate

Gate("tf-plan", ["terraform plan -out=tfplan"], ec2=Ec2(size="medium", timeout_minutes=90))
```

* Any step takes `ec2=`: `Gate`, `Assembly`, `DeployStep` (including the construct-only deploy
  action) and `PostDeployStep`. Everything else, including CDK Pipelines' own projects, stays on
  Lambda.
* Sizes are CodeBuild's: `small` (2 vCPU, 3 GB), `medium` (4, 7), `large` (8, 15), `xlarge`,
  `2xlarge`. `timeout_minutes` runs from 5 to 480 (CodePipeline's limit for a CodeBuild action) and
  defaults to 60. Choose it from the worst case: a timeout during an apply does the same damage as
  Lambda's limit. `memory_gb` is ignored on EC2; the size sets memory.
* The image is Amazon Linux 2023 standard 6.0 (`aws/codebuild/amazonlinux-x86_64-standard:6.0`),
  never privileged. The image's own Node and Python are not used. Node is the same pinned,
  hash-checked tarball as on Lambda, including for `Runtime.NODE` and the deploy action. The venv is
  built from `python3.12`, and the build stops if the image has none. So a step gets the same
  toolchain on both computes. What differs: EC2 builds run as root with `HOME=/root`, and AWS
  patches the image under the same tag.
* An EC2 gate's PR check runs on EC2 too. The PR check of a `uses_assembly` gate synthesizes first,
  so when the assembly step is on EC2 it gets the larger of the two sizes and both timeouts added
  together (at most 480). A Lambda side counts as its EC2 equal: at least its memory, and 15
  minutes. EC2 PR checks have no concurrency limit, because CodeBuild drops builds over a limit
  instead of queueing them, which would leave a PR without its status. They do run the PR's code
  as root for up to their timeout, so keep PR-facing EC2 gates' timeouts tight.
* `DeployStep(ec2=...)` applies to its `PreDeploy` action as well as the deploy action.
* Not yet: caching, VPC, Docker. The contract still refuses all three.
* The contract expects EC2 exactly where it is declared, and infers it from the steps. Gates with
  `ec2=` are inferred automatically. Pass `assembly=` and `deploy_step=` to
  `assert_standard_contract` and their EC2 steps are inferred too, including the PR checks of
  `uses_assembly` gates. Name anything else in `ec2_steps=[...]`. An undeclared EC2 project fails,
  and so does a declared name that matches more than one project.
* Python on EC2: the 6.0 image installs several Pythons through pyenv but activates only 3.14. EC2
  steps select 3.12 with `PYENV_VERSION="$PYTHON_312_VERSION"`, run it to prove it is 3.12, and stop
  with a clear message otherwise. This is from the image's published Dockerfile, not yet from a live
  build.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check specter_pipeline tests
JSII_RUNTIME_PACKAGE_CACHE_ROOT=$TMPDIR/jsii-cache pytest -q   # env var only needed in a sandbox
```

`GitHubConnectionStack` creates the shared connection (retained). A new connection starts
`PENDING` and must be authorized once in the AWS console.
