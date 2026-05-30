# Local development setup

Practical setup for running Pipewright on one machine. Pipewright is a local,
single-user tool today — there is no Docker, no deployment, and no hosted auth.

## Prerequisites

- Python 3.11+
- Node.js 18+ (for the frontend / Vite)
- Git
- At least one LLM provider API key (Gemini by default)
- Optional: GitHub CLI (`gh`) if you want the `github_cli` PR mode

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
