#!/usr/bin/env bash
# End-to-end setup completion gate (setup job after models + marker).
#
# Requires validate_omni_env_reusable plus a matching .omni-env-complete marker
# from a prior successful setup on this OMNI_CI_HOME.
set -euo pipefail

fast=false
if [[ ${1:-} == --fast ]]; then
  fast=true
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "usage: $0 [--fast] <venv-name>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_NAME="$1"
MARKER="${OMNI_CI_HOME}/.omni-env-complete"

if "$fast"; then
  bash "${SCRIPT_DIR}/validate_omni_ci_home.sh"
  venv="${OMNI_CI_HOME}/${VENV_NAME}"
  site_packages="${venv}/lib/python3.12/site-packages"
  [[ -x "${venv}/bin/python" && -f "${site_packages}/cosyvoice.pth" ]] || exit 1
  grep -Fxq 'include-system-site-packages = true' "${venv}/pyvenv.cfg"
  [[ -n ${GITHUB_SHA:-} && -n ${GITHUB_RUN_ID:-} ]] || exit 1
  host_machine_id=$(cat /run/omni-ci-host-id)
  [[ "$host_machine_id" =~ ^[[:xdigit:]]{32}$ ]] || exit 1
  # Read a single snapshot even if another setup atomically replaces the marker.
  source "${SCRIPT_DIR}/omni_ci_deps_hash.sh"
  DEPS_HASH=$(omni_ci_deps_hash)
  [[ -n "$DEPS_HASH" && $(cat "${OMNI_CI_HOME}/.deps-hash") == "$DEPS_HASH" ]] || exit 1
  export DEPS_HASH
  python3 -IS - "$MARKER" "$site_packages" "$VENV_NAME" "$host_machine_id" <<'PYTHON'
import json
import os
import sys
from pathlib import Path

marker, site_packages, venv_name, machine_id = sys.argv[1:]
fields = {}
for line in Path(marker).read_text().splitlines():
    name, value = line.split("=", 1)
    if name in fields:
        raise SystemExit(f"duplicate marker field: {name}")
    fields[name] = value
site = Path(site_packages)
metadata, = site.glob("sglang_omni-*.dist-info/direct_url.json")
direct_url = json.loads(metadata.read_text())
expected = {
    "setup_run_id": os.environ["GITHUB_RUN_ID"],
    "setup_git_sha": os.environ["GITHUB_SHA"],
    "host_machine_id": machine_id,
    "venv_name": venv_name,
    "deps_hash": os.environ["DEPS_HASH"],
}
if not all(fields.get(k) == v for k, v in expected.items()) or not fields.get("marked_at"):
    raise SystemExit("completion marker does not match the current environment")
if not (direct_url.get("dir_info", {}).get("editable") is True
        and direct_url.get("url") == Path.cwd().as_uri()
        and Path("sglang_omni/__init__.py").is_file()
        and any(site.glob("__editable__.sglang_omni-*.pth"))):
    raise SystemExit("editable installation does not match this checkout")
PYTHON
  echo "Fast environment check passed for ${VENV_NAME}"
  exit 0
else
  bash "${SCRIPT_DIR}/validate_omni_env_reusable.sh" "${VENV_NAME}"
fi

if [ ! -f "${MARKER}" ]; then
  echo "missing completion marker ${MARKER}" >&2
  exit 1
fi

# shellcheck source=omni_ci_deps_hash.sh
source "${SCRIPT_DIR}/omni_ci_deps_hash.sh"
DEPS_HASH="$(omni_ci_deps_hash)"

MARKER_HASH="$(grep -E '^deps_hash=' "${MARKER}" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
MARKER_VENV="$(grep -E '^venv_name=' "${MARKER}" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
MARKER_AT="$(grep -E '^marked_at=' "${MARKER}" | head -1 | cut -d= -f2- | tr -d '[:space:]')"

if [ -z "${MARKER_HASH}" ] || [ "${MARKER_HASH}" != "${DEPS_HASH}" ]; then
  echo "marker deps_hash mismatch or missing: marker=${MARKER_HASH:-<empty>} current=${DEPS_HASH}" >&2
  exit 1
fi
if [ -z "${MARKER_VENV}" ] || [ "${MARKER_VENV}" != "${VENV_NAME}" ]; then
  echo "marker venv_name mismatch: marker=${MARKER_VENV:-<empty>} expected=${VENV_NAME}" >&2
  exit 1
fi
if [ -z "${MARKER_AT}" ]; then
  echo "marker missing marked_at in ${MARKER}" >&2
  exit 1
fi

echo "OMNI CI environment complete: ${OMNI_CI_HOME} (venv=${VENV_NAME}, deps_hash=${DEPS_HASH}, marked_at=${MARKER_AT})"
