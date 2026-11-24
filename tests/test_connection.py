import aws_cdk as cdk
from aws_cdk.assertions import Template

from specter_pipeline import GitHubConnectionStack


def _template() -> Template:
    app = cdk.App()
    stack = GitHubConnectionStack(
        app, "T", env=cdk.Environment(account="111111111111", region="us-east-1")
    )
    return Template.from_stack(stack)


def test_connection_is_github_provider():
    _template().has_resource_properties(
        "AWS::CodeConnections::Connection",
        {"ConnectionName": "specter099-github", "ProviderType": "GitHub"},
    )


def test_connection_is_retained():
    _template().has_resource(
        "AWS::CodeConnections::Connection", {"DeletionPolicy": "Retain"}
    )


def test_arn_is_exported_as_output():
    assert "ConnectionArn" in _template().to_json()["Outputs"]
