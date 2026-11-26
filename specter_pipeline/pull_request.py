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

A gate with ``uses_assembly=True`` reads ``cdk.out`` in the pipeline. A PR has no pipeline
assembly, so its project first runs ``assembly_commands`` (the pipeline's own assembly or synth
recipe) on the Python+Node runtime with the assembly's memory, then the gate. Its project
therefore also receives the assembly's (or synth's) environment variables, next to PR-head code:
keep secrets out of ``Assembly.env`` and ``synth_env``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from constructs import Construct

from .compute import Ec2, build_environment, build_timeout
from .gate import BASE_ENV, Gate
from .runtimes import DEFAULT_NODE_VERSION, Runtime, join, prelude


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
        assembly_commands: Sequence[str] = (),
        assembly_env: Mapping[str, str] | None = None,
        assembly_memory_gb: int = 4,
        assembly_ec2: Ec2 | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        owner, _, repo_name = repo.partition("/")
        if not owner or not repo_name:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")

        self.projects: Mapping[str, codebuild.Project] = {}
        projects: dict[str, codebuild.Project] = {}

        for gate in gates:
            if gate.uses_assembly and not assembly_commands:
                raise ValueError(
                    f"gate {gate.name!r} reads the assembly, so its PR check needs assembly_commands"
                )
            # An assembly gate builds cdk.out first, which needs Python and Node.
            runtime = Runtime.PYTHON_NODE if gate.uses_assembly else gate.runtime
            # ...and as much memory as the pipeline gives that synthesis.
            memory_gb = (
                max(gate.memory_gb, assembly_memory_gb) if gate.uses_assembly else gate.memory_gb
            )
            # A synthesis that needs EC2 in the pipeline needs it on the PR too, at least as big,
            # with time for the synthesis and the gate.
            # A Lambda side counts as its EC2 equal: at least its memory, up to Lambda's limit.
            if gate.uses_assembly and (assembly_ec2 is not None or gate.ec2 is not None):
                synth = assembly_ec2 or Ec2.at_least(assembly_memory_gb)
                own = gate.ec2 or Ec2.at_least(gate.memory_gb)
                ec2 = synth.larger(own)
            else:
                ec2 = gate.ec2
            pre = list(assembly_commands) if gate.uses_assembly else []
            env = (
                {**BASE_ENV, **(assembly_env or {}), **gate.env}
                if gate.uses_assembly
                else gate.environment
            )
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
                            # `and_base_branch_is` prepends "refs/heads/" itself, so a
                            # "^refs/heads/..." pattern becomes "refs/heads/^refs/heads/..."
                            # and can never match. Use the full ref and anchor it.
                        ).and_base_ref_is(f"^refs/heads/{re.escape(base_branch)}$")
                    ],
                ),
                environment=build_environment(runtime, memory_gb, ec2),
                timeout=build_timeout(ec2),
                environment_variables={
                    key: codebuild.BuildEnvironmentVariable(value=value)
                    for key, value in env.items()
                },
                build_spec=codebuild.BuildSpec.from_object(
                    {
                        "version": "0.2",
                        "phases": {
                            "build": {
                                "commands": join(
                                    prelude(runtime, node_version, ec2=ec2 is not None),
                                    pre,
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
            # Explicit rather than relying on a service default: a fork's PR
            # head would run with this role (which can fetch the shared GitHub
            # App token), so fork PRs need an approval comment from someone
            # with write access.
            cfn_project.add_property_override(
                "Triggers.PullRequestBuildPolicy",
                {
                    "RequiresCommentApproval": "FORK_PULL_REQUESTS",
                    "ApproverRoles": [
                        "GITHUB_WRITE",
                        "GITHUB_MAINTAIN",
                        "GITHUB_ADMIN",
                    ],
                },
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
