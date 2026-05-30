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
            sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
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

require_command() {
    local name="$1" hint="$2"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "[dev] ERROR: '$name' was not found on PATH." >&2
        echo "      $hint" >&2
        exit 1
    fi
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
step "Checking prerequisites (python3, node, npm)..."
require_command python3 "Install Python 3.11+ from https://www.python.org/downloads/ and re-open the terminal."
require_command node "Install Node.js (LTS) from https://nodejs.org/ and re-open the terminal."
require_command npm "Install Node.js (which bundles npm) and re-open the terminal."

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
