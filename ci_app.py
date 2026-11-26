#!/usr/bin/env python3
"""This repo's own pull-request checks. Deploy once from a machine with AWS access:

    cdk -a "python3 ci_app.py" deploy -c connection_arn=arn:aws:codeconnections:...

The connection is the shared one from ``app.py`` (``SpecterGitHubConnection``). Account and region
come from CDK context or the CLI's ``CDK_DEFAULT_*`` environment.
"""

import os

import aws_cdk as cdk

from ci.ci_stack import LibraryCiStack

app = cdk.App()
connection_arn = app.node.try_get_context("connection_arn")
if not connection_arn:
    raise SystemExit("pass -c connection_arn=<the shared GitHub connection ARN>")
LibraryCiStack(
    app,
    "CdkPipelineConstructCi",
    connection_arn=connection_arn,
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
    ),
    # No assets, so no bootstrap bucket or ECR repository; CloudFormation still deploys through
    # the account's CDK bootstrap roles, so the account must be bootstrapped.
    synthesizer=cdk.BootstraplessSynthesizer(),
)
app.synth()
