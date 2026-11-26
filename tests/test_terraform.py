"""Terraform runtime and static checks: pinned and verified, no AWS access, run on every PR."""

import json
import shutil
import subprocess
import zipfile

import pytest
from test_pipeline import POST, build

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


# --- the preset --------------------------------------------------------------------------------


def test_the_default_gates():
    gates = {g.name: g for g in terraform_gates()}
    assert list(gates) == ["tf-fmt", "tf-validate", "tf-checkov"]
    assert gates["tf-fmt"].commands == ["terraform -chdir=. fmt -check -recursive -diff"]
    assert gates["tf-validate"].commands[-2:] == [
        "terraform -chdir=. init -backend=false -input=false -lockfile=readonly -no-color",
        "terraform -chdir=. validate -no-color",
    ]
    ckv = " ".join(gates["tf-checkov"].commands)
    assert "--skip-download" in ckv and "--hard-fail-on 'CKV*'" in ckv
    assert gates["tf-checkov"].install == [f"pip install {CHECKOV}"] and "==" in CHECKOV


def test_no_gate_has_aws_access_and_all_run_on_pull_requests():
    for g in terraform_gates(tflint=("0.60.0", "a" * 64)):
        assert not g.policy_statements, g.name
        assert g.runs_on_pull_requests, g.name
        assert not any(w in " ".join(g.commands) for w in (" plan", " apply", "-backend=true")), g.name


def test_validate_never_reads_state_and_requires_the_lock_file():
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    init = [x for x in validate.commands if x.startswith("terraform ") and " init " in x]
    assert init and all("-backend=false" in x and "-lockfile=readonly" in x for x in init)
    assert validate.commands[0].startswith("test -f ./.terraform.lock.hcl ||")


def test_each_root_is_checked():
    gates = {g.name: g for g in terraform_gates(roots=["infra", "envs/prod"])}
    for name, per_root in (("tf-fmt", 1), ("tf-validate", 5), ("tf-checkov", 4)):
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
    # no --init: plugins named by the repo's .tflint.hcl would be fetched unpinned
    assert not any("--init" in x for x in lint.commands)
    assert lint.install[1] == f'echo "{"b" * 64}  /tmp/tflint.zip" | sha256sum -c -'


# --- in a pipeline -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tpl():
    return tf_build(terraform_gates())


def test_the_pipeline_satisfies_the_standard_contract(tpl):
    c.assert_standard_contract(tpl, post_deploy=[POST], repo=REPO, gates=terraform_gates())


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
    c.assert_standard_contract(t, post_deploy=[POST], repo=REPO, gates=gates)
    assert "terraform version" in c.step_commands(t, "Gate-tf-big")


def test_terraform_on_ec2_uses_the_pinned_python_too():
    cmds = prelude(Runtime.TERRAFORM, ec2=True)
    assert cmds == [*python_prelude(ec2=True), *terraform_prelude()]
    assert "python3.12 -m venv /tmp/venv" in cmds


def test_a_terraform_gate_on_ec2_passes_the_toolchain_contract():
    slow = Gate("tf-big", ["terraform -chdir=. validate"], Runtime.TERRAFORM, ec2=Ec2("medium", 45))
    t = tf_build([*terraform_gates(), slow])
    c.assert_lambda_compute(t, ["Gate-tf-big", "demo-pr-tf-big"])



# --- hardening from the PR #5 review ----------------------------------------------------------


def test_roots_as_a_bare_string_is_refused():
    with pytest.raises(TypeError, match="list of paths"):
        terraform_gates(roots="infra")


def test_a_terraform_gate_cannot_read_the_cdk_assembly():
    with pytest.raises(ValueError, match="cannot read the CDK assembly"):
        Gate("tfscan", ["terraform version"], Runtime.TERRAFORM, uses_assembly=True)


def _run(cmds, cwd, path, home=None):
    env = {"PATH": f"{path}:/usr/bin:/bin", "HOME": str(home or cwd)}
    return subprocess.run(["bash", "-ec", "\n".join(cmds)], check=False, cwd=cwd,
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "lock,ok",
    [
        ('provider "registry.terraform.io/hashicorp/aws" {\n  version = "5.0.0"\n  hashes = [\n    "h1:abc=",\n  ]\n}\n', True),
        ('provider "registry.terraform.io/hashicorp/aws" {\n  version = "5.0.0"\n  constraints = "~> 5.0"\n}\n', False),
        ('provider "a/b/c" {\n  hashes = ["zh:x"]\n}\nprovider "a/b/d" {\n  version = "1"\n}\n', False),
        ("", True),  # a root with no providers
    ],
)
def test_every_lock_entry_must_carry_hashes(tmp_path, lock, ok):
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    (tmp_path / ".terraform.lock.hcl").write_text(lock)
    r = _run(validate.commands[:3], tmp_path, "")
    assert (r.returncode == 0) is ok, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("plugins", ["terraform.d/plugins/x", ".terraform/providers/x"])
def test_committed_provider_binaries_are_refused(tmp_path, plugins):
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    (tmp_path / ".terraform.lock.hcl").write_text("")
    (tmp_path / plugins).mkdir(parents=True)
    r = _run(validate.commands[:3], tmp_path, "")
    assert r.returncode != 0 and "provider binaries" in r.stdout


def _fake_checkov(tmp_path, out, rc):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "checkov").write_text(f"#!/bin/sh\necho '{out}'\nexit {rc}\n")
    (bin_ / "checkov").chmod(0o755)
    return bin_


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "setup,out,rc,ok,says",
    [
        ({"infra/main.tf": "x"}, "Passed checks: 1, Failed checks: 0", 0, True, ""),
        ({"infra/main.tf": "x"}, "Passed checks: 1, Failed checks: 3", 1, False, ""),
        ({}, "", 0, False, "no such directory"),
        ({"infra/x.tf": "x"}, "Passed checks: 0, Failed checks: 0, Skipped checks: 0, Parsing errors: 1", 0, False, "could not parse"),
        ({"infra/main.tf": "x", "infra/.checkov.yaml": "soft-fail: true"}, "", 0, False, "config file"),
        ({"infra/main.tf": "x", ".checkov.yml": "skip-path: [.]"}, "", 0, False, "config file"),
    ],
)
def test_checkov_gate_cannot_be_switched_off_or_pass_vacuously(tmp_path, setup, out, rc, ok, says):
    """The gate's own logic around Checkov, run against a stand-in checkov."""
    (ckv,) = [g for g in terraform_gates(roots=["infra"]) if g.name == "tf-checkov"]
    work = tmp_path / "w"
    work.mkdir()
    for rel, body in setup.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(body)
    cmds = [x.replace("/tmp/checkov.out", f"{tmp_path}/checkov.out") for x in ckv.commands]
    r = _run(cmds, work, _fake_checkov(tmp_path, out, rc), home=tmp_path / "home")
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert says in r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_checkov_config_in_home_is_refused_too(tmp_path):
    (ckv,) = [g for g in terraform_gates(roots=["infra"]) if g.name == "tf-checkov"]
    (tmp_path / "w" / "infra").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".checkov.yaml").write_text("soft-fail: true")
    r = _run(ckv.commands, tmp_path / "w", _fake_checkov(tmp_path, "", 0), home=tmp_path / "home")
    assert r.returncode != 0 and "config file" in r.stdout


def test_post_deploy_steps_get_the_pipeline_terraform_version(monkeypatch):
    from specter_pipeline import PostDeployStep

    other = {"amd64": "9" * 64, "arm64": "8" * 64}
    monkeypatch.setitem(TERRAFORM_LINUX_SHA256, "9.9.9", other)
    post = PostDeployStep("tf-out", ["terraform version"], Runtime.TERRAFORM)
    t = tf_build(terraform_gates(), post_deploy=[post], terraform_version="9.9.9")
    cmds = c.step_commands(t, "Post-tf-out")
    assert any("/terraform/9.9.9/terraform_9.9.9_linux_amd64.zip" in x for x in cmds)
    assert f'echo "{"9" * 64}  /tmp/terraform.zip" | sha256sum -c -' in cmds


def test_a_lambda_terraform_gate_with_its_download_swapped_fails_the_contract():
    import copy

    from aws_cdk.assertions import Template

    doc = copy.deepcopy(tf_build(terraform_gates()).to_json())
    for r in doc["Resources"].values():
        if r["Type"] == "AWS::CodeBuild::Project" and r["Properties"].get("Name") == "demo-pr-tf-validate":
            spec = json.loads(r["Properties"]["Source"]["BuildSpec"])
            spec["phases"]["build"]["commands"] = [
                x for x in spec["phases"]["build"]["commands"] if "terraform.zip" not in x
            ]
            r["Properties"]["Source"]["BuildSpec"] = json.dumps(spec)
    with pytest.raises(AssertionError, match="pinned TERRAFORM toolchain"):
        c.assert_standard_contract(Template.from_json(doc), post_deploy=[POST], repo=REPO, gates=terraform_gates())



# --- second round: strict lock grammar, .tf.json, quote-aware deploy check ---------------------

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "lockfile"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "lock,ok",
    [
        ((FIXTURES / "terraform-1.17.2.lock.hcl").read_text(), True),  # written by terraform init
        ((FIXTURES / "terraform-1.17.2.lock.hcl").read_text().replace("\n", "\r\n"), True),
        ('provider "registry.terraform.io/a/b" {\n  version = "1"\n  # hashes = ["h1:x"]\n}\n', False),
        ('  provider "registry.terraform.io/a/b" {\n  hashes = [\n    "h1:x",\n  ]\n}\n', False),
        ('provider"registry.terraform.io/a/b" {\n}\n', False),
        ('﻿provider "registry.terraform.io/a/b" {\n  version = "1"\n}\n', False),
        ('/* x */ provider "registry.terraform.io/a/b" {\n  version = "1"\n}\n', False),
        ('provider "registry.terraform.io/a/b" {\n  version = "1"\n  hashes = [\n  ]\n}\n', False),
        ('provider "registry.terraform.io/a/b" {\n  hashes = [\n    "h1:x",\n', False),
    ],
    ids=["terraform-wrote-it", "crlf", "comment-hiding-hashes", "indented", "no-space", "bom",
         "block-comment", "empty-hashes", "unterminated"],
)
def test_lock_file_must_be_terraforms_own_layout_with_hashes(tmp_path, lock, ok):
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    (tmp_path / ".terraform.lock.hcl").write_bytes(lock.encode("utf-8"))
    r = _run(validate.commands[:2], tmp_path, "")
    assert (r.returncode == 0) is ok, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_tf_json_configuration_is_refused_because_checkov_skips_it(tmp_path):
    (ckv,) = [g for g in terraform_gates(roots=["infra"]) if g.name == "tf-checkov"]
    (tmp_path / "w" / "infra").mkdir(parents=True)
    (tmp_path / "w" / "infra" / "main.tf.json").write_text('{"resource": {}}')
    (tmp_path / "home").mkdir()
    r = _run(ckv.commands, tmp_path / "w", _fake_checkov(tmp_path, "Passed checks: 1", 0), home=tmp_path / "home")
    assert r.returncode != 0 and ".tf.json" in r.stdout


@pytest.mark.parametrize("arg", ["note=see `README`", "k=$(x)", "a\nb", "it's", "x;y", "#z"])
def test_quoted_deploy_args_are_valid(arg):
    from specter_pipeline.gate import deploy_command, split_deploy_command

    tokens = split_deploy_command(deploy_command(["A"], ["--context", arg]))
    assert tokens is not None and tokens[-3] == arg


@pytest.mark.parametrize("tail", [" --app$IFS/tmp/evil", " --a${u}pp /tmp/evil", " # rest", " && x",
                                  " *", " ~/x", "\nx", " `x`", " 'unterminated", ' "$x"'])
def test_unquoted_shell_syntax_in_the_deploy_command_is_refused(tail):
    from specter_pipeline.gate import deploy_command, split_deploy_command

    assert split_deploy_command(deploy_command(["A"], []) + tail) is None


# --- third round: repo-wide .tf.json, registry ports, non-default versions everywhere ----------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("where", ["modules/net/main.tf.json", "infra/.terraform/m/x.tf.json",
                                   "infra/sub/.terraform/x.tf.json", "linked"])
def test_tf_json_anywhere_in_the_repo_is_refused(tmp_path, where):
    (ckv,) = [g for g in terraform_gates(roots=["infra"]) if g.name == "tf-checkov"]
    w = tmp_path / "w"
    (w / "infra").mkdir(parents=True)
    if where == "linked":
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "main.tf.json").write_text("{}")
        (w / "infra" / "mod").symlink_to(tmp_path / "outside")
    else:
        (w / where).parent.mkdir(parents=True, exist_ok=True)
        (w / where).write_text("{}")
    (tmp_path / "home").mkdir()
    r = _run(ckv.commands, w, _fake_checkov(tmp_path, "Passed checks: 1", 0), home=tmp_path / "home")
    assert r.returncode != 0 and ".tf.json" in r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_lock_entry_for_a_registry_with_a_port_is_accepted(tmp_path):
    (validate,) = [g for g in terraform_gates() if g.name == "tf-validate"]
    lock = 'provider "tf.example.com:8443/a/b" {\n  version = "1"\n  hashes = [\n    "h1:x",\n  ]\n}\n'
    (tmp_path / ".terraform.lock.hcl").write_text(lock)
    r = _run(validate.commands[:2], tmp_path, "")
    assert r.returncode == 0, r.stdout + r.stderr


def test_gates_and_pr_checks_get_a_non_default_terraform_version(monkeypatch):
    monkeypatch.setitem(TERRAFORM_LINUX_SHA256, "9.9.9", {"amd64": "9" * 64, "arm64": "8" * 64})
    t = tf_build(terraform_gates(), terraform_version="9.9.9")
    want = f'echo "{"9" * 64}  /tmp/terraform.zip" | sha256sum -c -'
    for gate in ("tf-fmt", "tf-validate"):  # tf-checkov is Python only: it installs no Terraform
        assert want in c.step_commands(t, f"Gate-{gate}"), gate
    prs = {r["Properties"]["Name"]: r for r in c.pr_projects(t, "demo").values()}
    for name in ("demo-pr-tf-fmt", "demo-pr-tf-validate"):
        spec = json.loads(prs[name]["Properties"]["Source"]["BuildSpec"])
        cmds = spec["phases"]["build"]["commands"]
        assert want in cmds and not any(DEFAULT_TERRAFORM_VERSION in x for x in cmds), name


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_tf_json_check_fails_closed_without_find(tmp_path):
    (ckv,) = [g for g in terraform_gates(roots=["infra"]) if g.name == "tf-checkov"]
    (tmp_path / "w" / "infra").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    bin_ = _fake_checkov(tmp_path, "Passed checks: 1", 0)
    # a PATH holding only the fake checkov and the few tools the gate needs, but no find
    nofind = tmp_path / "nofind"
    nofind.mkdir()
    for tool in ("cat", "grep", "head", "test"):
        if shutil.which(tool):
            (nofind / tool).symlink_to(shutil.which(tool))
    env = {"PATH": f"{bin_}:{nofind}", "HOME": str(tmp_path / "home")}
    r = subprocess.run([shutil.which("bash"), "-ec", "\n".join(ckv.commands)], check=False,
                       cwd=tmp_path / "w", capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "find is not installed" in r.stdout
