"""Terraform drift detection: a scheduled, read-only refresh of each root against its state.

Step 5 of the Terraform pipeline. For every ``TerraformRoot`` a CodeBuild project checks out the
pipeline's branch through the shared GitHub connection, initialises against the state bucket, and
runs ``terraform plan -refresh-only -detailed-exitcode -lock=false``. Exit 2 (drift) fails the
build; failed builds are published to the pipeline's notification topic.

The project has the root's plan permissions (read), read-only access to its state object (no
lock: ``-lock=false`` never blocks a release) and the root's provider secrets. It has no state
write, no deploy rights and no webhook: it runs only on its schedule.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

from .compute import build_environment, build_timeout
from .runtimes import (
    DEFAULT_NODE_VERSION,
    DEFAULT_TERRAFORM_VERSION,
    Runtime,
    join,
    prelude,
)
from .terraform import (
    TerraformRoot,
    backend_check,
    init_command,
    secrets_buildspec,
    secrets_policy,
)


def drift_commands(root: TerraformRoot) -> list[str]:
    var_files = " ".join(f"-var-file={shlex.quote(v)}" for v in root.var_files)
    drift = shlex.quote(
        f"DRIFT in {root.name}: the real infrastructure differs from the state. "
        "Review the plan above, then release or fix the change made outside Terraform"
    )
    return [
        f"cd {shlex.quote(root.path)}",
        "test -f .terraform.lock.hcl || { echo 'commit .terraform.lock.hcl'; exit 1; }",
        init_command(root),
        backend_check(root),
        (
            "rc=0; terraform plan -refresh-only -detailed-exitcode -lock=false -input=false "
            f"-no-color {var_files} || rc=$?; ".replace("  ", " ")
            + f'if [ "$rc" -eq 2 ]; then echo {drift}; fi; [ "$rc" -eq 0 ]'
        ),
    ]


def state_read_policy(bucket: s3.IBucket, root: TerraformRoot) -> list[iam.PolicyStatement]:
    """Read the root's state object; nothing else in the bucket, and no lock."""
    return [
        # ListBucket so a missing state reads as 404, as for Plan.
        iam.PolicyStatement(actions=["s3:ListBucket"], resources=[bucket.bucket_arn]),
        iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[f"{bucket.bucket_arn}/{root.state_key}"]
        ),
    ]


class TerraformDriftChecks(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repo: str,
        branch: str,
        connection_arn: str,
        roots: Sequence[TerraformRoot],
        state_bucket: s3.IBucket,
        schedule: str,
        topic: sns.ITopic,
        name_prefix: str,
        node_version: str = DEFAULT_NODE_VERSION,
        terraform_version: str = DEFAULT_TERRAFORM_VERSION,
    ) -> None:
        super().__init__(scope, construct_id)
        owner, _, repo_name = repo.partition("/")
        self.projects: dict[str, codebuild.Project] = {}
        rule = events.Rule(
            self, "Schedule", schedule=events.Schedule.expression(schedule),
            description=f"Terraform drift check for {repo}",
        )
        for root in roots:
            project = codebuild.Project(
                self,
                f"Drift-{root.name}",
                project_name=f"{name_prefix}-drift-{root.name}",
                description=f"Terraform drift check '{root.name}' for {repo}",
                source=codebuild.Source.git_hub(
                    owner=owner, repo=repo_name, branch_or_ref=branch, webhook=False,
                    report_build_status=False,
                ),
                environment=build_environment(Runtime.TERRAFORM, 2, root.plan_ec2),
                timeout=build_timeout(root.plan_ec2),
                environment_variables={
                    **{
                        key: codebuild.BuildEnvironmentVariable(value=value)
                        for key, value in root.environment.items()
                    },
                    "TF_STATE_BUCKET": codebuild.BuildEnvironmentVariable(
                        value=state_bucket.bucket_name
                    ),
                },
                build_spec=codebuild.BuildSpec.from_object(
                    {
                        "version": "0.2",
                        # secrets in the buildspec: as project variables CDK grants "<arn>*"
                        **secrets_buildspec(root),
                        "phases": {
                            "build": {
                                "commands": join(
                                    prelude(Runtime.TERRAFORM, node_version, terraform_version, ec2=True),
                                    drift_commands(root),
                                )
                            }
                        },
                    }
                ),
            )
            cfn = project.node.default_child
            cfn.add_property_override(
                "Source.Auth", {"Type": "CODECONNECTIONS", "Resource": connection_arn}
            )
            for statement in (
                *root.plan_policy_statements,
                *state_read_policy(state_bucket, root),
                *secrets_policy(root),
                iam.PolicyStatement(
                    actions=["codeconnections:GetConnectionToken", "codeconnections:GetConnection"],
                    resources=[connection_arn],
                ),
            ):
                project.add_to_role_policy(statement)
            rule.add_target(targets.CodeBuildProject(project))
            project.on_build_failed(f"Drift-{root.name}-Failed", target=targets.SnsTopic(topic))
            self.projects[root.name] = project

