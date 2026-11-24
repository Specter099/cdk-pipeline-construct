"""Pull-request validation: one CodeBuild project per gate, triggered by webhook.

Why this is NOT part of the CDK Pipelines pipeline: a CodePipeline V2 PR
trigger runs the *whole* pipeline, including the self-mutation step that
rewrites the pipeline from the checked-out revision. A pull-request branch
would then be able to rewrite the production pipeline before any approval.
Plain CodeBuild webhook projects have no self-mutation and no deploy rights,
and they report a commit status back to the PR (one per gate), which a
CodePipeline PR trigger does not.

Authorization uses the shared CodeConnections connection through the project
source's ``Auth`` block (type ``CODECONNECTIONS``).

Security: these projects execute the code of the PR head. Their role is
deliberately limited to CloudWatch Logs and the connection token. Keep the
repository private (or restrict who can open PRs) -- do not attach deploy
permissions here.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from constructs import Construct

from .gate import Gate, compute_type
from .runtimes import DEFAULT_NODE_VERSION, join, prelude


class PullRequestChecks(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repo: str,
        base_branch: str,
        connection_arn: str,
        gates: Sequence[Gate],
        name_prefix: str,
        node_version: str = DEFAULT_NODE_VERSION,
    ) -> None:
        super().__init__(scope, construct_id)
        owner, _, repo_name = repo.partition("/")
        if not owner or not repo_name:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")

        self.projects: Mapping[str, codebuild.Project] = {}
        projects: dict[str, codebuild.Project] = {}

        for gate in gates:
            project = codebuild.Project(
                self,
                f"Pr-{gate.name}",
                project_name=f"{name_prefix}-pr-{gate.name}",
                description=f"PR check '{gate.name}' for {repo}",
                source=codebuild.Source.git_hub(
                    owner=owner,
                    repo=repo_name,
                    webhook=True,
                    report_build_status=True,
                    build_status_context=f"specter/{gate.name}",
                    webhook_filters=[
                        codebuild.FilterGroup.in_event_of(
                            codebuild.EventAction.PULL_REQUEST_CREATED,
                            codebuild.EventAction.PULL_REQUEST_UPDATED,
                            codebuild.EventAction.PULL_REQUEST_REOPENED,
                        ).and_base_branch_is(f"^refs/heads/{base_branch}$")
                    ],
                ),
                environment=codebuild.BuildEnvironment(
                    build_image=gate.runtime.image,
                    compute_type=compute_type(gate.memory_gb),
                ),
                environment_variables={
                    key: codebuild.BuildEnvironmentVariable(value=value)
                    for key, value in gate.environment.items()
                },
                build_spec=codebuild.BuildSpec.from_object(
                    {
                        "version": "0.2",
                        "phases": {
                            "build": {
                                "commands": join(
                                    prelude(gate.runtime, node_version),
                                    gate.install,
                                    gate.commands,
                                )
                            }
                        },
                    }
                ),
            )

            # Source-level credential: the shared GitHub App connection.
            cfn_project = project.node.default_child
            cfn_project.add_property_override(
                "Source.Auth", {"Type": "CODECONNECTIONS", "Resource": connection_arn}
            )
            project.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "codeconnections:GetConnectionToken",
                        "codeconnections:GetConnection",
                    ],
                    resources=[connection_arn],
                )
            )
            projects[gate.name] = project

        self.projects = projects
