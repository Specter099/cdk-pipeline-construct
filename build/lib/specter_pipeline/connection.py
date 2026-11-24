"""Shared GitHub connection (AWS CodeConnections).

One connection per account/region serves every repo's pipeline, so it lives in
its own stack rather than in any single pipeline stack. Pipelines consume the
connection by ARN (see ``connection_arn`` on the pipeline construct).

A newly created connection is always PENDING. A human must complete the GitHub
App authorization once in the console (Developer Tools > Settings > Connections
> Update pending connection). That step cannot be done through the API.
"""
import aws_cdk as cdk
from aws_cdk import aws_codeconnections as codeconnections
from constructs import Construct


class GitHubConnectionStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, connection_name: str = "specter099-github", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.connection = codeconnections.CfnConnection(
            self,
            "GitHubConnection",
            connection_name=connection_name,
            provider_type="GitHub",
        )
        # Never let a stack update/delete silently drop an authorized
        # connection: re-authorizing is a manual step.
        self.connection.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        self.connection_arn = self.connection.attr_connection_arn

        cdk.CfnOutput(
            self,
            "ConnectionArn",
            value=self.connection_arn,
            description="Pass as connection_arn to pipelines. Authorize in the console while PENDING.",
        )
