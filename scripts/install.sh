#!/usr/bin/env bash
# Local guided installer for the GrokBot <-> Hermes bridge.
# Linux/macOS only. Auditable: no remote pipe-to-shell, no privilege escalation, no gateway start.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Prepare a local checkout: venv, mode-600 .env.local, and MCP URL files.
Does not download remote scripts, raise privileges, or start the gateway.

Options:
  --dry-run            Print planned steps; write nothing
  --non-interactive    Use defaults / flags; do not prompt
  --endpoint URL       Public MCP URL (default https://mcp.example.com/mcp)
  --root DIR           Target checkout (default: repository root)
  --skip-venv          Skip venv creation and pip install (tests / review)
  -h, --help           Show this help
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_ROOT="${REPO_ROOT}"
DRY_RUN=0
NON_INTERACTIVE=0
SKIP_VENV=0
ENDPOINT="https://mcp.example.com/mcp"

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'install: %s\n' "$*" >&2
  exit 1
}

require_not_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    die "refusing to run as root"
  fi
}

require_os() {
  local name
  name="$(uname -s)"
  case "${name}" in
    Linux|Darwin) ;;
    *) die "unsupported operating system: ${name}" ;;
  esac
}

require_no_sudo_in_env() {
  if [[ -n "${SUDO_USER:-}" ]]; then
    die "refusing to run under sudo"
  fi
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    fi
  done
  return 1
}

validate_endpoint() {
  local endpoint="$1"
  ROOT="${REPO_ROOT}" ENDPOINT="${endpoint}" "${PYTHON}" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["ROOT"])
from scripts.configure_plugin import validate_endpoint

try:
    print(validate_endpoint(os.environ["ENDPOINT"]))
except ValueError as exc:
    raise SystemExit(str(exc))
PY
}

public_base_from_endpoint() {
  local endpoint="$1"
  ROOT="${REPO_ROOT}" ENDPOINT="${endpoint}" "${PYTHON}" - <<'PY'
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.environ["ROOT"])
from scripts.configure_plugin import validate_endpoint

parsed = urlsplit(validate_endpoint(os.environ["ENDPOINT"]))
print(f"{parsed.scheme}://{parsed.netloc}")
PY
}

hostname_from_endpoint() {
  local endpoint="$1"
  ROOT="${REPO_ROOT}" ENDPOINT="${endpoint}" "${PYTHON}" - <<'PY'
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.environ["ROOT"])
from scripts.configure_plugin import validate_endpoint

print(urlsplit(validate_endpoint(os.environ["ENDPOINT"])).hostname or "")
PY
}

write_local_env() {
  local dest="$1"
  local public_base="$2"
  local hostname="$3"
  ROOT="${REPO_ROOT}" DEST="${dest}" PUBLIC_BASE="${public_base}" HOSTNAME="${hostname}" "${PYTHON}" - <<'PY'
import os
import secrets
from pathlib import Path

root = Path(os.environ["ROOT"])
dest = Path(os.environ["DEST"])
public_base = os.environ["PUBLIC_BASE"]
hostname = os.environ["HOSTNAME"]
example = (root / "examples" / "env.example").read_text(encoding="utf-8")
owner = secrets.token_hex(32)
lines = []
for line in example.splitlines():
    if line.startswith("HERMES_BRIDGE_SECRET="):
        lines.append("HERMES_BRIDGE_SECRET=" + owner)
    elif line.startswith("HERMES_BRIDGE_PUBLIC_BASE_URL="):
        lines.append("HERMES_BRIDGE_PUBLIC_BASE_URL=" + public_base)
    elif line.startswith("HERMES_BRIDGE_ALLOWED_HOSTS="):
        hosts = ["localhost", "127.0.0.1"]
        if hostname and hostname not in hosts:
            hosts.append(hostname)
        lines.append("HERMES_BRIDGE_ALLOWED_HOSTS=" + ",".join(hosts))
    else:
        lines.append(line)
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
fd = os.open(dest, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.chmod(dest, 0o600)
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --skip-venv)
      SKIP_VENV=1
      shift
      ;;
    --endpoint)
      [[ $# -ge 2 ]] || die "--endpoint requires a URL"
      ENDPOINT="$2"
      shift 2
      ;;
    --root)
      [[ $# -ge 2 ]] || die "--root requires a directory"
      TARGET_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

require_not_root
require_no_sudo_in_env
require_os

PYTHON="$(find_python)" || die "Python 3.11+ is required"
TARGET_ROOT="$(cd "${TARGET_ROOT}" && pwd)"
ENV_FILE="${TARGET_ROOT}/.env.local"

if [[ "${NON_INTERACTIVE}" -eq 0 && -t 0 ]]; then
  printf 'Public MCP endpoint [%s]: ' "${ENDPOINT}"
  read -r reply || true
  if [[ -n "${reply:-}" ]]; then
    ENDPOINT="${reply}"
  fi
fi

ENDPOINT="$(validate_endpoint "${ENDPOINT}")" || die "invalid endpoint"
PUBLIC_BASE="$(public_base_from_endpoint "${ENDPOINT}")"
HOSTNAME="$(hostname_from_endpoint "${ENDPOINT}")"

log "install: python=${PYTHON}"
log "install: target=${TARGET_ROOT}"
log "install: endpoint=${ENDPOINT}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "dry-run: would create ${TARGET_ROOT}/.venv"
  log "dry-run: would run python -m pip install -e ${REPO_ROOT}"
  if [[ -f "${ENV_FILE}" ]]; then
    log "dry-run: would keep existing local env file and chmod 600"
  else
    log "dry-run: would write local env file with mode 600 (owner code not printed)"
  fi
  log "dry-run: would run scripts/configure_plugin.py for the HTTPS /mcp URL"
  log "dry-run: would not start the gateway or configure a runtime"
  exit 0
fi

mkdir -p "${TARGET_ROOT}"

if [[ "${SKIP_VENV}" -eq 0 ]]; then
  log "install: creating local venv"
  "${PYTHON}" -m venv "${TARGET_ROOT}/.venv"
  "${TARGET_ROOT}/.venv/bin/python" -m pip install -U pip
  "${TARGET_ROOT}/.venv/bin/python" -m pip install -e "${REPO_ROOT}"
else
  log "install: skipping venv"
fi

if [[ -f "${ENV_FILE}" ]]; then
  log "install: keeping existing local env file"
  chmod 600 "${ENV_FILE}"
else
  log "install: writing local env file (owner code not printed)"
  write_local_env "${ENV_FILE}" "${PUBLIC_BASE}" "${HOSTNAME}"
fi

log "install: configuring MCP URL files"
"${PYTHON}" "${SCRIPT_DIR}/configure_plugin.py" "${ENDPOINT}" --root "${TARGET_ROOT}"

log "install: done. Fill remaining local paths in the env file, then run scripts/doctor.py"
log "install: the gateway was not started"
