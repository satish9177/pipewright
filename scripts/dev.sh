#!/usr/bin/env bash
#
# One-command local dev launcher for Pipewright (macOS / Linux).
#
# Sets up and starts the Pipewright backend and frontend for local development:
#   - verifies python3, node, and npm are available
#   - creates the backend virtualenv (venv/) if missing
#   - installs backend requirements from backend/requirements.txt
#   - installs frontend deps only if frontend/node_modules is missing
#   - starts the FastAPI backend and the Vite frontend, with Ctrl+C cleanup
#
# This is local developer setup only. It does not change runtime behavior,
# store secrets, run destructive commands, or require root.
#
# Usage:
#   ./scripts/dev.sh              # set up (if needed) and start both servers
#   ./scripts/dev.sh --skip-install
#   ./scripts/dev.sh --help

set -euo pipefail

SKIP_INSTALL=0
for arg in "$@"; do
    case "$arg" in
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --skip-install)
            SKIP_INSTALL=1
            ;;
        *)
            echo "[dev] Unknown argument: $arg (use --help)" >&2
            exit 1
            ;;
    esac
done

step() { printf '[dev] %s\n' "$1"; }
info() { printf '      %s\n' "$1"; }

# Track whether any required prerequisite is missing so we can report all of
# them together (instead of failing on the first) and then exit once.
MISSING_PREREQ=0

require_command() {
    local name="$1"
    if command -v "$name" >/dev/null 2>&1; then
        return 0
    fi
    MISSING_PREREQ=1
    echo "[dev] ERROR: '$name' was not found on PATH." >&2
    case "$name" in
        python3) echo "      Python 3.11+ is required." >&2 ;;
        node)    echo "      Node.js LTS is required." >&2 ;;
        npm)     echo "      npm is required (it ships with Node.js LTS)." >&2 ;;
        git)     echo "      Git is required." >&2 ;;
    esac
    return 0
}

print_install_help() {
    echo "" >&2
    echo "[dev] Install the missing tool(s), re-open your terminal so PATH refreshes," >&2
    echo "      then re-run this script. It never installs anything for you and does" >&2
    echo "      not require root." >&2
    echo "" >&2
    case "$(uname -s)" in
        Darwin)
            echo "      macOS (Homebrew):" >&2
            echo "        brew install python node git" >&2
            ;;
        Linux)
            echo "      Ubuntu/Debian:" >&2
            echo "        sudo apt update" >&2
            echo "        sudo apt install python3 python3-venv python3-pip nodejs npm git" >&2
            ;;
        *)
            echo "      macOS (Homebrew):" >&2
            echo "        brew install python node git" >&2
            echo "      Ubuntu/Debian:" >&2
            echo "        sudo apt update" >&2
            echo "        sudo apt install python3 python3-venv python3-pip nodejs npm git" >&2
            ;;
    esac
    echo "" >&2
    echo "      GitHub CLI is optional (only for the 'github_cli' PR mode):" >&2
    echo "        https://cli.github.com/" >&2
}

# Resolve the repo root from this script's location (scripts/ -> repo root),
# so the script works no matter where it is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="$REPO_ROOT/frontend"
VENV_DIR="$REPO_ROOT/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS_FILE="$REPO_ROOT/backend/requirements.txt"

step "Repo root: $REPO_ROOT"

# --- Prerequisite checks -----------------------------------------------------
step "Checking prerequisites (python3, node, npm, git)..."
require_command python3
require_command node
require_command npm
require_command git

if [ "$MISSING_PREREQ" -ne 0 ]; then
    print_install_help
    exit 1
fi

# GitHub CLI is optional: only needed for the 'github_cli' PR mode.
if ! command -v gh >/dev/null 2>&1; then
    info "GitHub CLI (gh) not found - optional, only for the 'github_cli' PR mode: https://cli.github.com/"
fi

# --- Version checks (informational) -----------------------------------------
PYTHON_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
if [ -n "$PYTHON_VERSION" ]; then
    info "Detected Python $PYTHON_VERSION"
    py_major="$(printf '%s' "$PYTHON_VERSION" | cut -d. -f1)"
    py_minor="$(printf '%s' "$PYTHON_VERSION" | cut -d. -f2)"
    # Guarded inside the if-condition so a non-numeric version never trips set -e.
    if [ "${py_major:-0}" -lt 3 ] 2>/dev/null \
        || { [ "${py_major:-0}" -eq 3 ] && [ "${py_minor:-0}" -lt 11 ]; } 2>/dev/null; then
        info "WARNING: Python 3.11+ is recommended (found $PYTHON_VERSION)."
    fi
fi

NODE_VERSION="$(node --version 2>&1)"
if [ -n "$NODE_VERSION" ]; then
    info "Detected Node.js $NODE_VERSION"
fi

# --- Backend venv + dependencies --------------------------------------------
if [ ! -x "$VENV_PYTHON" ]; then
    step "Creating backend virtualenv at venv/ ..."
    python3 -m venv "$VENV_DIR"
else
    step "Backend virtualenv already exists."
fi

if [ "$SKIP_INSTALL" -eq 1 ]; then
    step "Skipping dependency install (--skip-install)."
else
    step "Installing backend requirements..."
    "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"

    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        step "Installing frontend dependencies (first run)..."
        ( cd "$FRONTEND_DIR" && npm install )
    else
        step "Frontend dependencies already installed (frontend/node_modules present)."
    fi
fi

# --- Start servers with Ctrl+C cleanup --------------------------------------
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    step "Stopping servers..."
    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

step "Starting backend (FastAPI / uvicorn)..."
(
    cd "$REPO_ROOT"
    exec "$VENV_PYTHON" -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
) &
BACKEND_PID=$!

step "Starting frontend (Vite)..."
(
    cd "$FRONTEND_DIR"
    exec npm run dev
) &
FRONTEND_PID=$!

echo ""
step "Pipewright is starting."
info "Backend:  http://127.0.0.1:8001  (API docs at /docs)"
info "Frontend: http://127.0.0.1:5173  (Vite prints the exact URL)"
echo ""
info "Reminder: set GEMINI_API_KEY (or your selected provider key) and"
info "PIPEWRIGHT_ENCRYPTION_KEY in .env before running a pipeline."
info "LLM config: docs/llm/role-based-configuration.md"
echo ""
step "To stop: press Ctrl+C here (both servers are stopped together)."

# Wait for either server to exit; cleanup runs on Ctrl+C or exit.
wait
