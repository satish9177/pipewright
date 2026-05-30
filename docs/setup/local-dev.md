# Local development setup

Practical setup for running Pipewright on one machine. Pipewright is a local,
single-user tool today — there is no Docker, no deployment, and no hosted auth.

## Prerequisites

- Python 3.11+
- Node.js 18+ and npm (for the frontend / Vite)
- Git
- At least one LLM provider API key (Gemini by default)
- Optional: GitHub CLI (`gh`) — only needed for the `github_cli` PR mode

The dev scripts ([below](#one-command-local-dev)) check for these and print
install guidance if anything is missing. **They never auto-install system tools,
never modify your PATH or shell profile, and never require admin/root.** Install
the tools yourself with the commands below, then re-open your terminal so PATH
refreshes.

**Windows:**

```powershell
winget install Python.Python.3.11
winget install OpenJS.NodeJS.LTS
winget install Git.Git
# Optional (github_cli PR mode):
winget install GitHub.cli
gh auth login
```

Or download installers: [Python](https://www.python.org/downloads/),
[Node.js](https://nodejs.org/), [Git](https://git-scm.com/downloads).

**macOS (Homebrew):**

```bash
brew install python node git
# Optional (github_cli PR mode): https://cli.github.com/
```

**Ubuntu/Debian:**

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip nodejs npm git
# Optional (github_cli PR mode): https://cli.github.com/
```

## One-command local dev

The fastest way to start both servers. The script verifies prerequisites,
creates the backend venv if missing, installs backend requirements, installs
frontend deps only on first run, and starts the backend and frontend.

**Windows (PowerShell):**

```powershell
.\scripts\dev.ps1
```

The backend and frontend each open in their own PowerShell window. Stop them with
`Ctrl+C` in each window (or just close the windows). On repeat runs, once
dependencies are installed, you can skip the install step:

```powershell
.\scripts\dev.ps1 -SkipInstall
```

**macOS / Linux:**

```bash
chmod +x scripts/dev.sh   # first time only
./scripts/dev.sh
```

Both servers run in the same terminal; press `Ctrl+C` once to stop both. Use
`./scripts/dev.sh --skip-install` to skip dependency install on repeat runs.

The script prints the URLs:

- Backend: `http://127.0.0.1:8001` (API docs at `/docs`)
- Frontend: `http://127.0.0.1:5173` (Vite prints the exact URL)

> **The script does not create or edit `.env`** (it never stores secrets). Before
> your first *pipeline run*, still do [step 2](#2-configure-environment-variables):
> set `PIPEWRIGHT_ENCRYPTION_KEY` and your selected provider key (e.g.
> `GEMINI_API_KEY`). LLM configuration is always via `.env` — see
> [`role-based-configuration.md`](../llm/role-based-configuration.md).

> **Windows `npm.ps1` issue:** `dev.ps1` already prefers `npm.cmd`, so the
> PowerShell execution-policy error does not apply when using the script. If you
> run npm by hand instead, use `npm.cmd` (see step 4).

If a script fails (or you prefer to run each step yourself), use the manual
fallback below.

## 1. Clone and create a virtual environment

**Windows (PowerShell):**

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r backend\requirements.txt
```

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
```

> The Python dependencies live in `backend/requirements.txt` (not the repo root).

## 2. Configure environment variables

Copy the template and edit `.env`:

```powershell
Copy-Item .env.example .env
```

`.env` is gitignored — never commit real keys.

### Encryption key (required)

`PIPEWRIGHT_ENCRYPTION_KEY` encrypts any stored GitHub token at rest. Generate
one:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the value into `.env`.

### LLM keys (only what you use)

Provider API keys are optional unless a role actually selects that provider. With
no LLM env vars set, every role falls back to `gemini` / `gemini-2.5-flash-lite`,
so the minimal config is a valid `GEMINI_API_KEY`. See
[`role-based-configuration.md`](../llm/role-based-configuration.md) for per-role
overrides and the full key matrix.

Inspect and validate your resolved config (no secrets printed, no network calls):

```powershell
python scripts\print_role_config.py
python scripts\print_role_config.py --validate
```

## 3. Run the backend

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

- `--reload` is for development only. **Do not** use it during an active pipeline
  run — reload can interrupt background execution.
- The API serves on `http://127.0.0.1:8001`.

## 4. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

**Windows / PowerShell execution policy:** if `npm install` fails with a message
about `npm.ps1` being blocked, use the `.cmd` shims (they bypass the PowerShell
script policy):

```powershell
npm.cmd install
npm.cmd run dev
```

Vite prints the dev URL (typically `http://localhost:5173`). Open it in a browser.

## 5. Verify

- Backend: open `http://127.0.0.1:8001/docs` for the FastAPI docs.
- Frontend: the Pipewright UI loads and can reach the backend.
- Config: `python scripts\print_role_config.py --validate` reports `[OK]` for
  every role you intend to use.

Next: follow [`../demo/local-self-use-demo.md`](../demo/local-self-use-demo.md)
for an end-to-end demo.

## Running tests

```powershell
python -m pytest backend\tests -q -m unit
```

Frontend build check:

```bash
cd frontend
npm run build
```

See [`../troubleshooting.md`](../troubleshooting.md) for operational issues
(Windows pytest temp errors, `.git/index.lock`, dirty repo cleanup, etc.).
