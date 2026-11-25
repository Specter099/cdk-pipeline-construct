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
from specter_pipeline import Assembly, DeployStep, StandardPipeline
from specter_pipeline.presets import python_cdk_assembly, python_cdk_gates

GATES = python_cdk_gates(assembly=True)          # no cdk-synth gate; access-analyzer reads cdk.out
StandardPipeline(
    self, "Pipeline", repo="Specter099/my-repo", connection_arn=connection_arn, gates=GATES,
    assembly=python_cdk_assembly(),                # pip install -r requirements.txt && cdk synth
    deploy_step=DeployStep("app", ["cdk deploy --app cdk.out MyStack --require-approval never"]),
    synth_commands=['cdk -a "python3 pipeline_app.py" synth'],
)
```

* `Assembly` runs first in the Gates stage on Python+Node with the pinned CDK CLI. Its `cdk.out` is
  linked into every gate with `Gate(uses_assembly=True)` and into the deploy step. Gates that do not
  read it start at the same time, so the stage is no slower.
* A `cdk deploy` in the deploy step without `--app cdk.out` is refused at synth time: it would
  synthesize again. A deploy script is not inspected, so `assert_builds_once` checks the result.
* The assembly step takes no IAM permissions. Commit `cdk.context.json` instead of looking values up.
* With `application_stage` there is nothing to add: the Synth output is what the stage deploys, and
  `uses_assembly` gates read it. The stage's templates are under `cdk.out/assembly-<StageName>/`,
  so pass `python_cdk_gates(assembly=True, assembly_dir="cdk.out/assembly-Prod")`.
* On pull requests there is no pipeline assembly, so the PR project of a `uses_assembly` gate runs
  the assembly (or synth) commands first.
* Not yet proven on a live pipeline: that `cdk deploy --app cdk.out` works when `cdk.out` is the
  linked artifact (CDK Pipelines links it as a symlink and the CLI writes a lock file into it).

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

## Contract tests: `specter_pipeline.testing`

Every consumer must prove the same things about its pipeline. Instead of copying a test file:

```python
from specter_pipeline import testing as contract

def test_pipeline(template):
    contract.assert_standard_contract(template, repo="Specter099/my-repo", gates=GATES,
                                      node_gates=["tests", "cdk-synth"])
```

It checks Lambda compute everywhere, the stage order, the gates, build-once (when any gate reads the
assembly), the deploy closed (or open),
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
custom timeouts, no VPC. A repo that needs any of these should run on EC2 compute (not
implemented yet).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check specter_pipeline tests
JSII_RUNTIME_PACKAGE_CACHE_ROOT=$TMPDIR/jsii-cache pytest -q   # env var only needed in a sandbox
```

`GitHubConnectionStack` creates the shared connection (retained). A new connection starts
`PENDING` and must be authorized once in the AWS console.
