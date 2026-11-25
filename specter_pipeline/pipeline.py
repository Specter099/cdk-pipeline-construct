"""StandardPipeline: the shared CI/CD pipeline every Specter099 repo uses.

Shape (main branch only; PRs are handled by ``PullRequestChecks``)::

    Source (CodeConnections)  ->  Build (synth)  ->  UpdatePipeline (self-mutate)
        ->  Gates (all checks, in parallel)
        ->  <Stage> (manual approval -> deploy the application -> post-deploy steps)

Design rules baked in:

* Every CodeBuild project -- including CDK Pipelines' own synth, self-mutation
  and asset-publishing projects -- runs on Lambda compute.
* The deploy stage can be held closed from the pipeline's own template
  (``deploy_enabled=False`` => ``DisableInboundStageTransitions``), so the very
  first execution, which starts as soon as the pipeline is created, cannot
  deploy. Flip it in code via a reviewed change rather than in the console, so
  the pipeline's definition and its live state cannot disagree. (CloudFormation
  only acts on a property *diff*, so a manual console change is not reliably
  reverted by a later no-change update.)
* The artifact bucket has an explicit ``{purpose}-{account}-{region}-an`` name.
* Build once: the cloud assembly that is deployed is the one the gates inspect. With
  ``application_stage`` that is the Synth output; with ``deploy_step`` an ``Assembly`` step
  builds it at the start of the Gates stage and the deploy runs ``cdk deploy --app cdk.out``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import pipelines
from constructs import Construct

from .gate import (
    BASE_ENV,
    Assembly,
    DeployStep,
    Gate,
    PostDeployStep,
    _Step,
    assembly_deploy_violations,
    compute_type,
)
from .pull_request import PullRequestChecks
from .runtimes import (
    DEFAULT_CDK_CLI_VERSION,
    DEFAULT_NODE_VERSION,
    Runtime,
    cdk_cli_install,
    join,
    prelude,
)

#: Pipeline stage name used when deploying through a ``DeployStep`` (with an
#: ``application_stage`` the stage's own name is used instead).
DEPLOY_WAVE = "Deploy"

# S3 bucket names are max 63 chars: purpose + "-" + 12 (account) + "-" + <=14
# (longest region, ap-southeast-N) + "-an".
_MAX_PURPOSE_LEN = 63 - len("-an") - 1 - 14 - 1 - 12


def artifact_bucket_purpose(name: str) -> str:
    # House rule: replace dots with hyphens. S3 bucket names are lowercase.
    purpose = f"{name.lower().replace('.', '-')}-pipeline"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", purpose):
        raise ValueError(f"pipeline name {name!r} cannot form a valid S3 bucket name")
    if len(purpose) > _MAX_PURPOSE_LEN:
        raise ValueError(
            f"pipeline name {name!r} is too long for the artifact bucket naming pattern "
            f"({len(purpose)} > {_MAX_PURPOSE_LEN} chars)"
        )
    return purpose


def _environment(runtime: Runtime, memory_gb: int) -> codebuild.BuildEnvironment:
    return codebuild.BuildEnvironment(
        build_image=runtime.image, compute_type=compute_type(memory_gb)
    )


def _codebuild_step(
    step_id: str,
    spec: _Step,
    *,
    source: pipelines.IFileSetProducer,
    node_version: str,
    cdk_cli_version: str | None = None,
    extra_policies: Sequence[iam.PolicyStatement] = (),
    additional_inputs: dict[str, pipelines.IFileSetProducer] | None = None,
    primary_output_directory: str | None = None,
) -> pipelines.CodeBuildStep:
    """The one way a step becomes a CodeBuild action: runtime prelude, optional CDK CLI,
    the step's install and commands, CI env, its IAM, Lambda compute."""
    return pipelines.CodeBuildStep(
        step_id,
        input=source,
        additional_inputs=additional_inputs or None,
        primary_output_directory=primary_output_directory,
        commands=join(
            prelude(spec.runtime, node_version),
            cdk_cli_install(cdk_cli_version) if cdk_cli_version else (),
            spec.install,
            spec.commands,
        ),
        env=spec.environment,
        role_policy_statements=[*extra_policies, *spec.policy_statements],
        build_environment=_environment(spec.runtime, spec.memory_gb),
    )


class StandardPipeline(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repo: str,
        connection_arn: str,
        gates: Sequence[Gate],
        application_stage: cdk.Stage | None = None,
        deploy_step: DeployStep | None = None,
        assembly: Assembly | None = None,
        bootstrap_qualifier: str = "hnb659fds",
        synth_commands: Sequence[str],
        synth_install: Sequence[str] = (),
        synth_env: dict[str, str] | None = None,
        branch: str = "main",
        post_deploy: Sequence[PostDeployStep] = (),
        deploy_enabled: bool = False,
        notification_emails: Sequence[str] = (),
        require_approval: bool = True,
        pull_request_checks: bool = True,
        synth_output_dir: str = "cdk.out",
        synth_memory_gb: int = 4,
        cdk_cli_version: str = DEFAULT_CDK_CLI_VERSION,
        node_version: str = DEFAULT_NODE_VERSION,
    ) -> None:
        super().__init__(scope, construct_id)

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        _, _, name = repo.partition("/")
        if not gates:
            raise ValueError("at least one gate is required")
        if len({g.name for g in gates}) != len(gates):
            raise ValueError("gate names must be unique")
        if len({p.name for p in post_deploy}) != len(post_deploy):
            raise ValueError("post-deploy step names must be unique")

        if (application_stage is None) == (deploy_step is None):
            raise ValueError("provide exactly one of application_stage or deploy_step")

        if assembly is not None and application_stage is not None:
            raise ValueError(
                "assembly is for deploy_step pipelines: with application_stage the Synth step "
                "already builds the assembly that is deployed"
            )
        if deploy_step is not None and assembly is None and any(g.uses_assembly for g in gates):
            raise ValueError("a gate has uses_assembly=True but the pipeline has no assembly")
        if assembly is not None and deploy_step is not None:
            problems = assembly_deploy_violations(
                [*deploy_step.install, *deploy_step.commands], assembly.output_dir
            )
            if problems:
                raise ValueError(
                    f"deploy step {deploy_step.name!r} must deploy exactly the assembly the gates "
                    "checked: " + "; ".join(problems)
                )

        deploy_stage_name = (
            application_stage.stage_name if application_stage else DEPLOY_WAVE
        )

        # --- artifact bucket (explicit, account-namespaced name) -------------
        self.artifact_bucket = s3.Bucket(
            self,
            "Artifacts",
            # House naming rule {purpose}-{account}-{region}-an. A "...-an" name is
            # reserved for S3 account-regional namespaces: creating one with a literal
            # bucket_name fails ("missing the required x-amz-bucket-namespace header").
            # With the namespace set, S3 builds the name from this prefix.
            bucket_name_prefix=artifact_bucket_purpose(name),
            bucket_namespace=s3.BucketNamespace.ACCOUNT_REGIONAL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=cdk.Duration.days(30),
                    noncurrent_version_expiration=cdk.Duration.days(7),
                )
            ],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # --- source ----------------------------------------------------------
        source = pipelines.CodePipelineSource.connection(
            repo, branch, connection_arn=connection_arn, trigger_on_push=True
        )

        # --- synth (needs Python for the app and Node for the CDK CLI) -------
        synth = pipelines.CodeBuildStep(
            "Synth",
            input=source,
            commands=join(
                prelude(Runtime.PYTHON_NODE, node_version),
                cdk_cli_install(cdk_cli_version),
                synth_install,
                synth_commands,
            ),
            env={**BASE_ENV, **(synth_env or {})},
            primary_output_directory=synth_output_dir,
            build_environment=_environment(Runtime.PYTHON, synth_memory_gb),
        )

        # CDK Pipelines' own projects default to EC2 compute; pin them all.
        node_small = pipelines.CodeBuildOptions(
            build_environment=_environment(Runtime.NODE, 2)
        )

        cp_pipeline = codepipeline.Pipeline(
            self,
            "CodePipeline",
            pipeline_name=f"{name}-pipeline",
            pipeline_type=codepipeline.PipelineType.V2,
            artifact_bucket=self.artifact_bucket,
            # CodePipeline otherwise fails the running execution when the pipeline is
            # updated (self-mutation does exactly that), so a commit that changes the
            # pipeline would end "failed" at UpdatePipeline and never be deployed.
            restart_execution_on_update=True,
        )

        self.pipeline = pipelines.CodePipeline(
            self,
            "Pipeline",
            code_pipeline=cp_pipeline,
            synth=synth,
            cli_version=cdk_cli_version,
            code_build_defaults=node_small,
            synth_code_build_defaults=node_small,
            self_mutation_code_build_defaults=node_small,
            asset_publishing_code_build_defaults=node_small,
            docker_enabled_for_synth=False,
            docker_enabled_for_self_mutation=False,
            use_change_sets=True,
        )

        # --- build once: the assembly gates inspect is the one deployed -----
        # (application_stage: the Synth output; deploy_step: an Assembly step)
        assembly_inputs: dict[str, pipelines.IFileSetProducer] = {}
        assemble: pipelines.CodeBuildStep | None = None
        if assembly is not None:
            assemble = _codebuild_step(
                assembly.name,
                assembly,
                source=source,
                node_version=node_version,
                cdk_cli_version=cdk_cli_version,
                primary_output_directory=assembly.output_dir,
            )
            assembly_inputs = {assembly.output_dir: assemble}
        elif application_stage is not None:
            assembly_inputs = {synth_output_dir: synth}

        # --- gates: all run in parallel after the pipeline updates itself ----
        # (gates that read the assembly start once it exists; the rest do not wait)
        gate_steps = [
            _codebuild_step(
                f"Gate-{gate.name}",
                gate,
                source=source,
                node_version=node_version,
                additional_inputs=assembly_inputs if gate.uses_assembly else None,
            )
            for gate in gates
        ]
        self.pipeline.add_wave(
            "Gates", pre=[assemble] if assemble else [], post=gate_steps
        )

        # --- deploy stage: approval -> deploy -> post-deploy -----------------
        pre: list[pipelines.Step] = []
        if require_approval:
            pre.append(pipelines.ManualApprovalStep(f"Approve-{deploy_stage_name}"))

        post: list[pipelines.Step] = [
            _codebuild_step(
                f"Post-{step.name}", step, source=source, node_version=node_version
            )
            for step in post_deploy
        ]
        if application_stage is not None:
            self.pipeline.add_stage(application_stage, pre=pre, post=post)
        else:
            assert deploy_step is not None
            deploy = _codebuild_step(
                f"Deploy-{deploy_step.name}",
                deploy_step,
                source=source,
                node_version=node_version,
                cdk_cli_version=cdk_cli_version,
                additional_inputs=assembly_inputs,
                extra_policies=[
                    # `cdk deploy` assumes the CDK bootstrap roles (deploy,
                    # file/image publishing, lookup); nothing else.
                    iam.PolicyStatement(
                        actions=["sts:AssumeRole"],
                        resources=[
                            (
                                f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:role/"
                                f"cdk-{bootstrap_qualifier}-*-role-{cdk.Aws.ACCOUNT_ID}-{cdk.Aws.REGION}"
                            )
                        ],
                    ),
                ],
            )
            # In a wave with no stacks, `pre` and `post` steps are NOT ordered
            # by position alone: without these dependencies the approval and
            # the deploy get the same RunOrder and run in parallel, i.e. the
            # deploy would not wait for approval. Make the order explicit.
            for approval in pre:
                deploy.add_step_dependency(approval)
            for step in post:
                step.add_step_dependency(deploy)
            self.pipeline.add_wave(DEPLOY_WAVE, pre=pre, post=[deploy, *post])

        self.pipeline.build_pipeline()

        # --- hold the deploy stage closed unless explicitly enabled ----------
        if not deploy_enabled:
            cfn_pipeline = cp_pipeline.node.default_child
            cfn_pipeline.add_property_override(
                "DisableInboundStageTransitions",
                [
                    {
                        "StageName": deploy_stage_name,
                        "Reason": "Deploy disabled in pipeline definition (deploy_enabled=False)",
                    }
                ],
            )

        # --- failure notifications ------------------------------------------
        # Not KMS-encrypted on purpose: CodeStar Notifications cannot publish to a
        # topic encrypted with the AWS-managed SNS key.
        self.notification_topic = sns.Topic(self, "Notifications", enforce_ssl=True)
        for email in notification_emails:
            self.notification_topic.add_subscription(
                subscriptions.EmailSubscription(email)
            )
        cp_pipeline.notify_on(
            "Failed",
            self.notification_topic,
            events=[codepipeline.PipelineNotificationEvents.PIPELINE_EXECUTION_FAILED],
        )

        # --- PR validation (separate, deploy-less path) ----------------------
        self.pull_request_checks: PullRequestChecks | None = None
        if pull_request_checks:
            self.pull_request_checks = PullRequestChecks(
                self,
                "PullRequestChecks",
                repo=repo,
                base_branch=branch,
                connection_arn=connection_arn,
                gates=[g for g in gates if g.runs_on_pull_requests],
                name_prefix=name,
                node_version=node_version,
                assembly_commands=join(
                    cdk_cli_install(cdk_cli_version),
                    assembly.install if assembly else synth_install,
                    assembly.commands if assembly else synth_commands,
                ),
                assembly_env=(assembly.env if assembly else dict(synth_env or {})),
                assembly_memory_gb=assembly.memory_gb if assembly else synth_memory_gb,
            )
