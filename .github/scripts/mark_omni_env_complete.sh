#!/usr/bin/env bash
# Record that setup finished successfully for this PR-scoped CI home.
set -euo pipefail

skip_check=false
if [[ ${1:-} == --skip-reusable-check ]]; then
  skip_check=true
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "usage: $0 [--skip-reusable-check] <venv-name>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_NAME="$1"

if "$skip_check"; then
  bash "${SCRIPT_DIR}/validate_omni_ci_home.sh"
else
  bash "${SCRIPT_DIR}/validate_omni_env_reusable.sh" "${VENV_NAME}"
fi

# shellcheck source=omni_ci_deps_hash.sh
source "${SCRIPT_DIR}/omni_ci_deps_hash.sh"
DEPS_HASH="$(omni_ci_deps_hash)"
MARKER="${OMNI_CI_HOME}/.omni-env-complete"
MARKED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SETUP_GIT_SHA="${GITHUB_SHA:-unknown}"

temporary_marker=$(mktemp "${MARKER}.XXXXXX")
trap 'rm -f "${temporary_marker}"' EXIT
cat > "${temporary_marker}" <<EOF
deps_hash=${DEPS_HASH}
venv_name=${VENV_NAME}
marked_at=${MARKED_AT}
setup_git_sha=${SETUP_GIT_SHA}
setup_run_id=${GITHUB_RUN_ID:-unknown}
host_machine_id=$(cat /run/omni-ci-host-id 2>/dev/null || true)
EOF
mv -f "${temporary_marker}" "${MARKER}"

echo "Marked OMNI CI environment complete: ${MARKER}"
