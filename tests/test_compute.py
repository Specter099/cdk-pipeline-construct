"""EC2 compute: opt-in per step; everything else stays on Lambda, and the contract knows which."""

import copy
import inspect

import pytest
from aws_cdk.assertions import Template
from test_assembly import ASM_GATES, asm_build
from test_pipeline import GATES, build

from specter_pipeline import Assembly, DeployStep, Ec2, Gate, PostDeployStep, Runtime
from specter_pipeline import testing as c
from specter_pipeline.compute import EC2_IMAGE, PR_EC2_CONCURRENT_BUILDS
from specter_pipeline.runtimes import DEFAULT_NODE_VERSION, NODE_LINUX_X64_SHA256

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
    # the venv is built from python3.12 explicitly, and the build stops if the image lacks it
    assert ec2[0] == 'command -v python3.12 >/dev/null || { echo "this image has no python3.12; EC2 steps need it"; exit 1; }'
    assert "python3.12 -m venv /tmp/venv" in ec2 and "python3 -m venv /tmp/venv" in lam
    assert ec2[1:] == [x.replace("python3 -m venv", "python3.12 -m venv") for x in lam]


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
    c.assert_standard_contract(t, repo=REPO, gates=GATES, node_gates=NODE_GATES, ec2_steps=["Deploy-app"])


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
    c.assert_standard_contract(t, repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES,
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


def test_ec2_pr_checks_are_concurrency_capped_and_lambda_ones_are_not():
    t = build(gates=[*GATES, TF])
    assert project(t, "demo-pr-tf-plan")["ConcurrentBuildLimit"] == PR_EC2_CONCURRENT_BUILDS
    assert "ConcurrentBuildLimit" not in project(t, "demo-pr-unit")


def test_a_deploy_step_on_ec2_moves_its_pre_deploy_action_too_and_the_contract_knows():
    asm = Assembly()
    deploy = DeployStep("app", ["./migrate.sh"], stacks=["AppStack"], ec2=Ec2("small", 240))
    t = asm_build(assembly=asm, deploy_step=deploy)
    assert project(t, "PreDeploy-app")["Environment"]["Type"] == "LINUX_CONTAINER"
    c.assert_standard_contract(t, repo=REPO, gates=ASM_GATES, node_gates=NODE_GATES,
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
