# Shared by prepare and reconcile to serialize changes to the host venv.
LOCK_DIR="${UV_CACHE_DIR:-/data/omni-ci/uv-cache}"
mkdir -p "${LOCK_DIR}"
LOCK_FILE="${LOCK_DIR}/omni-venv-prepare-$(echo -n "${OMNI_CI_HOME}" | sha256sum | awk '{print $1}').lock"

exec 200>"${LOCK_FILE}"
if ! flock -w 3600 200; then
  echo "Timed out waiting for venv prepare lock: ${LOCK_FILE}" >&2
  exit 1
fi
