"""Several Terraform roots in one pipeline, and credentials for non-AWS providers."""

import copy
import json

import pytest
from aws_cdk.assertions import Template
from test_pipeline import POST, build

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
    c.assert_standard_contract(tpl, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOTS)


def test_a_single_root_still_works_as_before():
    t = build(application_stage=None, terraform=NET, gates=terraform_gates(roots=["net"]))
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=terraform_gates(roots=["net"]), terraform=NET)


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


def _buildspec_secrets(t, step):
    return c._step_buildspec(t, step).get("env", {}).get("secrets-manager", {})


def test_secrets_reach_only_their_roots_plan_and_deploy(tpl):
    # in the buildspec, not as project variables: for those CDK adds a grant on "<arn>*"
    for step in ("Plan-github", "Deploy-github"):
        assert _buildspec_secrets(tpl, step) == {"GITHUB_TOKEN": GH}
    for step in ("Plan-edge", "Deploy-edge"):
        assert _buildspec_secrets(tpl, step) == {"METAL_AUTH_TOKEN": EQX}
    for step in ("Plan-network", "Deploy-network", "Gate-tf-plan-check-github", "Gate-tf-validate"):
        assert not _buildspec_secrets(tpl, step), step
    for proj in c.projects(tpl).values():
        env = proj["Properties"]["Environment"].get("EnvironmentVariables", [])
        assert not any(v.get("Type") == "SECRETS_MANAGER" for v in env)


def test_secret_grants_are_exactly_the_secret_arns(tpl):
    for pol in tpl.find_resources("AWS::IAM::Policy").values():
        for st in pol["Properties"]["PolicyDocument"]["Statement"]:
            if "secretsmanager" in json.dumps(st["Action"]):
                assert all(r in {GH, "arn:aws:secretsmanager:us-east-1:000000000000:secret:equinix-XyZ789"}
                           for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]])), st
            assert "kms:" not in json.dumps(st["Action"]), st


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
        ({"TF_VAR_TOKEN": GH}, "saved plan"),
        ({"CI": GH}, "upper-case"),
        ({"TF_LOG": GH}, "secrets may not set"),
        ({"AWS_ACCESS_KEY_ID": GH}, "secrets may not set"),
        ({"CODEBUILD_X": GH}, "secrets may not set"),
        ({"PATH": GH}, "secrets may not set"),
        ({"GITHUB_TOKEN": GH + ":key\nrm -rf /"}, "complete Secrets"),
        ({"GITHUB_TOKEN": GH + ":*"}, "complete Secrets"),
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


def _drop_buildspec_secrets(step):
    def change(res):
        p = _project(res, step)
        spec = json.loads(p["Source"]["BuildSpec"])
        spec.pop("env")
        p["Source"]["BuildSpec"] = json.dumps(spec)
    return change


def _widen_secret_grant(step):
    def change(res):
        role = _project(res, step)["ServiceRole"]["Fn::GetAtt"][0]
        pol = next(r for r in res.values() if r["Type"] == "AWS::IAM::Policy"
                   and {x["Ref"] for x in r["Properties"]["Roles"]} == {role})
        pol["Properties"]["PolicyDocument"]["Statement"].append(
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": GH + "*"})
    return change


@pytest.mark.parametrize(
    "change,root,message",
    [
        (lambda r: _deploy_actions(r)["Deploy-github"].__setitem__("RunOrder", _deploy_actions(r)["Deploy-network"]["RunOrder"]),
         None, "not strictly in the order"),
        (_grant_secret_to("Gate-tf-validate"), GITHUB, "can read github's secret"),
        (_grant_secret_to("Plan-network"), GITHUB, "can read github's secret"),
        (_secret_env_on("Gate-tf-plan-check-github"), GITHUB, "besides CI=true"),
        (_secret_env_on("Deploy-github"), GITHUB, "environment differs"),
        (_drop_buildspec_secrets("Deploy-github"), GITHUB, "buildspec has more than"),
        (_widen_secret_grant("Plan-github"), GITHUB, "reads secrets more widely"),
    ],
)
def test_contract_rejects(change, root, message):
    t = mutated(change)
    with pytest.raises(AssertionError, match=message):
        if root is None:
            c.assert_terraform_order(t, ROOTS)
        else:
            c.assert_applies_plan(t, root, "demo")


_TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def test_an_unrelated_role_with_aws_managed_policies_does_not_break_the_secret_check():
    def change(res):
        res["OtherRole"] = {"Type": "AWS::IAM::Role", "Properties": {
            "AssumeRolePolicyDocument": _TRUST, "ManagedPolicyArns": ["arn:aws:iam::aws:policy/ReadOnlyAccess"]}}
    t = mutated(change)
    for r in ROOTS:
        c.assert_applies_plan(t, r)


@pytest.mark.parametrize(
    "principal",
    [
        {"Type": "AWS::IAM::Group", "Properties": {"Policies": [{"PolicyName": "p", "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "secretsmanager:*", "Resource": GH}]}}]}},
        {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": _TRUST, "Policies": [
            {"PolicyName": "p", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "NotAction": "s3:*", "Resource": "*"}]}}]}},
    ],
    ids=["group", "not-action-role"],
)
def test_any_other_principal_that_can_read_a_secret_fails(principal):
    def change(res):
        res["Other"] = principal
    with pytest.raises(AssertionError, match="can read github's secret"):
        c.assert_applies_plan(mutated(change), GITHUB)


@pytest.mark.parametrize("key", ["AWS_REGION", "AWS_PROFILE", "CODEBUILD_SRC_DIR", "PATH", "TF_LOG", "CI"])
def test_env_may_not_redirect_the_sdk_or_codebuild(key):
    with pytest.raises(ValueError, match="env may not set"):
        TerraformRoot("x", env={key: "v"})


def test_roots_may_share_a_secret():
    a = TerraformRoot("a", name="a", secrets={"GITHUB_TOKEN": GH})
    b = TerraformRoot("b", name="b", secrets={"GITHUB_TOKEN": GH})
    t = build(application_stage=None, terraform=[a, b], gates=terraform_gates(roots=["a", "b"]))
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=terraform_gates(roots=["a", "b"]),
                               terraform=[a, b])


def _role_stmts(res, step):
    role = _project(res, step)["ServiceRole"]["Fn::GetAtt"][0]
    pol = next(r for r in res.values() if r["Type"] == "AWS::IAM::Policy"
               and {x["Ref"] for x in r["Properties"]["Roles"]} == {role})
    return pol["Properties"]["PolicyDocument"]["Statement"]


def _state_lid(res):
    return next(k for k, r in res.items() if r["Type"] == "AWS::S3::Bucket"
                and r["Properties"].get("BucketNamePrefix") == "demo-tfstate")


def _key(res, key):
    return {"Fn::Join": ["", [{"Fn::GetAtt": [_state_lid(res), "Arn"]}, "/" + key]]}


@pytest.mark.parametrize(
    "step,action,key,message",
    [
        ("Plan-github", "s3:GetObject", "network/terraform.tfstate", "can read network's"),
        ("Plan-github", "s3:GetObject", "*", "can read network's"),
        ("Deploy-github", "s3:DeleteObject", "network/terraform.tfstate.tflock", "release the lock of network's"),
        ("Plan-github", "s3:PutObject", "network/terraform.tfstate.tflock", "take the lock of network's"),
        ("Gate-tf-validate", "s3:GetObject", "network/terraform.tfstate", "can read network's"),
    ],
)
def test_one_root_cannot_reach_another_roots_state_or_lock(step, action, key, message):
    def change(res):
        _role_stmts(res, step).append({"Effect": "Allow", "Action": action, "Resource": _key(res, key)})
    with pytest.raises(AssertionError, match=message):
        c.assert_applies_plan(mutated(change), NET, "demo", ROOTS)


def test_a_role_reading_a_secret_of_this_template_is_not_a_false_positive():
    def change(res):
        res["MySecret"] = {"Type": "AWS::SecretsManager::Secret", "Properties": {}}
        _role_stmts(res, "Plan-network").append(
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": {"Ref": "MySecret"}})
    t = mutated(change)
    for r in ROOTS:
        c.assert_applies_plan(t, r, "demo", ROOTS)


def test_a_missing_apply_is_an_assertion_not_a_key_error():
    with pytest.raises(AssertionError, match="no \\['Deploy-nope'\\]"):
        c.assert_terraform_order(tf_build(), [*ROOTS, TerraformRoot("nope", name="nope")])
