#!/usr/bin/env python3
"""Deploys the shared GitHub connection only. Pipelines live in each repo.

Account and region come from CDK context (``-c account=... -c region=...``) or
the CDK CLI's ``CDK_DEFAULT_*`` environment, never from hardcoded values.
"""

import os

import aws_cdk as cdk

from specter_pipeline import GitHubConnectionStack

app = cdk.App()
GitHubConnectionStack(
    app,
    "SpecterGitHubConnection",
    env=cdk.Environment(
        account=app.node.try_get_context("account")
        or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region")
        or os.environ.get("CDK_DEFAULT_REGION"),
    ),
    # No assets, so no dependency on CDK bootstrap resources.
    synthesizer=cdk.BootstraplessSynthesizer(),
)
app.synth()
