"""EC2 compute: opt-in per step; everything else stays on Lambda, and the contract knows which."""

import copy

import pytest
from aws_cdk.assertions import Template
from test_assembly import ASM_GATES, asm_build
from test_pipeline import GATES, build

from specter_pipeline import Assembly, DeployStep, Ec2, Gate, PostDeployStep
from specter_pipeline import testing as c
from specter_pipeline.compute import EC2_IMAGE

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
    if cls is Gate:  # pull_request is still the eighth positional field
        assert Gate("s", ["true"], Gate.runtime, (), {}, 2, (), False).pull_request is False


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


def test_the_same_commands_run_on_ec2():
    lam = c.step_commands(build(gates=[*GATES, Gate("tf-plan", ["terraform plan"])]), "Gate-tf-plan")
    assert c.step_commands(build(gates=[*GATES, TF]), "Gate-tf-plan") == lam


def test_contract_infers_ec2_gates():
    t = build(gates=[*GATES, TF])
    c.assert_standard_contract(t, repo=REPO, gates=[*GATES, TF], deploy_stage="Prod", node_gates=NODE_GATES)


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
    ],
)
def test_an_ec2_step_is_still_held_to_the_rules(change, message):
    doc = copy.deepcopy(build(gates=[*GATES, TF]).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and c._project_step_name(r["Properties"]) == "Gate-tf-plan":
            change(r["Properties"])
    with pytest.raises(AssertionError, match=message):
        c.assert_lambda_compute(Template.from_json(doc), ["Gate-tf-plan", "demo-pr-tf-plan"])


def test_a_legacy_deploy_step_on_ec2():
    t = build(
        application_stage=None,
        deploy_step=DeployStep("app", ["cdk deploy AppStack --require-approval never"], ec2=Ec2("small", 120)),
    )
    assert project(t, "Deploy-app")["TimeoutInMinutes"] == 120
    c.assert_standard_contract(t, repo=REPO, gates=GATES, node_gates=NODE_GATES, ec2_steps=["Deploy-app"])


def test_the_construct_only_deploy_and_the_assembly_on_ec2():
    t = asm_build(
        assembly=Assembly(ec2=Ec2("large", 30)),
        deploy_step=DeployStep("app", stacks=["AppStack"], ec2=Ec2("small", 240)),
    )
    assert project(t, "Deploy-app")["Environment"]["Image"] == EC2_IMAGE.image_id
    assert project(t, "Assemble")["Environment"]["ComputeType"] == "BUILD_GENERAL1_LARGE"
    # the assembly gate's PR check synthesizes, so it follows the assembly onto EC2
    assert project(t, "demo-pr-scan")["Environment"]["Type"] == "LINUX_CONTAINER"
    c.assert_standard_contract(
        t, repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES,
        ec2_steps=["Deploy-app", "Assemble", "demo-pr-scan"],
    )
