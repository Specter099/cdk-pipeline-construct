"""Several Terraform roots in one pipeline, and credentials for non-AWS providers."""

import copy
import json

import pytest
from aws_cdk.assertions import Template
from test_pipeline import build

from specter_pipeline import TerraformRoot
from specter_pipeline import testing as c
from specter_pipeline.presets import terraform_gates

REPO = "Specter099/demo"
GH = "arn:aws:secretsmanager:us-east-1:000000000000:secret:gh-token-AbC123"
EQX = "arn:aws:secretsmanager:us-east-1:000000000000:secret:equinix-XyZ789:client_secret::"
NET = TerraformRoot("net", name="network")
GITHUB = TerraformRoot("gh", name="github", secrets={"GITHUB_TOKEN": GH})
EDGE = TerraformRoot("edge", name="edge", secrets={"METAL_AUTH_TOKEN": EQX})
ROOTS = [NET, GITHUB, EDGE]
GATES = terraform_gates(roots=["net", "gh", "edge"])


def tf_build(roots=ROOTS, **kw):
    return build(application_stage=None, terraform=roots, gates=GATES, **kw)


@pytest.fixture(scope="module")
def tpl():
    return tf_build()


def actions(t, stage):
    return {a["Name"]: a for a in c._stage(t, stage)["Actions"]}


def env_of(t, step):
    return {v["Name"]: (v["Type"], v["Value"]) for v in c._project_of(t, step)["Environment"]["EnvironmentVariables"]}


# --- shape -------------------------------------------------------------------------------------


def test_every_root_plans_and_is_checked_before_the_one_approval(tpl):
    gates = actions(tpl, "Gates")
    for r in ROOTS:
        assert gates[f"Gate-tf-plan-check-{r.name}"]["RunOrder"] > gates[f"Plan-{r.name}"]["RunOrder"]
    deploy = actions(tpl, "Deploy")
    assert [a for a in deploy if a.startswith("Approve-")] == ["Approve-Deploy"]


def test_applies_run_one_at_a_time_in_the_order_given(tpl):
    deploy = actions(tpl, "Deploy")
    orders = [deploy[f"Deploy-{r.name}"]["RunOrder"] for r in ROOTS]
    assert orders == sorted(orders) and len(set(orders)) == 3
    assert deploy["Approve-Deploy"]["RunOrder"] < orders[0] < deploy["Post-frontend"]["RunOrder"] > orders[-1]


def test_each_apply_reads_its_own_plan(tpl):
    gates, deploy = actions(tpl, "Gates"), actions(tpl, "Deploy")
    for r in ROOTS:
        (out,) = gates[f"Plan-{r.name}"]["OutputArtifacts"]
        assert [i["Name"] for i in deploy[f"Deploy-{r.name}"]["InputArtifacts"]] == [out["Name"]]


def test_one_state_bucket_and_one_key_per_root(tpl):
    buckets = [b for b in tpl.find_resources("AWS::S3::Bucket").values()
               if b["Properties"].get("BucketNamePrefix") == "demo-tfstate"]
    assert len(buckets) == 1
    for r in ROOTS:
        assert f"key={r.name}/terraform.tfstate" in " ".join(c.step_commands(tpl, f"Plan-{r.name}"))


def test_contract_passes(tpl):
    c.assert_standard_contract(tpl, repo=REPO, gates=GATES, terraform=ROOTS)


def test_a_single_root_still_works_as_before():
    t = build(application_stage=None, terraform=NET, gates=terraform_gates(roots=["net"]))
    c.assert_standard_contract(t, repo=REPO, gates=terraform_gates(roots=["net"]), terraform=NET)


@pytest.mark.parametrize(
    "roots,err,match",
    [
        ([], TypeError, "non-empty"),
        (["net"], TypeError, "TerraformRoot"),
        ([NET, TerraformRoot("other", name="network")], ValueError, "unique"),
    ],
)
def test_invalid_root_lists(roots, err, match):
    with pytest.raises(err, match=match):
        tf_build(roots=roots)


# --- provider secrets --------------------------------------------------------------------------


def test_secrets_reach_only_their_roots_plan_and_deploy(tpl):
    for step in ("Plan-github", "Deploy-github"):
        assert env_of(tpl, step)["GITHUB_TOKEN"] == ("SECRETS_MANAGER", GH)
    for step in ("Plan-edge", "Deploy-edge"):
        assert env_of(tpl, step)["METAL_AUTH_TOKEN"] == ("SECRETS_MANAGER", EQX)
    for step in ("Plan-network", "Deploy-network", "Gate-tf-plan-check-github", "Gate-tf-validate"):
        assert not any(t == "SECRETS_MANAGER" for t, _ in env_of(tpl, step).values()), step


def test_only_two_roles_per_secret_can_read_it(tpl):
    readers = {}
    for pol in tpl.find_resources("AWS::IAM::Policy").values():
        text = json.dumps(pol["Properties"]["PolicyDocument"])
        for name, arn in (("github", GH), ("edge", EQX.split(":client_secret")[0])):
            if arn in text:
                readers.setdefault(name, []).extend(r["Ref"] for r in pol["Properties"]["Roles"])
    assert {k: len(v) for k, v in readers.items()} == {"github": 2, "edge": 2}


@pytest.mark.parametrize(
    "secrets,match",
    [
        ({"GITHUB_TOKEN": "arn:aws:secretsmanager:us-east-1:000000000000:secret:gh-token"}, "complete Secrets"),
        ({"GITHUB_TOKEN": "arn:aws:ssm:us-east-1:000000000000:parameter/x"}, "complete Secrets"),
        ({"GITHUB_TOKEN": "*"}, "complete Secrets"),
        ({"github_token": GH}, "upper-case"),
        ({"TF_CLI_ARGS": GH}, "may not set"),
        ({"TF_WORKSPACE": GH}, "may not set"),
    ],
)
def test_secret_settings_are_validated(secrets, match):
    with pytest.raises(ValueError, match=match):
        TerraformRoot("gh", secrets=secrets)


def test_a_secret_name_cannot_also_be_plain_env():
    with pytest.raises(ValueError, match="not also in env"):
        TerraformRoot("gh", env={"GITHUB_TOKEN": "x"}, secrets={"GITHUB_TOKEN": GH})


# --- the contract rejects ----------------------------------------------------------------------


def mutated(change):
    doc = copy.deepcopy(tf_build().to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def _deploy_actions(res):
    pipe = next(r for r in res.values() if r["Type"] == "AWS::CodePipeline::Pipeline")
    return {a["Name"]: a for s in pipe["Properties"]["Stages"] if s["Name"] == "Deploy" for a in s["Actions"]}


def _project(res, step):
    return next(r["Properties"] for r in res.values() if r["Type"] == "AWS::CodeBuild::Project"
                and str(r["Properties"].get("Description", "")).endswith(f"/{step}"))


def _grant_secret_to(step):
    def change(res):
        role = _project(res, step)["ServiceRole"]["Fn::GetAtt"][0]
        pol = next(r for r in res.values() if r["Type"] == "AWS::IAM::Policy"
                   and {x["Ref"] for x in r["Properties"]["Roles"]} == {role})
        pol["Properties"]["PolicyDocument"]["Statement"].append(
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "*"})
    return change


def _secret_env_on(step):
    def change(res):
        _project(res, step)["Environment"]["EnvironmentVariables"].append(
            {"Name": "GITHUB_TOKEN", "Type": "SECRETS_MANAGER", "Value": GH})
    return change


@pytest.mark.parametrize(
    "change,root,message",
    [
        (lambda r: _deploy_actions(r)["Deploy-github"].__setitem__("RunOrder", _deploy_actions(r)["Deploy-network"]["RunOrder"]),
         None, "not strictly in the order"),
        (_grant_secret_to("Gate-tf-validate"), GITHUB, "can read github's secret"),
        (_grant_secret_to("Plan-network"), GITHUB, "can read github's secret"),
        (_secret_env_on("Gate-tf-plan-check-github"), GITHUB, "besides CI=true"),
        (lambda r: _project(r, "Deploy-github")["Environment"]["EnvironmentVariables"].__setitem__(
            slice(None), [v for v in _project(r, "Deploy-github")["Environment"]["EnvironmentVariables"] if v["Name"] != "GITHUB_TOKEN"]),
         GITHUB, "environment differs"),
    ],
)
def test_contract_rejects(change, root, message):
    t = mutated(change)
    with pytest.raises(AssertionError, match=message):
        if root is None:
            c.assert_terraform_order(t, ROOTS)
        else:
            c.assert_applies_plan(t, root, "demo")
