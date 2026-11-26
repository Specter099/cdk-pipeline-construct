"""This repo's own CI stack: PR checks only, least privilege, the EC2 smoke check on EC2."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from ci.ci_stack import CI_GATES, NAME_PREFIX, LibraryCiStack
from specter_pipeline import testing as c
from specter_pipeline.compute import EC2_IMAGE

CONN = "arn:aws:codeconnections:us-east-1:000000000000:connection/abc"


@pytest.fixture(scope="module")
def tpl():
    app = cdk.App()
    stack = LibraryCiStack(app, "Ci", connection_arn=CONN,
                           env=cdk.Environment(account="0" * 12, region="us-east-1"))
    return Template.from_stack(stack)


def test_one_pr_check_per_gate_and_nothing_else(tpl):
    names = {r["Properties"]["Name"] for r in c.projects(tpl).values()}
    assert names == {f"{NAME_PREFIX}-pr-{g.name}" for g in CI_GATES}
    assert not tpl.find_resources("AWS::CodePipeline::Pipeline")


def test_pr_checks_are_least_privilege(tpl):
    c.assert_pr_checks(tpl, CI_GATES, NAME_PREFIX, connection_arn=CONN)
    c.assert_pr_roles_least_privilege(tpl, NAME_PREFIX)
    c.assert_no_wildcard_resources(tpl)


def test_only_the_smoke_check_is_on_ec2(tpl):
    c.assert_lambda_compute(tpl, [f"{NAME_PREFIX}-pr-ec2-smoke"])
    (smoke,) = [r["Properties"] for r in c.projects(tpl).values()
                if r["Properties"]["Name"].endswith("-pr-ec2-smoke")]
    assert smoke["Environment"]["Image"] == EC2_IMAGE.image_id
    cmds = json.loads(smoke["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert any("Python 3" in x and "3\\.12" in x for x in cmds)
    assert any("^v22" in x for x in cmds)


def test_the_smoke_check_rejects_the_wrong_interpreters(tmp_path):
    """The smoke assertions, run for real against stand-in python and node."""
    import shutil
    import subprocess

    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    (smoke,) = [g for g in CI_GATES if g.name == "ec2-smoke"]
    checks = smoke.commands[:2]
    bin_ = tmp_path / "bin"
    bin_.mkdir()

    def run(py, node):
        (bin_ / "python").write_text(f"#!/bin/sh\necho 'Python {py}'\n")
        (bin_ / "node").write_text(f"#!/bin/sh\necho 'v{node}'\n")
        for f in bin_.iterdir():
            f.chmod(0o755)
        return subprocess.run(["bash", "-ec", "\n".join(checks)], check=False, capture_output=True,
                              text=True, env={"PATH": f"{bin_}:/usr/bin:/bin"}).returncode

    assert run("3.12.15", "22.22.0") == 0
    assert run("3.14.9", "22.22.0") != 0
    assert run("3.12.15", "24.1.0") != 0
