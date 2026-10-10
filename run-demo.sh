#!/usr/bin/env bash
# Local demo launcher. Run with ./run-demo.sh or bash run-demo.sh, not sh.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CHECK_ONLY=0
REPAIR=0
while (($#)); do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --repair) REPAIR=1 ;;
        --help|-h)
            cat <<'HELP'
Usage: ./run-demo.sh [--check] [--repair]

Sets up .venv, installs DLN, saves secrets in .demo.env, checks SQLite and ports,
then starts both the public API and private observatory on loopback.
  --check   Bootstrap and check configuration without starting HTTP servers.
  --repair  Reinstall project dependencies even if the cached setup looks valid.

Python 3.11+ and its venv/pip support must already be installed. No sudo or
system-package changes are made. Ctrl+C stops both listeners. Existing secrets
and the SQLite database are reused. .demo.env is a separate local-demo config;
Docker's .env is not loaded. Read the admin password from .demo.env when needed.
HELP
            exit 0 ;;
        *) printf 'Unknown option: %s (use --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

fail() { printf '\n[DLN] ERROR: %s\n' "$*" >&2; exit 1; }
[[ -f pyproject.toml && -f dln/server.py ]] || fail \
    'Project files are missing beside this script. Update/clone the complete repository.'
command -v python3 >/dev/null 2>&1 || fail 'python3 is missing. Install Python 3.11+ first.'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || fail \
    'Python 3.11+ is required. Your python3 is older.'

if [[ ! -x .venv/bin/python ]]; then
    printf '[DLN] Creating project virtual environment...\n'
    python3 -m venv .venv || fail \
        'Could not create .venv. On Debian install python3-venv, then retry. Also check directory permissions.'
fi
PY="$ROOT/.venv/bin/python"
"$PY" -c 'import sys; assert sys.prefix != sys.base_prefix; assert sys.version_info >= (3,11)' || fail \
    'Existing .venv is invalid or too old. Rename it and rerun to create a fresh environment.'
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m ensurepip --upgrade || fail 'pip is missing inside .venv and ensurepip failed.'
fi

ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
CONSTRAINT=""
if [[ "$ARCH" == riscv64 ]]; then
    CONSTRAINT="$ROOT/deployment/riscv64-constraints.txt"
    [[ -f "$CONSTRAINT" ]] || fail 'RISC-V constraints are missing; update the complete repository.'
    printf '[DLN] RISC-V detected: using pinned Pydantic/core and Maturin source-build versions.\n'
fi
FINGERPRINT="$("$PY" - "$ARCH" "$CONSTRAINT" <<'HASH'
import hashlib, pathlib, sys
data = pathlib.Path("pyproject.toml").read_bytes() + sys.argv[1].encode()
if sys.argv[2]:
    data += pathlib.Path(sys.argv[2]).read_bytes()
print(hashlib.sha256(data).hexdigest())
HASH
)"
MARKER="$ROOT/.venv/.dln-demo-install.sha256"
INSTALLED="$(cat "$MARKER" 2>/dev/null || true)"
if [[ "$REPAIR" == 1 || "$INSTALLED" != "$FINGERPRINT" ]] || \
   ! "$PY" -c 'import dln.api, uvicorn; from pydantic import field_validator' >/dev/null 2>&1; then
    printf '[DLN] Installing dependencies using %s...\n' "$PY"
    if [[ -n "$CONSTRAINT" ]]; then
        for tool in cargo rustc cc; do
            command -v "$tool" >/dev/null 2>&1 || fail \
                "RISC-V source builds need $tool. Install build-essential, python3-dev and a Rust toolchain (rustc/cargo 1.75+), then rerun."
        done
        "$PY" - <<'RUST' || fail 'RISC-V source builds require rustc and cargo 1.75+.'
import re, subprocess
for tool in ("rustc", "cargo"):
    version = subprocess.check_output([tool, "--version"], text=True).strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    print("[DLN] " + version)
    if not match or tuple(map(int, match.groups())) < (1, 75, 0):
        raise SystemExit(1)
RUST
        "$PY" -c 'import pathlib,sysconfig; assert (pathlib.Path(sysconfig.get_path("include"))/"Python.h").is_file()' || fail \
            'Python development headers are missing. Install python3-dev matching your Python version.'
        printf '[DLN] First RISC-V installation may compile Rust packages and take several minutes.\n'
        # PIP_CONSTRAINT is inherited by pip's isolated build subprocesses: merely
        # preinstalling old Maturin would NOT stop the isolated build choosing 1.15.
        if ! PIP_CONSTRAINT="$CONSTRAINT" PIP_BUILD_CONSTRAINT="$CONSTRAINT" \
             CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}" \
             "$PY" -m pip install -e .; then
            fail 'RISC-V source installation failed; no servers started. See the first build error above. Pinned builds still need working compiler/header/Rust tools and access to the Cargo registry.'
        fi
    elif ! "$PY" -m pip install -e .; then
        fail 'Dependency installation failed; server startup was cancelled. Fix the first pip error above and rerun. Source builds may need a compiler or Rust toolchain; this script does not install system packages.'
    fi
    printf '%s\n' "$FINGERPRINT" > "$MARKER"
fi
"$PY" -m pip check || fail 'Dependency conflicts found. Try ./run-demo.sh --repair and inspect pip output.'

# Always use the project interpreter; never fall back to system uvicorn/Pydantic.
exec "$PY" - "$ROOT" "$CHECK_ONLY" <<'PYTHON'
import asyncio
import contextlib
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

root = Path(sys.argv[1])
check_only = sys.argv[2] == "1"
config = root / ".demo.env"


def fail(message):
    print(f"\n[DLN] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


print(f"[DLN] Python {platform.python_version()} / {platform.machine()} / {sys.executable}")
try:
    import pydantic
    import uvicorn
    from dln.api import create_apps
    from dln.config import Settings
    from dln.store import Store
    print(f"[DLN] Pydantic {pydantic.__version__}, Uvicorn {uvicorn.__version__}")
except ImportError as error:
    fail(f"Dependency import failed: {error}. Run ./run-demo.sh --repair.")

# Parse data, never source/evaluate the configuration as shell code.
allowed = {"DLN_" + name.upper() for name in Settings.__dataclass_fields__}
if not config.exists():
    values = {
        "DLN_DB": os.environ.get("DLN_DB") or str(root / "data" / "dln.sqlite3"),
        "DLN_IP_SECRET": os.environ.get("DLN_IP_SECRET") or secrets.token_hex(32),
        "DLN_ADMIN_USER": os.environ.get("DLN_ADMIN_USER") or "operator",
        "DLN_ADMIN_PASSWORD": os.environ.get("DLN_ADMIN_PASSWORD") or secrets.token_urlsafe(32),
    }
    # Persist any other inherited DLN settings as well, for reproducible restarts.
    for key in sorted(allowed - values.keys()):
        if key in os.environ:
            values[key] = os.environ[key]
    if any("\n" in v or "\r" in v for v in values.values()):
        fail("DLN configuration must contain single-line values.")
    try:
        # Exclusive creation avoids replacing credentials from another invocation.
        with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write("# Generated by run-demo.sh. Keep private; reused on every launch.\n")
            file.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        print("[DLN] Saved new demo settings in .demo.env (owner-only permissions).")
    except FileExistsError:
        pass
    except OSError as error:
        fail(f"Could not save .demo.env: {error}")
try:
    os.chmod(config, 0o600)
    saved = {}
    for number, line in enumerate(config.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in allowed or key in saved:
            fail(f"Invalid or duplicate setting at .demo.env line {number}.")
        saved[key] = value
    for key in ("DLN_DB", "DLN_IP_SECRET", "DLN_ADMIN_USER", "DLN_ADMIN_PASSWORD"):
        if not saved.get(key):
            fail(f"{key} is missing/empty in .demo.env. Fill it; existing secrets are never regenerated.")
    # Saved configuration takes precedence over stale/random shell exports.
    for key in allowed:
        os.environ.pop(key, None)
    os.environ.update(saved)
    os.environ["DLN_DB"] = str(Path(saved["DLN_DB"]).expanduser().resolve())
    settings = Settings.from_env()
except (OSError, ValueError) as error:
    fail(f"Invalid demo configuration: {error}")

try:
    with contextlib.closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    store = Store(settings)
    with store.connect() as db:
        db.execute("SELECT 1").fetchone()
        store.maintenance(db)
except (OSError, sqlite3.Error) as error:
    fail(f"SQLite/FTS5 or database write check failed: {error}")
print(f"[DLN] SQLite FTS5 and writable database OK: {settings.db}")

for port in (8000, 8001):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        fail(f"Port {port} is unavailable: {error}. Stop the existing listener and rerun.")

print("[DLN] Checks passed. Config: .demo.env; observatory username: " + settings.admin_user)
if check_only:
    print("[DLN] Check-only mode; no servers started.")
    sys.exit(0)


async def run():
    processes = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # HTTP probe bypasses proxy environment variables for local health checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def healthy():
        for port, path, expected in ((8000, "/healthz", 200), (8001, "/", 401)):
            try:
                with opener.open(f"http://127.0.0.1:{port}{path}", timeout=0.5) as response:
                    if response.status != expected:
                        return False
            except urllib.error.HTTPError as error:
                if error.code != expected:
                    return False
            except OSError:
                return False
        return True

    status = 0
    try:
        for name, port in (("public", 8000), ("admin", 8001)):
            processes.append(subprocess.Popen([
                sys.executable, "-m", "uvicorn", f"dln.server:{name}",
                "--host", "127.0.0.1", "--port", str(port),
                "--no-proxy-headers", "--no-access-log",
            ]))
        deadline = time.monotonic() + 30
        while not stop.is_set():
            if any(p.poll() is not None for p in processes):
                print("[DLN] A server exited during startup; see its error above.", file=sys.stderr)
                status = 1
                break
            if await asyncio.to_thread(healthy):
                print("[DLN] Ready: API http://127.0.0.1:8000 | Observatory http://127.0.0.1:8001", flush=True)
                print("[DLN] Admin password is saved in .demo.env. Ctrl+C stops both servers.", flush=True)
                break
            if time.monotonic() >= deadline:
                print("[DLN] Startup checks timed out after 30 seconds.", file=sys.stderr)
                status = 1
                break
            await asyncio.sleep(0.2)
        if not status:
            while not stop.is_set():
                if any(p.poll() is not None for p in processes):
                    print("[DLN] A server stopped unexpectedly; stopping the other listener.", file=sys.stderr)
                    status = 1
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
    except OSError as error:
        print(f"[DLN] Failed to launch a server: {error}", file=sys.stderr)
        status = 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        print("[DLN] Both demo listeners stopped.")
    return status


sys.exit(asyncio.run(run()))
PYTHON
