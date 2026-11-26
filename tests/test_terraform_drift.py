"""Scheduled, read-only Terraform drift checks."""

import copy
import json
import shutil
import subprocess

import pytest
from aws_cdk.assertions import Template
from test_pipeline import POST, build
from test_terraform_multi import GATES, GH, ROOTS, tf_build

from specter_pipeline import TerraformRoot
from specter_pipeline import testing as c
from specter_pipeline.drift import drift_commands

REPO = "Specter099/demo"
CRON = "cron(17 6 * * ? *)"


@pytest.fixture(scope="module")
def tpl():
    return tf_build(drift_schedule=CRON)


def drift_project(t, root):
    return c._drift_project(t, root)


def test_contract_passes(tpl):
    c.assert_standard_contract(tpl, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOTS, drift=True)


def test_one_drift_check_per_root_on_a_schedule(tpl):
    names = sorted(r["Properties"]["Name"] for r in c.projects(tpl).values()
                   if "-drift-" in r["Properties"].get("Name", ""))
    assert names == sorted(f"demo-drift-{r.name}" for r in ROOTS)
    (rule,) = [r for r in tpl.find_resources("AWS::Events::Rule").values()
               if r["Properties"].get("ScheduleExpression")]
    assert rule["Properties"]["ScheduleExpression"] == CRON and len(rule["Properties"]["Targets"]) == 3


def test_drift_checks_read_the_branch_with_no_webhook(tpl):
    for r in ROOTS:
        p = drift_project(tpl, r)
        assert p["Source"]["Type"] == "GITHUB" and p["Source"]["Auth"]["Type"] == "CODECONNECTIONS"
        assert p["Triggers"] == {"Webhook": False}


def test_drift_check_never_locks_or_writes(tpl):
    cmds = c._buildspec_commands(drift_project(tpl, ROOTS[0]))
    assert "-refresh-only -detailed-exitcode -lock=false" in cmds[-1]
    assert not any(" apply" in x for x in cmds)


def test_secrets_reach_the_drift_check_of_their_root_only(tpl):
    import json

    def secrets(root):
        spec = json.loads(drift_project(tpl, root)["Source"]["BuildSpec"])
        return spec.get("env", {}).get("secrets-manager", {})

    assert secrets(ROOTS[1]) == {"GITHUB_TOKEN": GH}
    assert secrets(ROOTS[0]) == {}
    for root in ROOTS:
        env = drift_project(tpl, root)["Environment"]["EnvironmentVariables"]
        assert not any(v.get("Type") == "SECRETS_MANAGER" for v in env)


@pytest.mark.parametrize("bad", ["daily", "cron(0 6 * * ? *", "rate 1 day"])
def test_schedule_is_validated(bad):
    with pytest.raises(ValueError, match="cron\\(...\\) or rate"):
        tf_build(drift_schedule=bad)


def test_drift_needs_terraform():
    with pytest.raises(ValueError, match="this pipeline has none"):
        build(drift_schedule=CRON)


def _fake_terraform(tmp_path, plan_rc):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "terraform").write_text(f'#!/bin/sh\nif [ "$1" = plan ]; then exit {plan_rc}; fi\nexit 0\n')
    (bin_ / "terraform").chmod(0o755)
    return bin_


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("plan_rc,ok,drift", [(0, True, False), (2, False, True), (1, False, False)])
def test_drift_exit_codes(tmp_path, plan_rc, ok, drift):
    """-detailed-exitcode: 0 no changes, 2 drift (fails and says so), 1 error (fails)."""
    bin_ = _fake_terraform(tmp_path, plan_rc)
    cmd = drift_commands(TerraformRoot("."))[-1]
    r = subprocess.run(["bash", "-c", cmd], check=False, cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": f"{bin_}:/usr/bin:/bin"})
    assert (r.returncode == 0) is ok
    assert ("DRIFT in main" in r.stdout) is drift


# --- the contract rejects ----------------------------------------------------------------------


def mutated(change):
    doc = copy.deepcopy(tf_build(drift_schedule=CRON).to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def _drift(res, name="network"):
    return next(r["Properties"] for r in res.values() if r["Type"] == "AWS::CodeBuild::Project"
                and r["Properties"].get("Name") == f"demo-drift-{name}")


def _drift_policy(res, name="network"):
    role = _drift(res, name)["ServiceRole"]["Fn::GetAtt"][0]
    return next(r["Properties"]["PolicyDocument"]["Statement"] for r in res.values()
                if r["Type"] == "AWS::IAM::Policy" and {x["Ref"] for x in r["Properties"]["Roles"]} == {role})


def _edit(name, edit):
    def change(res):
        p = _drift(res, name)
        spec = json.loads(p["Source"]["BuildSpec"])
        edit(spec["phases"]["build"]["commands"])
        p["Source"]["BuildSpec"] = json.dumps(spec)
    return change


def _drop_schedule_target(res):
    for r in res.values():
        if r["Type"] == "AWS::Events::Rule" and r["Properties"].get("ScheduleExpression"):
            r["Properties"]["Targets"] = r["Properties"]["Targets"][1:]


def _drop_failure_rules(res):
    for k in [k for k, r in res.items() if r["Type"] == "AWS::Events::Rule"
              and "FAILED" in json.dumps(r["Properties"].get("EventPattern", {}))]:
        res.pop(k)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: _drift(r).__setitem__("Triggers", {"Webhook": True, "FilterGroups": [[{"Type": "EVENT", "Pattern": "PUSH"}]]}), "has a webhook"),
        (_edit("network", lambda cmds: cmds.__setitem__(-1, cmds[-1].replace("-lock=false ", ""))), "runs more or other"),
        (_edit("network", lambda cmds: cmds.append("terraform apply -auto-approve")), "runs more or other"),
        (_drop_schedule_target, "not started by a schedule"),
        (_drop_failure_rules, "failures are not published"),
        (lambda r: _drift_policy(r).append({"Effect": "Allow", "Action": "s3:PutObject", "Resource": "*"}), "can write the state or its lock|can write the Terraform state"),
    ],
)
def test_assert_drift_checks_rejects(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_standard_contract(mutated(change), post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOTS, drift=True)


def test_the_drift_role_is_an_allowed_reader_of_its_roots_secret_only(tpl):
    c.assert_applies_plan(tpl, ROOTS[1], "demo", drift=True)
    # without drift=True the drift role is an unexpected reader (its state first, then its secret)
    with pytest.raises(AssertionError, match="demo-drift-github can read github's"):
        c.assert_applies_plan(tpl, ROOTS[1], "demo", drift=False)
    assert GH
