"""Terraform runtime and static checks: pinned and verified, no AWS access, run on every PR."""

import hashlib
import shutil
import subprocess
import zipfile

import pytest
from test_pipeline import build

from specter_pipeline import DeployStep, Ec2, Gate, Runtime
from specter_pipeline import testing as c
from specter_pipeline.presets import CHECKOV, terraform_gates
from specter_pipeline.runtimes import (
    DEFAULT_TERRAFORM_VERSION,
    TERRAFORM_LINUX_SHA256,
    prelude,
    python_prelude,
    terraform_prelude,
)

REPO = "Specter099/demo"
DEPLOY = DeployStep("app", ["./scripts/deploy.sh"])


def tf_build(gates, **kw):
    return build(application_stage=None, deploy_step=DEPLOY, gates=gates, **kw)


# --- the runtime -------------------------------------------------------------------------------


def test_terraform_is_pinned_and_verified_against_a_committed_hash():
    cmds = terraform_prelude()
    v = DEFAULT_TERRAFORM_VERSION
    sha = TERRAFORM_LINUX_SHA256[v]["amd64"]
    assert cmds[0].endswith(f"https://releases.hashicorp.com/terraform/{v}/terraform_{v}_linux_amd64.zip")
    assert cmds[1] == f'echo "{sha}  /tmp/terraform.zip" | sha256sum -c -'
    assert "export TF_IN_AUTOMATION=1 TF_INPUT=0 CHECKPOINT_DISABLE=1" in cmds
    assert cmds[-1] == "terraform version"


def test_every_committed_hash_is_well_formed():
    for version, by_arch in TERRAFORM_LINUX_SHA256.items():
        assert set(by_arch) == {"amd64", "arm64"}, version
        for sha in by_arch.values():
            assert len(sha) == 64 and int(sha, 16) >= 0


def test_an_unknown_version_is_refused():
    with pytest.raises(ValueError, match="no committed SHA-256 for Terraform '9.9.9'"):
        terraform_prelude("9.9.9")
    with pytest.raises(ValueError, match="no committed SHA-256"):
        tf_build(terraform_gates(), terraform_version="9.9.9")


def test_the_runtime_is_python_plus_terraform():
    assert Runtime.TERRAFORM.image.image_id == Runtime.PYTHON.image.image_id
    assert prelude(Runtime.TERRAFORM) == [*python_prelude(), *terraform_prelude()]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_prelude_installs_an_executable_on_path(tmp_path):
    """Everything after the download and hash check, run for real against a stand-in zip."""
    fake = tmp_path / "terraform.zip"
    with zipfile.ZipFile(fake, "w") as z:
        z.writestr("terraform", "#!/bin/sh\necho fake-terraform \"$@\"\n")
        z.writestr("LICENSE.txt", "x")
    cmds = [x.replace("/tmp/", f"{tmp_path}/") for x in terraform_prelude()[2:]]
    r = subprocess.run(["bash", "-ec", "\n".join(cmds)], check=False, cwd=tmp_path,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "fake-terraform version"
    assert not (tmp_path / "terraform" / "bin" / "LICENSE.txt").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_tampered_download_fails_the_hash_check(tmp_path):
    (tmp_path / "terraform.zip").write_bytes(b"not the release")
    check = terraform_prelude()[1].replace("/tmp/", f"{tmp_path}/")
    r = subprocess.run(["bash", "-ec", check], check=False, capture_output=True, text=True)
    assert r.returncode != 0
    assert hashlib.sha256(b"not the release").hexdigest() != TERRAFORM_LINUX_SHA256[DEFAULT_TERRAFORM_VERSION]["amd64"]


# --- the preset --------------------------------------------------------------------------------


def test_the_default_gates():
    gates = {g.name: g for g in terraform_gates()}
    assert list(gates) == ["tf-fmt", "tf-validate", "tf-checkov"]
    assert gates["tf-fmt"].commands == ["terraform -chdir=. fmt -check -recursive -diff"]
    assert gates["tf-validate"].commands[1:] == [
        "terraform -chdir=. init -backend=false -input=false -lockfile=readonly -no-color",
        "terraform -chdir=. validate -no-color",
    ]
    assert gates["tf-checkov"].install == [f"pip install {CHECKOV}"] and "==" in CHECKOV


def test_no_gate_has_aws_access_and_all_run_on_pull_requests():
    for g in terraform_gates(tflint=("0.60.0", "a" * 64)):
        assert not g.policy_statements, g.name
        assert g.runs_on_pull_requests, g.name
        assert not any(w in " ".join(g.commands) for w in (" plan", " apply", "-backend=true")), g.name


def test_validate_never_reads_state_and_requires_the_lock_file():
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    init = [x for x in validate.commands if " init " in x]
    assert init and all("-backend=false" in x and "-lockfile=readonly" in x for x in init)
    assert validate.commands[0].startswith("test -f ./.terraform.lock.hcl ||")


def test_each_root_is_checked():
    gates = {g.name: g for g in terraform_gates(roots=["infra", "envs/prod"])}
    for name, per_root in (("tf-fmt", 1), ("tf-validate", 3), ("tf-checkov", 1)):
        cmds = gates[name].commands
        assert len(cmds) == 2 * per_root
        assert any("infra" in x for x in cmds) and any("envs/prod" in x for x in cmds)


@pytest.mark.parametrize("root", ["/abs", "../up", "a/../b", "", "-rf", "a b", "a;b", "$(x)"])
def test_roots_are_validated(root):
    with pytest.raises(ValueError, match="terraform root|non-empty"):
        terraform_gates(roots=[root])


def test_roots_must_be_distinct():
    with pytest.raises(ValueError, match="distinct"):
        terraform_gates(roots=["infra", "./infra"])


def test_checkov_is_optional():
    assert "tf-checkov" not in {g.name for g in terraform_gates(checkov=False)}


@pytest.mark.parametrize("pin", [("v0.60.0", "a" * 64), ("0.60.0", "abc"), ("latest", "a" * 64)])
def test_tflint_pin_is_validated(pin):
    with pytest.raises(ValueError, match="tflint"):
        terraform_gates(tflint=pin)


def test_tflint_downloads_the_pinned_release_and_checks_its_hash():
    (lint,) = [g for g in terraform_gates(tflint=("0.60.0", "b" * 64)) if g.name == "tf-lint"]
    assert lint.install[0].endswith("/download/v0.60.0/tflint_linux_amd64.zip")
    assert lint.install[1] == f'echo "{"b" * 64}  /tmp/tflint.zip" | sha256sum -c -'


# --- in a pipeline -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tpl():
    return tf_build(terraform_gates())


def test_the_pipeline_satisfies_the_standard_contract(tpl):
    c.assert_standard_contract(tpl, repo=REPO, gates=terraform_gates())


def test_every_terraform_gate_has_a_pr_check_with_no_aws_access(tpl):
    names = {r["Properties"]["Name"] for r in c.pr_projects(tpl, "demo").values()}
    assert names == {"demo-pr-tf-fmt", "demo-pr-tf-validate", "demo-pr-tf-checkov"}
    c.assert_pr_roles_least_privilege(tpl, "demo")


def test_the_pr_check_installs_the_pinned_terraform(tpl):
    import json

    (proj,) = [r for r in c.pr_projects(tpl, "demo").values() if r["Properties"]["Name"] == "demo-pr-tf-validate"]
    cmds = json.loads(proj["Properties"]["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    sha = TERRAFORM_LINUX_SHA256[DEFAULT_TERRAFORM_VERSION]["amd64"]
    assert f'echo "{sha}  /tmp/terraform.zip" | sha256sum -c -' in cmds
    assert cmds.index("terraform version") < cmds.index(
        "terraform -chdir=. validate -no-color"
    )


def test_a_terraform_gate_can_move_to_ec2():
    slow = Gate("tf-big", ["terraform -chdir=. validate"], Runtime.TERRAFORM, ec2=Ec2("medium", 45))
    gates = [*terraform_gates(), slow]
    t = tf_build(gates)
    c.assert_standard_contract(t, repo=REPO, gates=gates)
    assert "terraform version" in c.step_commands(t, "Gate-tf-big")


def test_terraform_on_ec2_uses_the_pinned_python_too():
    cmds = prelude(Runtime.TERRAFORM, ec2=True)
    assert cmds == [*python_prelude(ec2=True), *terraform_prelude()]
    assert "python3.12 -m venv /tmp/venv" in cmds


def test_a_terraform_gate_on_ec2_passes_the_toolchain_contract():
    slow = Gate("tf-big", ["terraform -chdir=. validate"], Runtime.TERRAFORM, ec2=Ec2("medium", 45))
    t = tf_build([*terraform_gates(), slow])
    c.assert_lambda_compute(t, ["Gate-tf-big", "demo-pr-tf-big"])
