# Docker for Pipewright — design doc (design-only, not implemented)

Status: **Design only.** No Dockerfiles, no `docker-compose.yml`, and no
runtime changes exist or are introduced by this document. This is the output of
a senior/principal-engineer design review answering one question: *if* and *how*
Pipewright should adopt Docker, given its safety model and current phase
(Phase 2G — demo/readme/devex readiness).

Related docs:
- Primary onboarding path today: [`local-dev.md`](./local-dev.md)
- PR modes and safety: [`../decisions/github-cli-pr-mode.md`](../decisions/github-cli-pr-mode.md)
- LLM config: [`../llm/role-based-configuration.md`](../llm/role-based-configuration.md)

---

## 1. Executive recommendation

- **Docker is design-only for now.** Nothing in this PR adds Docker support.
- **Native dev scripts remain the primary onboarding path.** `scripts/dev.ps1`
  and `scripts/dev.sh` (see [`local-dev.md`](./local-dev.md)) stay the
  recommended way to run Pipewright against a real repo.
- **The first Docker implementation, if/when we do it, should be a
  self-contained local *demo*** — not a live-repo development workflow. It runs
  the full pipeline against a bundled sample repo on a safe in-container path,
  in `local_only` mode, with no GitHub CLI/token flows.

Rationale in one line: Docker's value here is "boots the demo in one command on
a clean machine," not "replaces the native dev loop." The native loop is already
one command and already understands host filesystem paths; Docker mostly *adds*
a path-translation and trust surface we do not want to take on yet.

Risk level of shipping Docker badly: **high** — wrong mount/path design can make
project setup confusing, can expose far more of the host filesystem than the
user expects, and can mislead users about where commits land.

---

## 2. Why Docker is tricky for Pipewright

Pipewright is not a stateless web app. It is a tool that *reads and writes a
real git repository on disk* and stores the absolute path to that repository.

- **Pipewright stores `repo_path` as a raw filesystem path.** A project row
  holds the literal path string the user entered; the pipeline `cd`s into it,
  reads files, applies patches, and runs `git` there.
- **Native host paths do not exist inside a container.** A path like
  `C:\Users\satis\Projects\repo` (or `/Users/satish/Projects/repo` on macOS) is
  meaningless inside a Linux container. The container only sees what is mounted,
  at the mount's *container* path.
- **Container paths look different**, e.g. `/workspace/projects/repo`. So the
  path the user knows on the host and the path Pipewright must store/use inside
  the container are **two different strings**.
- **Wrong path/mount design makes setup confusing or unsafe.** Two concrete
  failure modes:
  - *Confusing:* the user pastes their host path into the UI; inside the
    container that path does not exist, project detection fails, and the error
    ("not a git repo") does not explain why.
  - *Unsafe:* to "make paths just work," someone mounts the entire
    home/`Projects` directory into the container. Now the AI pipeline can read
    and write **every repo and file** under that tree, not just the one the user
    intended — a large, silent expansion of blast radius.

There is no silent host↔container path translation today, and **this PR does not
build one.** Building one is a real design decision (see Open Questions), not a
convenience to bolt on.

### Hard dependency the demo plan must account for

The SQLite database path defaults to `backend/db/pipewright.db`
(`backend/db/database.py`). As of PR #15E it can be overridden with the
`PIPEWRIGHT_DB_PATH` environment variable, which points the engine at a
different SQLite file (the override's parent directory is created if missing).
When the variable is unset, the default path and behavior are unchanged. So
"persist SQLite under `./data`" (the #14B goal) can be done either way:

- mount a host `./data` volume at the container's `backend/db/` directory, so
  the default path lands on the volume (docs/compose-only, no code change), or
- set `PIPEWRIGHT_DB_PATH=/data/pipewright.db` and mount `./data` at `/data`.

Either keeps backend behavior unchanged; the variable simply makes the location
explicit. (There is still no `DATABASE_URL` and no non-SQLite support.)

---

## 3. Options compared

### Option A — Mount one target repo manually

The user mounts exactly one host repo to a fixed container path (e.g.
`-v C:\Users\satis\Projects\myrepo:/workspace/repo`) and registers
`/workspace/repo` as the project.

- **Benefits:** smallest blast radius of any "real repo" option; the user works
  on a genuine repo inside Docker; only the intended repo is exposed.
- **Drawbacks:** the user must understand that the project path is the
  *container* path (`/workspace/repo`), not their host path; one mount per repo;
  re-run/edit of compose to switch repos.
- **Security/trust:** good — single explicit mount, single repo exposed. Still
  requires the user to reason about path translation, which is an error surface.
- **Recommendation:** **viable later**, as an advanced/appendix path once the
  demo exists and the path-translation UX is documented. Not the first step.

### Option B — Mount the whole workspace / `Projects` folder

Mount the user's entire `Projects` (or home) directory so any repo "just works."

- **Benefits:** convenient; any repo path resolves without re-mounting.
- **Drawbacks:** the AI pipeline can read/write *everything* under that tree.
- **Security/trust:** **bad.** This is a major, silent expansion of blast
  radius and directly undercuts Pipewright's local-first trust story. A scope or
  patch bug now risks every repo the user owns, not one.
- **Recommendation:** **reject as a default.** If ever offered, it must be an
  explicit, loudly-warned advanced opt-in — never the documented happy path.

### Option C — Backend-only Docker

Containerize only the backend; run the frontend natively on the host.

- **Benefits:** smaller image; avoids dockerizing Vite.
- **Drawbacks:** split-brain setup (half Docker, half native) is *more* to
  explain, not less; the backend in a container still has the same repo-path and
  mount problems as A/B, plus host↔container networking (CORS, API base URL) to
  configure by hand; doesn't deliver the "one command on a clean machine" demo.
- **Security/trust:** neutral, but no real win.
- **Recommendation:** **reject.** It combines the costs of Docker with the costs
  of native setup and the benefits of neither.

### Option D — Self-contained Docker demo

Backend + frontend in compose, running the full pipeline against a **bundled
sample repo** on a safe in-container path, `local_only`, no GitHub, SQLite
persisted to a project-local `./data` volume.

- **Benefits:** "one command, clean machine, see the aha moment" — exactly the
  Phase 2G goal. No host repo is mounted, so **no path-translation problem and
  no host blast radius.** Reproducible demo for screenshots/GIF/review.
- **Drawbacks:** not for working on the user's real repos; the bundled repo is a
  toy; needs the DB-path handling from section 2.
- **Security/trust:** **strong.** `local_only` means no push/PR; no host mount
  means nothing outside the demo is touched; no keys baked into images.
- **Recommendation:** **the correct first Docker implementation.**

### Option E — No Docker; keep improving native scripts

Invest the same effort into the native scripts, README, and demo doc instead.

- **Benefits:** zero new trust/path surface; the native loop already works on a
  real repo with real host paths and `gh`; lowest risk for the current phase.
- **Drawbacks:** "clone, install Python/Node, run a script" is slightly more
  first-run friction than "docker compose up" for a reviewer who only wants to
  watch the demo.
- **Security/trust:** best — nothing new.
- **Recommendation:** **remains the primary path.**

### Final recommendation

- **E remains the primary path** for real-repo development and onboarding.
- **D is the first Docker implementation** if/when Docker is pursued.
- **A can be advanced later** as a documented, single-repo, container-path
  workflow.
- **Reject B as a default.** Whole-workspace/home mounts are not the happy path.
- **Reject C.** Backend-only Docker adds cost without delivering the demo.

---

## 4. Recommended future PR plan

> These are *proposed future PRs*, not part of this design-only PR.

### PR #14B — Self-contained Docker demo

Scope: a runnable, local-only demo. Likely artifacts:

- `backend/Dockerfile.dev`
- `frontend/Dockerfile.dev`
- `docker-compose.dev.yml`
- `.dockerignore` (must exclude `.env`, `.git`, `venv/`, `node_modules/`,
  `*.db`, `*.sqlite`, `backend/backups/`)
- a bundled **sample repo** under a safe in-container demo path (e.g.
  `/workspace/demo-repo`), git-initialized at image build time
- `pr_mode = local_only` only — no GitHub
- SQLite persisted under a project-local `./data` volume (mounted at the
  container's `backend/db/`, per the section 2 dependency)
- `env_file: .env` for keys at runtime — **never** `COPY .env` into an image
- **no `gh`** installed in the image; no manual-token flow

Constraints #14B must hold:
- No whole-home / whole-`Projects` mount.
- No host repo mounted by default (the demo uses the bundled repo).
- No change to the safety pipeline, approval gates, or scope guard.

### PR #14C — Docker docs / troubleshooting

Scope: documentation only, written after #14B exists. Should cover:

- path issues (host vs container paths; why a host path "doesn't exist")
- `git config --global --add safe.directory ...` for container-owned repos
- SQLite reset (stop compose, delete `./data`, restart)
- Windows / WSL2 caveats (file-watching, bind-mount performance, line endings)
- Vite `--host` so the dev server is reachable from outside the container
- CORS / API base URL between frontend container and backend container
- an **advanced appendix: mount-one-repo** (Option A) with explicit warnings

---

## 5. Required warnings (must appear in #14B/#14C docs)

- **Do not mount your whole home directory.**
- **Do not mount your whole `Projects` folder by default.**
- **Mount only the specific repo you want Pipewright to work on** (advanced
  Option A only).
- **In Docker, project paths are *container* paths, not host paths.** Register
  the container path (e.g. `/workspace/repo`), not `C:\Users\...`.
- **Docker mode is `local_only` initially** — no push, no PR from inside the
  container.
- **GitHub CLI PR creation is native-dev first.** `gh` flows are not supported
  inside Docker initially.
- **Never copy `.env` into images.** Pass configuration via `env_file` / runtime
  environment only.
- **Keys are passed at runtime only** and must never be baked into an image
  layer (image layers are cacheable and shareable — a baked key is a leaked key).

---

## 6. Open questions

- **Is Docker meant for the demo, or for real-repo development?** This doc
  recommends *demo first* (Option D). Real-repo Docker (Option A) needs a
  decision on path-translation UX before it ships.
- **Do we eventually need host↔container path mapping?** If we support real-repo
  Docker, we need either an explicit "this is your container path" UX or a
  documented convention. We are deliberately **not** building silent translation
  now.
- **Should the frontend API base URL be parameterized later?** Container-to-
  container networking differs from `127.0.0.1`. A build/runtime-configurable API
  base URL is likely required for #14B; it is a runtime change to scope there.
- **Should GitHub CLI inside Docker ever be supported?** It means handling `gh`
  auth state (mounted config or device-login) inside a container — a new trust
  surface. Defer until there is a concrete need.
- **How should the DB path be relocated for `./data`?** Volume-mount over
  `backend/db/` (no code change) vs. a configurable DB path (runtime change).
  Decide in #14B; this doc leans volume-mount to keep #14B docs/compose-only.

---

## 7. Manual validation plan for future #14B

When #14B is implemented, it should be validated by hand against this checklist
(no automated coverage is assumed for compose wiring):

1. `docker compose -f docker-compose.dev.yml up` boots both backend and
   frontend without errors.
2. The backend health endpoint responds (e.g. `GET /docs` or the health route)
   from the host.
3. The UI loads in a browser and can reach the backend (no CORS errors).
4. The bundled **demo repo** can be selected as a project (using its *container*
   path).
5. A **`report_only`** request runs end-to-end and mutates nothing.
6. A **`local_only`** implementation run reaches final approval and creates a
   **local commit** inside the container's demo repo.
7. **No GitHub push and no PR** happens at any point (no `gh`, no token; verify
   nothing was pushed).
8. Data **persists under `./data`** across `docker compose down` / `up`
   (project + run state survive a restart).
9. **Deleting `./data`** (with compose stopped) **resets state** to a clean
   first-run.

All nine must pass before #14B is considered demo-ready.

---

## Constraints honored by this document (#14A)

- **Docs only.** This file is the only change.
- **No runtime changes.** No backend or frontend code touched.
- **No Docker files.** No Dockerfile, no `docker-compose*.yml`, no
  `.dockerignore`.
- **No new features.** No deployment, Ollama, Provider Settings UI, BYOK DB
  storage, or execution modes introduced or recommended for now.
