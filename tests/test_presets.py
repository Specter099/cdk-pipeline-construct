"""python_cdk_gates reproduces the shared CDK review checks, and Gate IAM/PR rules hold."""

import shutil
import subprocess

import pytest
from aws_cdk import aws_iam as iam
from test_pipeline import _step_build

from specter_pipeline import Gate, Runtime
from specter_pipeline import presets as p
from specter_pipeline import testing as c
from specter_pipeline.presets import python_cdk_gates

NAMES = ["lint", "sast", "tests", "dependency-audit", "cdk-synth", "access-analyzer"]


def _by_name(**kw):
    return {g.name: g for g in python_cdk_gates(**kw)}


def test_default_gates_match_the_shared_workflow_in_order():
    assert [g.name for g in python_cdk_gates()] == NAMES


def test_tests_and_access_analyzer_are_optional():
    assert "tests" not in _by_name(tests=False)
    assert "access-analyzer" not in _by_name(access_analyzer=False)
    assert [g.name for g in python_cdk_gates(tests=False, access_analyzer=False)] == [
        "lint",
        "sast",
        "dependency-audit",
        "cdk-synth",
    ]


def test_tool_versions_are_pinned_like_the_shared_workflow():
    g = _by_name()
    assert "bandit==1.9.4" in " ".join(g["sast"].install)
    assert "pip-audit==2.10.1" in " ".join(g["dependency-audit"].install)
    assert "boto3==1.43.102" in " ".join(g["access-analyzer"].install)
    assert "cfn-flip==1.3.0" in " ".join(g["access-analyzer"].install)
    assert any("aws-cdk@2.1149.0" in cmd for cmd in g["cdk-synth"].install)


def test_gates_that_run_cdk_or_import_it_have_node():
    c.assert_gates_have_node(
        python_cdk_gates(), ["tests", "cdk-synth", "access-analyzer"]
    )
    assert _by_name()["lint"].runtime is Runtime.PYTHON  # no Node needed


def test_bandit_excludes_and_requirement_paths_are_configurable():
    g = _by_name(
        bandit_excludes=("./x",),
        runtime_requirements="infra/req.txt",
        dev_requirements="infra/dev.txt",
    )
    assert g["sast"].commands == ["bandit -r . -ll --exclude ./x"]
    assert g["dependency-audit"].commands == ["pip-audit -r infra/req.txt"]
    assert "pip install -r infra/dev.txt" in g["lint"].install
    assert "pip install -r infra/req.txt" in g["cdk-synth"].install


def test_env_reaches_the_steps_that_run_cdk_or_tests():
    g = _by_name(env={"JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1"})
    for name in ("tests", "cdk-synth", "access-analyzer"):
        assert g[name].environment["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] == "1"
    assert g["tests"].environment["CI"] == "true"


# --- Access Analyzer gate -----------------------------------------------------------------


def test_access_analyzer_script_is_pinned_to_a_commit_and_verified_before_it_runs():
    cmds = _by_name()["access-analyzer"].commands
    text = "\n".join(cmds)
    assert p.ACCESS_ANALYZER_COMMIT in text and "/main/" not in text, (
        "must pin a commit, not a branch"
    )
    assert len(p.ACCESS_ANALYZER_COMMIT) == 40 and len(p.ACCESS_ANALYZER_SHA256) == 64
    order = [
        i
        for i, cmd in enumerate(cmds)
        if "curl" in cmd or "sha256sum -c" in cmd or "python3 -I /tmp/aa/check" in cmd
    ]
    assert order == sorted(order) and len(order) == 3
    assert cmds[0] == "cdk synth --quiet", (
        "templates must exist before they are scanned"
    )


def test_access_analyzer_has_exactly_one_iam_permission_and_never_runs_on_pull_requests():
    g = _by_name()
    aa = g["access-analyzer"]
    assert [s.to_statement_json()["Action"] for s in aa.policy_statements] == [
        "access-analyzer:CheckNoPublicAccess"
    ]
    assert aa.runs_on_pull_requests is False
    assert all(g[n].runs_on_pull_requests for n in NAMES if n != "access-analyzer")
    assert all(not g[n].policy_statements for n in NAMES if n != "access-analyzer")


def test_a_gate_with_its_own_iam_cannot_be_forced_onto_the_pr_path():
    stmt = iam.PolicyStatement(actions=["s3:ListAllMyBuckets"], resources=["*"])
    with pytest.raises(ValueError, match="cannot run on pull requests"):
        Gate("g", ["true"], policy_statements=[stmt], pull_request=True)
    assert Gate("g", ["true"], policy_statements=[stmt]).runs_on_pull_requests is False
    assert Gate("g", ["true"]).runs_on_pull_requests is True
    assert Gate("g", ["true"], pull_request=False).runs_on_pull_requests is False


# --- inside a synthesized pipeline ------------------------------------------------------------


def _preset_pipeline():
    return _step_build(gates=python_cdk_gates(env={"X": "1"}))


def test_pipeline_with_the_preset_satisfies_the_standard_contract():
    t = _preset_pipeline()
    c.assert_standard_contract(
        t,
        repo="Specter099/demo",
        gates=python_cdk_gates(env={"X": "1"}),
        node_gates=["tests", "cdk-synth", "access-analyzer"],
    )


def test_pr_checks_exclude_the_access_analyzer_gate_and_its_permission_stays_off_the_pr_roles():
    t = _preset_pipeline()
    names = {r["Properties"]["Name"] for r in c.pr_projects(t, "demo").values()}
    assert "demo-pr-access-analyzer" not in names
    assert names == {f"demo-pr-{n}" for n in NAMES if n != "access-analyzer"}
    c.assert_pr_roles_least_privilege(
        t, "demo"
    )  # would fail if the analyzer permission reached a PR role
    allowed = c.CDK_INTERNAL_WILDCARDS | {"access-analyzer:CheckNoPublicAccess"}
    c.assert_no_wildcard_resources(t, allowed_actions=allowed)
    # anything beyond the framework's own wildcard and the analyzer's must still be caught
    with pytest.raises(AssertionError, match="wildcard resource"):
        c.assert_no_wildcard_resources(t, allowed_actions=c.CDK_INTERNAL_WILDCARDS)
    with pytest.raises(AssertionError, match="wildcard resource"):
        c.assert_no_wildcard_resources(t)


def test_the_pipeline_gate_for_access_analyzer_does_get_the_permission():
    t = _preset_pipeline()
    holders = [
        pol
        for pol in t.find_resources("AWS::IAM::Policy").values()
        if "access-analyzer:CheckNoPublicAccess"
        in str(pol["Properties"]["PolicyDocument"])
    ]
    assert len(holders) == 1


# --- the shell idioms, executed for real ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "pytest_rc,expected_ok", [(0, True), (5, True), (1, False), (2, False)]
)
def test_tests_gate_treats_no_tests_collected_as_a_skip_but_real_failures_as_failures(
    pytest_rc, expected_ok
):
    command = _by_name()["tests"].commands[0]
    script = f"pytest() {{ return {pytest_rc}; }}; {command}"
    assert (
        subprocess.run(["bash", "-c", script], check=False).returncode == 0
    ) is expected_ok


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_lint_gate_skips_when_ruff_is_not_installed():
    command = _by_name()["lint"].commands[0]
    result = subprocess.run(
        [shutil.which("bash"), "-c", command],
        env={"PATH": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "skipping lint" in result.stdout
