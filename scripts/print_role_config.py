"""
Print the resolved LLM provider/model for every pipeline role.

Read-only and secret-safe: makes no network calls and never prints API keys.
Resolution follows backend/llm/role_config.py (overrides -> role env ->
default env -> hardcoded fallback).

Usage:
    venv\\Scripts\\python.exe scripts\\print_role_config.py
    venv\\Scripts\\python.exe scripts\\print_role_config.py --validate

--validate additionally checks, per role, that the provider is registered, the
model is supported, and the required API key is present (the same checks the
backend runs via validate_all_roles at startup).

Note: provider API keys are optional, so this runs without GEMINI_API_KEY.
With --validate, a missing key is reported only for a provider a role selects.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.llm import get_provider_for_role
from backend.llm.errors import LLMError
from backend.llm.role_config import Role, resolve_role_config

# Roles that currently have a pipeline stage calling complete_for_role(...).
WIRED_ROLES = {Role.TRIAGE, Role.PLANNER, Role.CODER, Role.SUMMARY}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print resolved LLM provider/model per role (no secrets).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Also validate provider/model/credentials for each role.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exit_code = 0

    print(f"{'role':<12} {'provider':<12} {'model':<28} wired")
    print(f"{'-' * 12} {'-' * 12} {'-' * 28} {'-' * 5}")

    for role in Role:
        config = resolve_role_config(role)
        wired = "yes" if role in WIRED_ROLES else "no"
        print(f"{role.value:<12} {config.provider:<12} {config.model:<28} {wired}")

    if not args.validate:
        return exit_code

    print()
    print("Validation:")
    for role in Role:
        try:
            provider, model = get_provider_for_role(role)
            validate_config = getattr(provider, "validate_config", None)
            if validate_config:
                validate_config(model)
            print(f"  [OK]   {role.value}")
        except LLMError as error:
            # LLMError messages are already sanitized; safe to print.
            print(f"  [FAIL] {role.value}: {error}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
