<#
.SYNOPSIS
    One-command local dev launcher for Pipewright (Windows / PowerShell).

.DESCRIPTION
    Sets up and starts the Pipewright backend and frontend for local
    development:
      - verifies python, node, and npm are available
      - creates the backend virtualenv (venv\) if missing
      - installs backend requirements from backend\requirements.txt
      - installs frontend deps only if frontend\node_modules is missing
      - starts the FastAPI backend and the Vite frontend in two new windows

    This is local developer setup only. It does not change runtime behavior,
    store secrets, run destructive commands, or require admin privileges.

.PARAMETER SkipInstall
    Skip dependency install; just start the backend and frontend. Use this on
    repeat runs once dependencies are already installed.

.PARAMETER Help
    Print this help and exit (no setup, no servers started).

.EXAMPLE
    .\scripts\dev.ps1

.EXAMPLE
    .\scripts\dev.ps1 -SkipInstall
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Get-Help -Detailed $PSCommandPath
    exit 0
}

function Write-Step($message) {
    Write-Host "[dev] $message" -ForegroundColor Cyan
}

function Write-Info($message) {
    Write-Host "      $message" -ForegroundColor Gray
}

function Write-Guidance($lines) {
    foreach ($line in $lines) {
        Write-Host "      $line" -ForegroundColor Yellow
    }
}

# Track whether any required prerequisite is missing so we can report all of
# them together (instead of failing on the first) and then exit once.
$script:MissingPrereq = $false

function Test-RequiredCommand($name, $guidanceLines) {
    if (Get-Command $name -ErrorAction SilentlyContinue) {
        return $true
    }
    Write-Host "[dev] ERROR: '$name' was not found on PATH." -ForegroundColor Red
    Write-Guidance $guidanceLines
    Write-Host ""
    $script:MissingPrereq = $true
    return $false
}

# Resolve the repo root from this script's location (scripts\ -> repo root),
# so the script works no matter where it is invoked from.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$FrontendDir = Join-Path $RepoRoot "frontend"
$VenvDir = Join-Path $RepoRoot "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $RepoRoot "backend\requirements.txt"

Write-Step "Repo root: $RepoRoot"

# --- Prerequisite checks -----------------------------------------------------
Write-Step "Checking prerequisites (python, node, npm, git)..."

Test-RequiredCommand "python" @(
    "Python not found.",
    "Install Python 3.11+:",
    "  winget install Python.Python.3.11",
    "or download from:",
    "  https://www.python.org/downloads/"
) | Out-Null

Test-RequiredCommand "node" @(
    "Node.js not found.",
    "Install Node.js LTS:",
    "  winget install OpenJS.NodeJS.LTS",
    "or download from:",
    "  https://nodejs.org/"
) | Out-Null

# npm ships with Node.js. Prefer npm.cmd on Windows to avoid the PowerShell
# npm.ps1 execution-policy error; fall back to whatever 'npm' resolves to.
$NpmCommand = "npm.cmd"
if (-not (Get-Command $NpmCommand -ErrorAction SilentlyContinue)) {
    if (Get-Command "npm" -ErrorAction SilentlyContinue) {
        $NpmCommand = "npm"
    } else {
        Write-Host "[dev] ERROR: 'npm' was not found on PATH." -ForegroundColor Red
        Write-Guidance @(
            "npm not found.",
            "npm ships with Node.js LTS:",
            "  winget install OpenJS.NodeJS.LTS",
            "or download from:",
            "  https://nodejs.org/"
        )
        Write-Host ""
        $script:MissingPrereq = $true
    }
}

Test-RequiredCommand "git" @(
    "Git not found.",
    "Install Git:",
    "  winget install Git.Git",
    "or download from:",
    "  https://git-scm.com/downloads"
) | Out-Null

if ($script:MissingPrereq) {
    Write-Host "[dev] One or more required tools are missing." -ForegroundColor Red
    Write-Host "      Install them using the suggestions above (this script never installs" -ForegroundColor Red
    Write-Host "      anything for you and does not require admin rights), re-open the" -ForegroundColor Red
    Write-Host "      terminal so PATH refreshes, then re-run this script." -ForegroundColor Red
    exit 1
}

Write-Info "Using npm command: $NpmCommand"

# GitHub CLI is optional: only needed for the 'github_cli' PR mode.
if (-not (Get-Command "gh" -ErrorAction SilentlyContinue)) {
    Write-Info "GitHub CLI (gh) not found - optional, only for the 'github_cli' PR mode:"
    Write-Info "  winget install GitHub.cli"
    Write-Info "  gh auth login"
}

# --- Version checks (informational) -----------------------------------------
$PythonVersion = (& python --version | Out-String).Trim()
if ($PythonVersion) {
    Write-Info "Detected $PythonVersion"
    if ($PythonVersion -match "(\d+)\.(\d+)") {
        $pyMajor = [int]$Matches[1]
        $pyMinor = [int]$Matches[2]
        if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 11)) {
            Write-Host "[dev] WARNING: Python 3.11+ is recommended (found $PythonVersion)." -ForegroundColor Yellow
        }
    }
}

$NodeVersion = (& node --version | Out-String).Trim()
if ($NodeVersion) {
    Write-Info "Detected Node.js $NodeVersion"
}

# --- Backend venv + dependencies --------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Step "Creating backend virtualenv at venv\ ..."
    python -m venv $VenvDir
} else {
    Write-Step "Backend virtualenv already exists."
}

if ($SkipInstall) {
    Write-Step "Skipping dependency install (-SkipInstall)."
} else {
    Write-Step "Installing backend requirements..."
    & $VenvPython -m pip install -r $RequirementsFile

    $NodeModules = Join-Path $FrontendDir "node_modules"
    if (-not (Test-Path $NodeModules)) {
        Write-Step "Installing frontend dependencies (first run)..."
        Push-Location $FrontendDir
        try {
            & $NpmCommand install
        } finally {
            Pop-Location
        }
    } else {
        Write-Step "Frontend dependencies already installed (frontend\node_modules present)."
    }
}

# --- Start servers in separate windows --------------------------------------
Write-Step "Starting backend (FastAPI / uvicorn) in a new window..."
$BackendCommand = "& '$VenvPython' -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001"
Start-Process -FilePath "powershell" `
    -ArgumentList "-NoExit", "-Command", $BackendCommand `
    -WorkingDirectory $RepoRoot | Out-Null

Write-Step "Starting frontend (Vite) in a new window..."
$FrontendCommand = "& '$NpmCommand' run dev"
Start-Process -FilePath "powershell" `
    -ArgumentList "-NoExit", "-Command", $FrontendCommand `
    -WorkingDirectory $FrontendDir | Out-Null

Write-Host ""
Write-Step "Pipewright is starting in two new PowerShell windows."
Write-Host "      Backend:  http://127.0.0.1:8001  (API docs at /docs)" -ForegroundColor Green
Write-Host "      Frontend: http://127.0.0.1:5173  (Vite prints the exact URL)" -ForegroundColor Green
Write-Host ""
Write-Info "Reminder: set GEMINI_API_KEY (or your selected provider key) and"
Write-Info "PIPEWRIGHT_ENCRYPTION_KEY in .env before running a pipeline."
Write-Info "LLM config: docs\llm\role-based-configuration.md"
Write-Host ""
Write-Step "To stop: press Ctrl+C in each window, or close the two windows."
