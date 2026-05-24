"""
Reset a local smoke-test repository without deleting branches or resetting hard.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_REPO_PATH = r"C:\Users\Hp\pipewright-smoke-test"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset a smoke-test repo")
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args()


def run_git(repo_path: Path, args: list[str]) -> int:
    print(f"git {' '.join(args)}")
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        print(f"[ERROR] git {' '.join(args)} failed with {result.returncode}")
    return result.returncode


def main() -> int:
    args = parse_args()
    repo_path = Path(args.repo_path).resolve()

    if not repo_path.exists() or not repo_path.is_dir():
        print(f"[ERROR] repo path does not exist or is not a directory: {repo_path}")
        return 1

    if "smoke" not in repo_path.name.lower() and not args.force:
        print(
            "[ERROR] Refusing to reset repo because path name does not contain "
            "'smoke'. Pass --force to override."
        )
        return 1

    print(f"[WARN] This will clean untracked files in: {repo_path}")
    print("[WARN] It will not delete branches and will not run git reset --hard.")
    if not args.yes:
        response = input("Continue? Type YES: ").strip()
        if response != "YES":
            print("Aborted.")
            return 1

    for command in [
        ["checkout", "main"],
        ["restore", "."],
        ["clean", "-fd"],
        ["status"],
    ]:
        code = run_git(repo_path, command)
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
