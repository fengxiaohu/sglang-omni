import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / ".github/scripts"


@pytest.mark.parametrize(
    "drift",
    [
        "none",
        "deps_hash",
        "venv_name",
        "setup_git_sha",
        "setup_run_id",
        "host_machine_id",
        "cosyvoice.pth",
        ".omni-env-complete",
        "direct_url.json",
        "missing_run_id",
        ".deps-hash",
    ],
)
def test_fast_gate_requires_current_setup(tmp_path: Path, drift: str) -> None:
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, scripts)
    # The fixture substitutes only the host mount and its path allowlist.
    (scripts / "validate_omni_ci_home.sh").write_text(
        '[[ "$OMNI_CI_HOME" == "$PWD/home" ]]'
    )
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("a" * 32)
    for script_name in ("mark_omni_env_complete.sh", "validate_omni_env_complete.sh"):
        script = scripts / script_name
        script.write_text(
            script.read_text().replace("/run/omni-ci-host-id", str(machine_id))
        )
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "sglang_omni").mkdir()
    (tmp_path / "sglang_omni/__init__.py").touch()
    home = tmp_path / "home"
    venv = home / "omni"
    site = venv / "lib/python3.12/site-packages"
    metadata = site / "sglang_omni-1.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": True}, "url": tmp_path.resolve().as_uri()})
    )
    (site / "__editable__.sglang_omni-1.pth").touch()
    (site / "cosyvoice.pth").touch()
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
    (venv / "bin").mkdir()
    (venv / "bin/python").symlink_to(shutil.which("false"))
    env = dict(
        os.environ, OMNI_CI_HOME=str(home), GITHUB_RUN_ID="123", GITHUB_SHA="abc"
    )
    subprocess.run(
        [
            "bash",
            scripts / "mark_omni_env_complete.sh",
            "--skip-reusable-check",
            "omni",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    marker = home / ".omni-env-complete"
    fields = dict(line.split("=", 1) for line in marker.read_text().splitlines())
    (home / ".deps-hash").write_text(fields["deps_hash"] + "\n")
    if drift in fields:
        fields[drift] += "stale"
        marker.write_text("".join(f"{key}={value}\n" for key, value in fields.items()))
    elif drift == "missing_run_id":
        env.pop("GITHUB_RUN_ID")
    elif drift != "none":
        next(home.rglob(drift)).unlink()
    else:
        pass
    result = subprocess.run(
        ["bash", scripts / "validate_omni_env_complete.sh", "--fast", "omni"],
        cwd=tmp_path,
        env=env,
    )
    assert (result.returncode == 0) == (drift == "none")
