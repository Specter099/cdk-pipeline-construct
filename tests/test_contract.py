"""The reusable contract helpers pass on good pipelines and FAIL on each violation they claim to catch."""

import pytest
from test_pipeline import CONN, GATES, POST, _step_build, build

from specter_pipeline import testing as c

REPO = "Specter099/demo"
NODE_GATES = ["synth-check", "lint"]


def _contract(template, **kw):
    kw.setdefault("repo", REPO)
    kw.setdefault("gates", GATES)
    kw.setdefault("node_gates", NODE_GATES)
    c.assert_standard_contract(template, post_deploy=[POST], **kw)


# --- passes ------------------------------------------------------------------------


def test_contract_passes_for_a_stage_deployed_pipeline():
    _contract(build(), deploy_stage="Prod")


def test_contract_passes_for_a_deploy_step_pipeline():
    _contract(_step_build())


def test_contract_passes_with_the_deploy_open():
    _contract(_step_build(deploy_enabled=True), deploy_enabled=True)


def test_contract_passes_with_pr_checks_off():
    _contract(_step_build(pull_request_checks=False), pull_request_checks=False)


def test_individual_helpers_accept_the_optional_connection_and_branch():
    t = _step_build()
    c.assert_source(t, REPO, "main", connection_arn=CONN)
    c.assert_pr_checks(t, GATES, "demo", "main", connection_arn=CONN)


# --- each violation is caught ----------------------------------------------------------


def test_catches_a_deploy_stage_that_is_open_when_it_should_be_closed():
    with pytest.raises(AssertionError, match="not closed"):
        c.assert_deploy_closed(_step_build(deploy_enabled=True), "Deploy")


def test_catches_a_deploy_stage_that_is_closed_when_it_should_be_open():
    with pytest.raises(AssertionError, match="closed but should be open"):
        c.assert_deploy_open(_step_build())


def test_catches_a_missing_approval():
    with pytest.raises(AssertionError, match="exactly one approval"):
        c.assert_deploy_ordering(_step_build(require_approval=False), "Deploy")


def test_catches_missing_pr_checks():
    with pytest.raises(AssertionError, match="PR check projects differ"):
        c.assert_pr_checks(_step_build(pull_request_checks=False), GATES, "demo")


def test_catches_a_gate_that_is_not_in_the_pipeline():
    from specter_pipeline import Gate

    with pytest.raises(AssertionError, match="gates differ"):
        c.assert_gates_present(_step_build(), [*GATES, Gate("extra", ["true"])])


def test_catches_a_gate_that_needs_node_but_has_none():
    with pytest.raises(AssertionError, match="needs Node"):
        c.assert_gates_have_node(
            GATES, ["unit"]
        )  # 'unit' runs on the plain Python image


def test_catches_an_unknown_gate_name():
    with pytest.raises(AssertionError, match="no gate named"):
        c.assert_gates_have_node(GATES, ["nope"])


def test_catches_the_wrong_source_branch_and_repo():
    t = _step_build()
    with pytest.raises(AssertionError, match="source branch"):
        c.assert_source(t, REPO, "release")
    with pytest.raises(AssertionError, match="source repo"):
        c.assert_source(t, "Specter099/other")


def test_catches_a_wrong_stage_order():
    with pytest.raises(AssertionError, match="unexpected stage order"):
        c.assert_stage_order(_step_build(), "Prod")


def test_catches_the_wrong_artifact_bucket_name():
    with pytest.raises(AssertionError, match="expected one artifact bucket with prefix .not-demo-pipeline., found 0"):
        c.assert_artifact_bucket(_step_build(), "not-demo")


def test_catches_pr_checks_present_when_declared_off():
    with pytest.raises(AssertionError, match="PR-check projects exist"):
        _contract(_step_build(), pull_request_checks=False)


def test_catches_a_contract_called_with_a_malformed_repo():
    with pytest.raises(AssertionError, match="owner/name"):
        _contract(_step_build(), repo="demo")


def test_wildcard_resource_check_passes_on_the_unmodified_pipeline_with_only_cdks_own_wildcards():
    """The negative cases (wildcards in any position, inline policies, NotAction, CDK's allowance
    only on the self-mutation role) are in test_contract_mutations.py."""
    c.assert_no_wildcard_resources(_step_build(), allowed_actions=c.CDK_INTERNAL_WILDCARDS)


def test_step_commands_returns_the_commands_of_the_project_behind_a_step():
    t = _step_build()
    cmds = c.step_commands(t, "Deploy-app")
    assert "cdk deploy AppStack --require-approval never" in cmds
    assert c.step_commands(t, "Gate-synth-check")[-1] == "cdk synth"
    with pytest.raises(AssertionError, match="no CodeBuild project"):
        c.step_commands(t, "Nope")


def test_catches_a_post_deploy_step_the_caller_did_not_declare():
    tpl = build()
    # an undeclared step would skip its runtime and compute checks
    with pytest.raises(AssertionError, match=r"Post-frontend.*not passed as post_deploy="):
        c.assert_standard_contract(tpl, repo=REPO, gates=GATES, node_gates=NODE_GATES)
