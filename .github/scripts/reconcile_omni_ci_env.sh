#!/usr/bin/env bash
# Align OMNI_CI_HOME with the checked-out tree: refresh if reusable, else rebuild.
#
# Always exits 0 only when validate_omni_env_reusable passes.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/pin_to_ci_cpuset.sh"

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <venv-name>" >&2
  exit 1
fi

if [ -z "${OMNI_CI_HOME:-}" ]; then
  echo "OMNI_CI_HOME is not set" >&2
  exit 1
fi

VENV_NAME="$1"
HOST="${OMNI_CI_HOME}/${VENV_NAME}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPS_HASH_FILE="${OMNI_CI_HOME}/.deps-hash"

# shellcheck source=omni_ci_deps_hash.sh
source "${SCRIPT_DIR}/omni_ci_deps_hash.sh"
DEPS_HASH="$(omni_ci_deps_hash)"

bash "${SCRIPT_DIR}/validate_omni_ci_home.sh"
source "${SCRIPT_DIR}/lock_omni_ci_venv.sh"

_refresh_editable_install() {
  rm -rf "./${VENV_NAME}" || return 1
  ln -sfn "${HOST}" "./${VENV_NAME}" || return 1
  uv pip install --python "${HOST}/bin/python" --upgrade --no-deps -e .
}

if [[ -x "${HOST}/bin/python" && -f "${DEPS_HASH_FILE}" ]] &&
   grep -Fxq 'include-system-site-packages = true' "${HOST}/pyvenv.cfg" &&
   [[ $(cat "${DEPS_HASH_FILE}") == "${DEPS_HASH}" ]]; then
  echo "Refreshing editable install under ${HOST}"
  if _refresh_editable_install && bash "${SCRIPT_DIR}/validate_omni_env_reusable.sh" "${VENV_NAME}"; then
    echo "Environment reuse succeeded for ${OMNI_CI_HOME}"
    exit 0
  fi
fi

bash "${SCRIPT_DIR}/prepare_omni_venv.sh" --lock-held "${VENV_NAME}"
