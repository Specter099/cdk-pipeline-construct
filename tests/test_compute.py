"""EC2 compute: opt-in per step; everything else stays on Lambda, and the contract knows which."""

import copy
import inspect
import json
import shutil
import subprocess

import pytest
from aws_cdk.assertions import Template
from test_assembly import ASM_GATES, asm_build
from test_pipeline import GATES, POST, build

from specter_pipeline import Assembly, DeployStep, Ec2, Gate, PostDeployStep, Runtime
from specter_pipeline import testing as c
from specter_pipeline.compute import EC2_IMAGE
from specter_pipeline.runtimes import (
    DEFAULT_NODE_VERSION,
    NODE_LINUX_X64_SHA256,
    ec2_python_select,
)

REPO = "Specter099/demo"
NODE_GATES = ["synth-check", "lint"]
TF = Gate("tf-plan", ["terraform plan"], ec2=Ec2("medium", 90))


def project(t: Template, step: str) -> dict:
    (props,) = [
        r["Properties"] for r in c.projects(t).values() if c._project_step_name(r["Properties"]) == step
    ]
    return props


# --- the Ec2 setting ---------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [{"size": "huge"}, {"timeout_minutes": 4}, {"timeout_minutes": 481},
                                {"timeout_minutes": 60.0}, {"timeout_minutes": True}])
def test_ec2_settings_are_validated(kw):
    with pytest.raises(ValueError):
        Ec2(**kw)


def test_ec2_must_be_an_ec2():
    with pytest.raises(TypeError, match="ec2 must be an Ec2"):
        Gate("x", ["true"], ec2="medium")


@pytest.mark.parametrize("cls", [Gate, DeployStep, PostDeployStep, Assembly])
def test_ec2_is_keyword_only_and_keeps_positional_order(cls):
    step = cls("s", ["true"], ec2=Ec2())
    assert step.ec2 == Ec2()
    params = inspect.signature(cls).parameters
    assert params["ec2"].kind is inspect.Parameter.KEYWORD_ONLY
    # the base's seven positional fields come first; subclass fields keep their places after them
    names = [n for n, prm in params.items() if prm.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert names[:7] == ["name", "commands", "runtime", "install", "env", "memory_gb", "policy_statements"]
    expected_next = {Gate: "pull_request", Assembly: "output_dir", DeployStep: "stacks", PostDeployStep: None}[cls]
    assert (names[7] if len(names) > 7 else None) == expected_next


# --- what it synthesizes -----------------------------------------------------------------------


def test_an_ec2_gate_runs_on_ec2_in_the_pipeline_and_on_pull_requests():
    t = build(gates=[*GATES, TF])
    for step in ("Gate-tf-plan", "demo-pr-tf-plan"):
        p = project(t, step)
        assert p["Environment"]["Type"] == "LINUX_CONTAINER"
        assert p["Environment"]["Image"] == EC2_IMAGE.image_id
        assert p["Environment"]["ComputeType"] == "BUILD_GENERAL1_MEDIUM"
        assert p["TimeoutInMinutes"] == 90
        assert not p["Environment"].get("PrivilegedMode")
    # everything else is untouched
    for step in ("Gate-unit", "demo-pr-unit"):
        assert project(t, step)["Environment"]["Type"] == "LINUX_LAMBDA_CONTAINER"
        assert "TimeoutInMinutes" not in project(t, step)


def test_ec2_steps_bring_the_same_pinned_toolchain_not_the_image_s():
    lam = c.step_commands(build(gates=[*GATES, Gate("tf-plan", ["terraform plan"])]), "Gate-tf-plan")
    ec2 = c.step_commands(build(gates=[*GATES, TF]), "Gate-tf-plan")
    select = ec2_python_select()
    # 3.12 is selected through pyenv and proven before the venv is built from it
    assert ec2[: len(select)] == select
    assert 'export PYENV_VERSION="$PYTHON_312_VERSION"' in select
    assert ec2[len(select):] == [x.replace("python3 -m venv", "python3.12 -m venv") for x in lam]


def _run_select(tmp_path, env_version, reported):
    """Run the selection with a stand-in python3.12 that reports ``reported``."""
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    fake = shim / "python3.12"
    fake.write_text(f"#!/bin/sh\nexec python3 -c 'import sys; sys.version_info = {reported!r}; '\"$2\"\n")
    fake.write_text("#!/usr/bin/env python3\nimport sys\n" f"sys.version_info = {reported!r}\n" "exec(sys.argv[2])\n")
    fake.chmod(0o755)
    env = {"PATH": f"{shim}:/usr/bin:/bin"}
    if env_version is not None:
        env["PYTHON_312_VERSION"] = env_version
    return subprocess.run(["bash", "-ec", "\n".join(ec2_python_select())], check=False,
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "env_version,reported,ok,says",
    [
        ("3.12.15", (3, 12, 15), True, ""),
        (None, (3, 12, 15), False, "PYTHON_312_VERSION is unset"),
        # the 6.0 image's shim case: python3.12 resolves to another interpreter
        ("3.12.15", (3, 14, 9), False, "does not run Python 3.12"),
    ],
)
def test_ec2_python_selection_fails_closed(tmp_path, env_version, reported, ok, says):
    r = _run_select(tmp_path, env_version, reported)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert says in r.stdout + r.stderr


@pytest.mark.parametrize("runtime", [Runtime.NODE, Runtime.PYTHON_NODE])
def test_node_on_ec2_is_the_pinned_tarball(runtime):
    cmds = c.step_commands(build(gates=[*GATES, Gate("n", ["npm run x"], runtime, ec2=Ec2())]), "Gate-n")
    sha = NODE_LINUX_X64_SHA256[DEFAULT_NODE_VERSION]
    assert f'echo "{sha}  /tmp/node.tgz" | sha256sum -c -' in cmds
    assert cmds.index("node --version") < cmds.index("npm run x")


def test_ec2_uses_the_amazon_linux_2023_6_image():
    assert EC2_IMAGE.image_id == "aws/codebuild/amazonlinux-x86_64-standard:6.0"


def test_contract_infers_ec2_gates():
    t = build(gates=[*GATES, TF])
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=[*GATES, TF], deploy_stage="Prod", node_gates=NODE_GATES)


def test_an_undeclared_ec2_step_fails_the_contract():
    t = build(gates=[*GATES, TF])
    with pytest.raises(AssertionError, match="not declared in ec2_steps"):
        c.assert_lambda_compute(t)


def test_a_declared_ec2_step_that_is_missing_or_on_lambda_fails():
    t = build()
    with pytest.raises(AssertionError, match="no CodeBuild project for EC2 steps"):
        c.assert_lambda_compute(t, ["Gate-nope"])
    with pytest.raises(AssertionError, match="declared EC2 but is not on EC2"):
        c.assert_lambda_compute(t, ["Gate-unit"])


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda p: p.pop("TimeoutInMinutes"), "no explicit timeout"),
        (lambda p: p["Environment"].__setitem__("Image", "aws/codebuild/standard:7.0"), "not the pinned EC2 image"),
        (lambda p: p["Environment"].__setitem__("PrivilegedMode", True), "privileged"),
        (lambda p: p.__setitem__("Cache", {"Type": "S3", "Location": "b/x"}), "enables caching"),
        (lambda p: p.__setitem__("VpcConfig", {"VpcId": "vpc-1"}), "VpcConfig"),
    ],
)
@pytest.mark.parametrize("which", ["Gate-tf-plan", "demo-pr-tf-plan"])
def test_an_ec2_step_is_still_held_to_the_rules(change, message, which):
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and c._project_step_name(r["Properties"]) == which:
            change(r["Properties"])
    with pytest.raises(AssertionError, match=message):
        c.assert_lambda_compute(Template.from_json(doc), ["Gate-tf-plan", "demo-pr-tf-plan"])


def test_a_legacy_deploy_step_on_ec2():
    t = build(
        application_stage=None,
        deploy_step=DeployStep("app", ["cdk deploy AppStack --require-approval never"], ec2=Ec2("small", 120)),
    )
    assert project(t, "Deploy-app")["TimeoutInMinutes"] == 120
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=GATES, node_gates=NODE_GATES, ec2_steps=["Deploy-app"])


def test_the_construct_only_deploy_and_the_assembly_on_ec2():
    asm = Assembly(ec2=Ec2("large", 30))
    deploy = DeployStep("app", stacks=["AppStack"], ec2=Ec2("small", 240))
    t = asm_build(assembly=asm, deploy_step=deploy)
    assert project(t, "Deploy-app")["Environment"]["Image"] == EC2_IMAGE.image_id
    assert project(t, "Assemble")["Environment"]["ComputeType"] == "BUILD_GENERAL1_LARGE"
    # the deploy runs the pinned Node, not the image's
    cmds = c.step_commands(t, "Deploy-app")
    assert cmds[0].startswith("curl -fsSL -o /tmp/node.tgz") and "node --version" in cmds
    # the assembly gate's PR check synthesizes, so it follows the assembly onto EC2
    assert project(t, "demo-pr-scan")["Environment"]["Type"] == "LINUX_CONTAINER"
    # inferred from the steps: no ec2_steps needed
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES,
                               assembly=asm, deploy_step=deploy)


def test_an_assembly_gate_pr_check_gets_the_larger_size_and_both_timeouts():
    scan = Gate("scan", ["ls cdk.out"], uses_assembly=True, ec2=Ec2("small", 5))
    asm = Assembly(ec2=Ec2("2xlarge", 120))
    t = asm_build(assembly=asm, gates=[*GATES, scan])
    pr = project(t, "demo-pr-scan")
    assert pr["Environment"]["ComputeType"] == "BUILD_GENERAL1_2XLARGE"
    assert pr["TimeoutInMinutes"] == 125
    # the pipeline gate itself does not synthesize, so it keeps its own size
    assert project(t, "Gate-scan")["Environment"]["ComputeType"] == "BUILD_GENERAL1_SMALL"
    assert Ec2("large", 400).larger(Ec2("small", 400)) == Ec2("large", 480)


def test_no_pr_check_has_a_concurrency_limit():
    """CodeBuild drops builds over a project's limit instead of queueing them, which would leave a
    PR without its status check."""
    t = build(gates=[*GATES, TF])
    for r in c.pr_projects(t, "demo").values():
        assert "ConcurrentBuildLimit" not in r["Properties"]


@pytest.mark.parametrize(
    "gate_ec2,asm_ec2,asm_mem,size,timeout",
    [
        (Ec2("small", 10), None, 10, "BUILD_GENERAL1_LARGE", 25),   # Lambda 10 GB synth: >= 10 GB
        (None, Ec2("large", 30), 4, "BUILD_GENERAL1_LARGE", 45),    # Lambda gate adds 15 min
        (Ec2("small", 10), None, 2, "BUILD_GENERAL1_SMALL", 25),
    ],
)
def test_mixed_lambda_and_ec2_pr_sizing(gate_ec2, asm_ec2, asm_mem, size, timeout):
    scan = Gate("scan", ["ls cdk.out"], uses_assembly=True, ec2=gate_ec2)
    t = asm_build(assembly=Assembly(ec2=asm_ec2, memory_gb=asm_mem), gates=[*GATES, scan])
    pr = project(t, "demo-pr-scan")
    assert (pr["Environment"]["ComputeType"], pr["TimeoutInMinutes"]) == (size, timeout)


def test_an_ec2_step_whose_venv_uses_the_image_python_fails_the_contract():
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and c._project_step_name(r["Properties"]) == "Gate-tf-plan":
            spec = json.loads(r["Properties"]["Source"]["BuildSpec"])
            cmds = spec["phases"]["build"]["commands"]
            spec["phases"]["build"]["commands"] = [x for x in cmds if x not in ec2_python_select()]
            r["Properties"]["Source"]["BuildSpec"] = json.dumps(spec)
    with pytest.raises(AssertionError, match="does not start with the pinned toolchain"):
        c.assert_lambda_compute(Template.from_json(doc), ["Gate-tf-plan", "demo-pr-tf-plan"])


def _edit_cmds(t, step, edit):
    doc = copy.deepcopy(t.to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and c._project_step_name(r["Properties"]) == step:
            spec = json.loads(r["Properties"]["Source"]["BuildSpec"])
            edit(spec["phases"]["build"]["commands"])
            r["Properties"]["Source"]["BuildSpec"] = json.dumps(spec)
    return Template.from_json(doc)


NODE_GATE = Gate("lint-ec2", ["npm run lint"], Runtime.NODE, ec2=Ec2())


@pytest.mark.parametrize(
    "gate,step,edit",
    [
        # (i) the image's Node instead of the pinned tarball
        (NODE_GATE, "Gate-lint-ec2", lambda cmds: [cmds.remove(x) for x in list(cmds) if "node" in x and x != "npm run lint"]),
        # (ii) no venv at all
        (TF, "Gate-tf-plan", lambda cmds: [cmds.remove(x) for x in list(cmds) if "venv" in x]),
        # (iii) another Python selected between the pin and the venv
        (TF, "Gate-tf-plan", lambda cmds: cmds.insert(3, 'export PYENV_VERSION="$PYTHON_314_VERSION"')),
    ],
)
def test_ec2_toolchain_tampering_fails_the_contract(gate, step, edit):
    t = _edit_cmds(build(gates=[*GATES, gate]), step, edit)
    with pytest.raises(AssertionError, match="does not start with the pinned toolchain"):
        c.assert_lambda_compute(t, [step, f"demo-pr-{gate.name}"])


def test_a_pr_check_with_a_concurrency_limit_fails_the_contract():
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and r["Properties"].get("Name") == "demo-pr-tf-plan":
            r["Properties"]["ConcurrentBuildLimit"] = 1
    with pytest.raises(AssertionError, match="concurrency limit"):
        c.assert_pr_checks(Template.from_json(doc), [*GATES, TF], "demo")


def test_ec2_steps_with_cdk_tokens_in_their_commands_pass_the_contract():
    """Regression: a token makes the BuildSpec an Fn::Join; the contract must read it, not crash."""
    import aws_cdk as cdk

    tok = cdk.Fn.import_value("SiteBucket")
    gate = Gate("tokgate", [f"echo {tok}"], ec2=Ec2("small", 30))
    post = PostDeployStep("frontend", [f"aws s3 sync dist s3://{tok}"], ec2=Ec2("small", 30))
    t = build(gates=[*GATES, gate], post_deploy=[post])
    assert isinstance(project(t, "Gate-tokgate")["Source"]["BuildSpec"], dict)
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=[*GATES, gate], deploy_stage="Prod",
                               node_gates=NODE_GATES, ec2_steps=["Post-frontend"])


def test_a_consumer_owned_ec2_project_is_not_held_to_the_construct_toolchain():

    t0 = build()
    doc = copy.deepcopy(t0.to_json())
    doc["Resources"]["Mine"] = {"Type": "AWS::CodeBuild::Project", "Properties": {
        "Name": "my-own-ec2", "Source": {"Type": "CODEPIPELINE", "BuildSpec": "buildspec.yml"},
        "Environment": {"Type": "LINUX_CONTAINER", "Image": EC2_IMAGE.image_id,
                        "ComputeType": "BUILD_GENERAL1_SMALL", "EnvironmentVariables": []},
        "TimeoutInMinutes": 30, "ServiceRole": "x", "Artifacts": {"Type": "CODEPIPELINE"}}}
    c.assert_lambda_compute(Template.from_json(doc), ["my-own-ec2"])


def test_pre_deploy_on_ec2_is_inferred_from_the_template():
    deploy = DeployStep("app", ["./migrate.sh"], stacks=["AppStack"], ec2=Ec2("small", 60))
    t = asm_build(deploy_step=deploy)
    # assembly= not passed: the PreDeploy action is still found
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES, deploy_step=deploy)


def test_a_deploy_step_on_ec2_moves_its_pre_deploy_action_too_and_the_contract_knows():
    asm = Assembly()
    deploy = DeployStep("app", ["./migrate.sh"], stacks=["AppStack"], ec2=Ec2("small", 240))
    t = asm_build(assembly=asm, deploy_step=deploy)
    assert project(t, "PreDeploy-app")["Environment"]["Type"] == "LINUX_CONTAINER"
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES,
                               assembly=asm, deploy_step=deploy)


def test_a_declared_name_matching_two_projects_is_ambiguous():
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    res = doc["Resources"]
    (lid,) = [k for k, r in res.items() if r["Type"] == "AWS::CodeBuild::Project"
              and c._project_step_name(r["Properties"]) == "Gate-tf-plan"]
    res["Dup"] = copy.deepcopy(res[lid])
    with pytest.raises(AssertionError, match="ambiguous"):
        c.assert_lambda_compute(Template.from_json(doc), ["Gate-tf-plan", "demo-pr-tf-plan"])


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda e: e.__setitem__("Image", EC2_IMAGE.image_id), "Node Lambda image"),
        (lambda e: (e.__setitem__("Type", "LINUX_CONTAINER"), e.__setitem__("Image", EC2_IMAGE.image_id)), "EC2 size"),
    ],
)
def test_the_deploy_action_image_and_compute_type_must_match(change, message):
    doc = copy.deepcopy(asm_build().to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and c._project_step_name(r["Properties"]) == "Deploy-app":
            change(r["Properties"]["Environment"])
    with pytest.raises(AssertionError, match=message):
        c.assert_builds_once(Template.from_json(doc), ASM_GATES)


def test_memory_is_not_validated_against_lambda_sizes_on_ec2():
    assert Gate("g", ["true"], memory_gb=16, ec2=Ec2("large")).ec2 == Ec2("large")
    with pytest.raises(ValueError, match="Lambda compute memory"):
        Gate("g", ["true"], memory_gb=16)


def test_a_non_string_size_is_a_clear_error():
    with pytest.raises(ValueError, match="EC2 size"):
        Ec2(size=["small"])


# --- second-round review: two-runtime preludes, PR-check identity, token buildspecs ------------

PN = Gate("pn", ["cdk synth"], runtime=Runtime.PYTHON_NODE, ec2=Ec2("small", 30))
PN_GATES = [*GATES, PN]


def _drop(pred):
    return lambda cmds: [cmds.remove(x) for x in list(cmds) if pred(x)]


@pytest.mark.parametrize(
    "edit",
    [
        _drop(lambda x: "venv" in x or "PIP_" in x or "PYTHON" in x or "jsii" in x or "pip" in x.lower()),
        _drop(lambda x: "node" in x),
    ],
    ids=["python-half-removed", "node-half-removed"],
)
@pytest.mark.parametrize("step", ["Gate-pn", "demo-pr-pn"])
def test_a_two_runtime_step_missing_half_its_toolchain_fails_the_contract(edit, step):
    t = _edit_cmds(build(gates=PN_GATES), step, edit)
    with pytest.raises(AssertionError, match="toolchain"):
        c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=PN_GATES, deploy_stage="Prod", node_gates=[*NODE_GATES, "pn"])


def test_without_known_runtimes_stray_toolchain_after_the_prelude_fails():
    t = _edit_cmds(build(gates=PN_GATES), "Gate-pn",
                   lambda cmds: cmds.insert(len(cmds) - 1, 'export PYENV_VERSION="$PYTHON_314_VERSION"'))
    with pytest.raises(AssertionError, match="outside the pinned prelude"):
        c.assert_lambda_compute(t, ["Gate-pn", "demo-pr-pn"])


def test_a_pr_check_with_its_description_removed_is_still_held_to_the_toolchain():
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and r["Properties"].get("Name") == "demo-pr-tf-plan":
            r["Properties"].pop("Description")
            spec = json.loads(r["Properties"]["Source"]["BuildSpec"])
            spec["phases"]["build"]["commands"] = ["python3 -m venv /tmp/venv", "terraform plan"]
            r["Properties"]["Source"]["BuildSpec"] = json.dumps(spec)
    with pytest.raises(AssertionError, match="toolchain"):
        c.assert_lambda_compute(Template.from_json(doc), ["Gate-tf-plan", "demo-pr-tf-plan"], pr_prefix="demo")


def test_a_consumer_project_described_as_a_pr_check_is_not_held_when_the_prefix_is_known():
    doc = copy.deepcopy(build().to_json())
    doc["Resources"]["Mine"] = {"Type": "AWS::CodeBuild::Project", "Properties": {
        "Name": "mine", "Description": "PR check of my own", "ServiceRole": "x",
        "Source": {"Type": "GITHUB", "BuildSpec": "buildspec.yml"}, "Artifacts": {"Type": "NO_ARTIFACTS"},
        "Environment": {"Type": "LINUX_CONTAINER", "Image": EC2_IMAGE.image_id,
                        "ComputeType": "BUILD_GENERAL1_SMALL"}, "TimeoutInMinutes": 30}}
    c.assert_lambda_compute(Template.from_json(doc), ["mine"], pr_prefix="demo")


def test_assert_builds_once_reads_token_buildspecs():
    import aws_cdk as cdk

    tok = cdk.Fn.import_value("X")
    deploy = DeployStep("app", [f"echo {tok}"], stacks=["AppStack"])
    scan = Gate("scan", [f"ls cdk.out && echo {tok}"], uses_assembly=True)
    t = asm_build(deploy_step=deploy, gates=[*GATES, scan])
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=[*GATES, scan], node_gates=NODE_GATES,
                               deploy_step=deploy)


@pytest.mark.parametrize("spec", [{"Fn::Join": ["", "notalist", "extra"]}, {"Fn::Join": "x"}, 42, "not json", "[1]"])
def test_unreadable_buildspecs_read_as_none(spec):
    assert c._read_buildspec({"Source": {"BuildSpec": spec}}) is None



def test_a_post_deploy_step_may_make_its_own_venv_when_its_runtime_is_known():
    post = PostDeployStep("fe", ["python -m venv /tmp/tools && /tmp/tools/bin/pip install x"],
                          Runtime.PYTHON, ec2=Ec2("small", 30))
    t = build(post_deploy=[post])
    c.assert_standard_contract(t, repo=REPO, gates=GATES, deploy_stage="Prod",
                               node_gates=NODE_GATES, post_deploy=[post])


def test_a_token_in_the_construct_deploy_buildspec_is_read():
    import aws_cdk as cdk

    deploy = DeployStep("app", stacks=["AppStack"],
                        deploy_args=["--context", f"k={cdk.Fn.import_value('X')}"])
    t = asm_build(deploy_step=deploy)
    assert isinstance(project(t, "Deploy-app")["Source"]["BuildSpec"], dict)
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES, deploy_step=deploy)


@pytest.mark.parametrize("props", [{"Source": {"BuildSpec": {"Fn::Join": [1, ["a"]]}}},
                                   {"Source": {"BuildSpec": {"Fn::Join": [None, ["{}"]]}}},
                                   {"Source": None}, {}])
def test_more_malformed_buildspecs_read_as_none(props):
    assert c._read_buildspec(props) is None
