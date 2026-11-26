"""Terraform plan once, check the plan, apply exactly that plan."""

import copy
import json
import pathlib
import shutil
import subprocess

import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from test_pipeline import build

from specter_pipeline import Assembly, DeployStep, Ec2, Gate, TerraformRoot
from specter_pipeline import testing as c
from specter_pipeline.compute import EC2_IMAGE
from specter_pipeline.presets import terraform_gates
from specter_pipeline.terraform import (
    apply_commands,
    plan_check_commands,
    plan_commands,
)

REPO = "Specter099/demo"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "tfplan"
APPLY_IAM = iam.PolicyStatement(actions=["sqs:CreateQueue"], resources=["arn:aws:sqs:us-east-1:000000000000:app-*"])
PLAN_IAM = iam.PolicyStatement(actions=["sqs:GetQueueAttributes"], resources=["arn:aws:sqs:us-east-1:000000000000:app-*"])
ROOT = TerraformRoot("infra", plan_policy_statements=[PLAN_IAM], apply_policy_statements=[APPLY_IAM])
GATES = terraform_gates(roots=["infra"])


def tf_build(root=ROOT, gates=GATES, **kw):
    return build(application_stage=None, terraform=root, gates=gates, **kw)


@pytest.fixture(scope="module")
def tpl():
    return tf_build()


def actions(t, stage):
    return {a["Name"]: a for a in c._stage(t, stage)["Actions"]}


def reads(action):
    return [i["Name"] for i in action.get("InputArtifacts", [])]


def project(t, step):
    return c._project_of(t, step)


def policies_of(t, step):
    role = project(t, step)["ServiceRole"]["Fn::GetAtt"][0]
    return json.dumps([s for _, s in c._role_policy_statements(t, {role})])


# --- the root ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/abs", "../up", "a/../b", "", "-rf", "a b"])
def test_root_path_is_validated(path):
    with pytest.raises(ValueError, match="terraform root"):
        TerraformRoot(path)


@pytest.mark.parametrize("kw,err", [({"name": "a b"}, ValueError), ({"allow_destroy": "yes"}, TypeError),
                                    ({"plan_ec2": "small"}, TypeError), ({"var_files": "x.tfvars"}, TypeError),
                                    ({"var_files": ["../secrets.tfvars"]}, ValueError)])
def test_root_settings_are_validated(kw, err):
    with pytest.raises(err):
        TerraformRoot("infra", **kw)


def test_plan_and_apply_default_to_ec2():
    r = TerraformRoot()
    assert (r.plan_ec2, r.apply_ec2) == (Ec2("small", 60), Ec2("small", 120))
    assert r.state_key == "main/terraform.tfstate"


def test_plan_commands():
    cmds = plan_commands(TerraformRoot("infra", var_files=["prod.tfvars"]))
    assert cmds[0] == "cd infra"
    assert "-lockfile=readonly" in cmds[2] and "use_lockfile=true" in cmds[2]
    assert '"bucket=$TF_STATE_BUCKET"' in cmds[2] and "key=main/terraform.tfstate" in cmds[2]
    assert cmds[3] == "terraform plan -input=false -no-color -lock-timeout=5m -var-file=prod.tfvars -out=tfplan"
    assert cmds[4] == "terraform show -json tfplan > tfplan.json"
    assert cmds[-1] == "rm -rf .terraform"


def test_apply_is_exactly_the_saved_plan():
    cmds = apply_commands(ROOT)
    assert cmds[-1] == "terraform apply -input=false -no-color -lock-timeout=5m tfplan"
    assert not any("terraform plan" in x or "-auto-approve" in x or "-replace" in x or "-target" in x for x in cmds)


# --- the destroy check, on real Terraform plans -------------------------------------------------


def _check(fixture, allow, tmp_path):
    shutil.copy(FIXTURES / fixture, tmp_path / "tfplan.json")
    cmd = plan_check_commands(TerraformRoot(".", allow_destroy=allow), ".")[0]
    return subprocess.run(["bash", "-c", cmd], check=False, cwd=tmp_path, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "fixture,allow,ok,says",
    [
        ("create.json", False, True, "create=1"),
        ("replace.json", False, False, "replace: terraform_data.a"),
        ("replace.json", True, True, "replace=1"),
        ("delete.json", False, False, "delete: terraform_data.b"),
        ("delete.json", True, True, "delete=1"),
    ],
)
def test_destroy_check_on_real_plans(tmp_path, fixture, allow, ok, says):
    r = _check(fixture, allow, tmp_path)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert says in r.stdout


# --- the pipeline ------------------------------------------------------------------------------


def test_shape(tpl):
    gates, deploy = actions(tpl, "Gates"), actions(tpl, "Deploy")
    (out,) = gates["Plan-main"]["OutputArtifacts"]
    assert reads(gates["Plan-main"]) == ["Specter099_demo_Source"]
    assert reads(gates["Gate-tf-plan-check-main"]) == [out["Name"]]
    assert gates["Gate-tf-plan-check-main"]["RunOrder"] > gates["Plan-main"]["RunOrder"]
    assert reads(deploy["Deploy-main"]) == [out["Name"]], "apply reads the plan, nothing else"
    assert deploy["Approve-Deploy"]["RunOrder"] < deploy["Deploy-main"]["RunOrder"]


def test_plan_and_apply_run_on_ec2_and_the_check_on_lambda(tpl):
    for step, minutes in (("Plan-main", 60), ("Deploy-main", 120)):
        env = project(tpl, step)["Environment"]
        assert (env["Type"], env["Image"]) == ("LINUX_CONTAINER", EC2_IMAGE.image_id)
        assert project(tpl, step)["TimeoutInMinutes"] == minutes
    assert project(tpl, "Gate-tf-plan-check-main")["Environment"]["Type"] == "LINUX_LAMBDA_CONTAINER"


def test_state_bucket(tpl):
    (b,) = [r["Properties"] for r in tpl.find_resources("AWS::S3::Bucket").values()
            if r["Properties"].get("BucketNamePrefix") == "demo-tfstate"]
    assert b["BucketNamespace"] == "account-regional"
    assert b["VersioningConfiguration"]["Status"] == "Enabled"
    assert all("ExpirationInDays" not in rule for rule in b["LifecycleConfiguration"]["Rules"])
    (res,) = [r for r in tpl.find_resources("AWS::S3::Bucket").values()
              if r["Properties"].get("BucketNamePrefix") == "demo-tfstate"]
    assert res["DeletionPolicy"] == "Retain"


def test_who_can_touch_state(tpl):
    plan, apply, check = (policies_of(tpl, s) for s in ("Plan-main", "Deploy-main", "Gate-tf-plan-check-main"))
    assert "TerraformState" not in check
    assert "sqs:GetQueueAttributes" in plan and "sqs:CreateQueue" not in plan
    assert "sqs:CreateQueue" in apply
    c.assert_applies_plan(tpl, ROOT)  # only Deploy may PutObject on the state key


def test_contract_passes(tpl):
    c.assert_standard_contract(tpl, repo=REPO, gates=GATES, terraform=ROOT)


def test_pr_checks_never_plan_or_reach_state(tpl):
    c.assert_pr_roles_least_privilege(tpl, "demo")
    names = {r["Properties"]["Name"] for r in c.pr_projects(tpl, "demo").values()}
    assert not any("plan" in n for n in names)


def test_deploy_held_closed_by_default(tpl):
    c.assert_deploy_closed(tpl)


@pytest.mark.parametrize(
    "kw,match",
    [
        ({"deploy_step": DeployStep("app", ["./d.sh"])}, "exactly one of"),
        ({"assembly": Assembly()}, "no cloud assembly"),
        ({"gates": [*GATES, Gate("scan", ["ls cdk.out"], uses_assembly=True)]}, "no cloud assembly"),
        ({"gates": [*GATES, Gate("tf-plan-check-main", ["x"])]}, "is the construct's"),
    ],
)
def test_invalid_combinations(kw, match):
    with pytest.raises(ValueError, match=match):
        tf_build(**kw)


# --- the contract rejects each way of not applying the checked plan ----------------------------


def mutated(change):
    doc = copy.deepcopy(tf_build().to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def _stage_actions(res, stage):
    pipe = next(r for r in res.values() if r["Type"] == "AWS::CodePipeline::Pipeline")
    return {a["Name"]: a for s in pipe["Properties"]["Stages"] if s["Name"] == stage for a in s["Actions"]}


def _project(res, step):
    return next(r["Properties"] for r in res.values() if r["Type"] == "AWS::CodeBuild::Project"
                and str(r["Properties"].get("Description", "")).endswith(f"/{step}"))


def _edit_cmds(step, edit):
    def change(res):
        p = _project(res, step)
        spec = json.loads(p["Source"]["BuildSpec"])
        edit(spec["phases"]["build"]["commands"])
        p["Source"]["BuildSpec"] = json.dumps(spec)
    return change


def _role_policy(res, step):
    role = _project(res, step)["ServiceRole"]["Fn::GetAtt"][0]
    return next(r["Properties"]["PolicyDocument"]["Statement"] for r in res.values()
                if r["Type"] == "AWS::IAM::Policy" and {x["Ref"] for x in r["Properties"]["Roles"]} == {role})


def _state_key_resource(res):
    for st in _role_policy(res, "Deploy-main"):
        if st["Action"] == "s3:PutObject" and "TerraformState" in json.dumps(st["Resource"]):
            return st["Resource"]
    raise AssertionError("no state write found")


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: _stage_actions(r, "Deploy")["Deploy-main"].__setitem__(
            "InputArtifacts", [{"Name": "Specter099_demo_Source"}]), "must read the plan artifact only"),
        (_edit_cmds("Deploy-main", lambda cmds: cmds.insert(-1, "terraform plan -out=tfplan")), "runs more or other"),
        (_edit_cmds("Deploy-main", lambda cmds: cmds.__setitem__(-1, "terraform apply -auto-approve")), "runs more or other"),
        (lambda r: _project(r, "Deploy-main")["Environment"].update(
            {"Type": "LINUX_LAMBDA_CONTAINER", "Image": "x"}), "is not on EC2"),
        (lambda r: _project(r, "Plan-main")["Environment"].update(
            {"Type": "LINUX_LAMBDA_CONTAINER", "Image": "x"}), "is not on EC2"),
        (lambda r: _stage_actions(r, "Gates")["Gate-tf-plan-check-main"].__setitem__(
            "InputArtifacts", [{"Name": "Specter099_demo_Source"}]), "does not read exactly the plan"),
        (lambda r: _role_policy(r, "Plan-main").append(
            {"Effect": "Allow", "Action": "s3:PutObject", "Resource": _state_key_resource(r)}), "Plan-main can write"),
        (lambda r: _role_policy(r, "Gate-tf-plan-check-main").append(
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": _state_key_resource(r)}), "can reach the state bucket"),
    ],
)
def test_assert_applies_plan_rejects(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_applies_plan(mutated(change), ROOT)


def test_plan_without_its_pinned_terraform_toolchain_fails_the_contract():
    t = mutated(_edit_cmds("Plan-main", lambda cmds: [cmds.remove(x) for x in list(cmds) if "terraform.zip" in x]))
    with pytest.raises(AssertionError, match="TERRAFORM toolchain"):
        c.assert_standard_contract(t, repo=REPO, gates=GATES, terraform=ROOT)
