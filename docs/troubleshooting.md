# Troubleshooting

Windows pytest temp `PermissionError`
: Create a local temp folder and point pytest at it:
  `New-Item -ItemType Directory -Force .tmp_pytest_fresh`;
  `$env:TEMP="C:\Users\Hp\pipewright\.tmp_pytest_fresh"`;
  `$env:TMP="C:\Users\Hp\pipewright\.tmp_pytest_fresh"`.

`.git/index.lock`
: A Git process was interrupted or another Git tool is open. Close editors or
  terminals using the target repo. Remove `.git\index.lock` only after
  confirming no Git process is running.

PowerShell quoting issues
: Build JSON with hashtables and `ConvertTo-Json` instead of inline escaped
  JSON strings.

Target repo on old `pipewright/*` branch
: Checkout the configured base branch in the target repo before starting a new
  run. Pipewright will not auto-checkout or delete old branches.

Missing GitHub config fields
: Run `venv\Scripts\python.exe scripts\verify_project_config.py <project_id>`.
  Required fields for push-pr are `github_token`, `github_owner`, and
  `github_repo`.

GitHub token safety
: Scripts print only `has_github_token`; they never print the token. Do not
  paste real tokens into docs, logs, or terminal transcripts.

Dirty repo cleanup
: Run `git status --short` in the target repo. Review changes first. If safe,
  use `git restore .` and `git clean -fd`. Do not use `git reset --hard`.

No pytest installed in smoke repo
: Update the project's stored test command or install test dependencies inside
  the target repo environment. Pipewright uses the test command configured on
  the project row.

Remote URL mismatch
: Run `venv\Scripts\python.exe scripts\verify_project_config.py <project_id>`.
  Ensure `origin` points to the configured `github_owner/github_repo`.

Final approval required before push-pr
: Call `POST /runs/{run_id}/final-approval/approve` before
  `POST /runs/{run_id}/push-pr`. Push-pr is blocked until status is
  `final_approved` or `push_failed`.
