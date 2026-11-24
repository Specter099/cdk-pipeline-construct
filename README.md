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
   -> Gates (every check, in parallel)
   -> Deploy (manual approval -> deploy -> post-deploy steps)
```

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
JSII_RUNTIME_PACKAGE_CACHE_ROOT=$TMPDIR/jsii-cache pytest -q   # env var only needed in a sandbox
```

`GitHubConnectionStack` creates the shared connection (retained). A new connection starts
`PENDING` and must be authorized once in the AWS console.
