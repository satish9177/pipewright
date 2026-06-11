"""
test_test_command_detection.py
Unit tests for the deterministic test-command detector (Phase 0, G4).

Detect-and-prefill only: the detector reads marker files and returns a
suggested command string or None. It never executes anything, so every test
here is pure filesystem setup + one function call. Precision over recall: a
file that merely *mentions* a runner (comment, script text, broken JSON) must
yield None, never a guess.
"""

import pytest

from backend.pipeline.test_command_detection import detect_test_command

pytestmark = pytest.mark.unit


def _write(tmp_repo, name: str, content: str) -> None:
    (tmp_repo / name).write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# One test per detector signal
# --------------------------------------------------------------------------

def test_pytest_ini_detects_pytest(tmp_repo):
    _write(tmp_repo, "pytest.ini", "[pytest]\ntestpaths = tests\n")
    assert detect_test_command(str(tmp_repo)) == "pytest"


def test_pyproject_tool_pytest_section_detects_pytest(tmp_repo):
    _write(
        tmp_repo,
        "pyproject.toml",
        "[project]\nname = \"demo\"\n\n[tool.pytest.ini_options]\nminversion = \"7.0\"\n",
    )
    assert detect_test_command(str(tmp_repo)) == "pytest"


def test_setup_cfg_tool_pytest_section_detects_pytest(tmp_repo):
    _write(tmp_repo, "setup.cfg", "[metadata]\nname = demo\n\n[tool:pytest]\n")
    assert detect_test_command(str(tmp_repo)) == "pytest"


def test_package_json_jest_dependency_detects_npm_test(tmp_repo):
    _write(
        tmp_repo,
        "package.json",
        '{"name": "demo", "devDependencies": {"jest": "^29.0.0"}}',
    )
    assert detect_test_command(str(tmp_repo)) == "npm test"


def test_package_json_vitest_dependency_detects_vitest_run(tmp_repo):
    _write(
        tmp_repo,
        "package.json",
        '{"name": "demo", "dependencies": {"vitest": "^1.0.0"}}',
    )
    assert detect_test_command(str(tmp_repo)) == "npx vitest run"


def test_makefile_test_target_detects_make_test(tmp_repo):
    _write(tmp_repo, "Makefile", "build:\n\tgcc main.c\n\ntest: build\n\t./run_tests\n")
    assert detect_test_command(str(tmp_repo)) == "make test"


def test_manage_py_with_django_dependency_detects_django_test(tmp_repo):
    _write(tmp_repo, "manage.py", "#!/usr/bin/env python\n")
    _write(tmp_repo, "requirements.txt", "Django==4.2\n")
    assert detect_test_command(str(tmp_repo)) == "python manage.py test"


# --------------------------------------------------------------------------
# No signal → None. Never guess.
# --------------------------------------------------------------------------

def test_empty_repo_returns_none(tmp_repo):
    assert detect_test_command(str(tmp_repo)) is None


def test_missing_repo_path_returns_none(tmp_repo):
    assert detect_test_command(str(tmp_repo / "does-not-exist")) is None


def test_unrelated_files_return_none(tmp_repo):
    _write(tmp_repo, "README.md", "run pytest to test\n")
    _write(tmp_repo, "main.py", "print('hi')\n")
    assert detect_test_command(str(tmp_repo)) is None


# --------------------------------------------------------------------------
# Priority order with multiple signals present
# --------------------------------------------------------------------------

def test_pytest_ini_wins_over_jest_and_makefile(tmp_repo):
    _write(tmp_repo, "pytest.ini", "[pytest]\n")
    _write(
        tmp_repo,
        "package.json",
        '{"devDependencies": {"jest": "^29.0.0"}}',
    )
    _write(tmp_repo, "Makefile", "test:\n\t./run_tests\n")
    assert detect_test_command(str(tmp_repo)) == "pytest"


def test_jest_wins_over_vitest_in_same_package_json(tmp_repo):
    _write(
        tmp_repo,
        "package.json",
        '{"devDependencies": {"jest": "^29.0.0", "vitest": "^1.0.0"}}',
    )
    assert detect_test_command(str(tmp_repo)) == "npm test"


def test_jest_wins_over_makefile_and_django(tmp_repo):
    _write(
        tmp_repo,
        "package.json",
        '{"devDependencies": {"jest": "^29.0.0"}}',
    )
    _write(tmp_repo, "Makefile", "test:\n\t./run_tests\n")
    _write(tmp_repo, "manage.py", "#!/usr/bin/env python\n")
    _write(tmp_repo, "requirements.txt", "Django==4.2\n")
    assert detect_test_command(str(tmp_repo)) == "npm test"


# --------------------------------------------------------------------------
# Precision over recall: lookalike signals must NOT match
# --------------------------------------------------------------------------

def test_pyproject_pytest_in_comment_is_not_a_signal(tmp_repo):
    _write(
        tmp_repo,
        "pyproject.toml",
        "[project]\nname = \"demo\"\n# we should add a [tool.pytest] section later\n",
    )
    assert detect_test_command(str(tmp_repo)) is None


def test_pyproject_pytest_as_dependency_only_is_not_a_signal(tmp_repo):
    # pytest listed as a dependency but no [tool.pytest config section: the
    # rule is the config section, so this stays None.
    _write(
        tmp_repo,
        "pyproject.toml",
        "[project]\ndependencies = [\"pytest>=7.0\"]\n",
    )
    assert detect_test_command(str(tmp_repo)) is None


def test_package_json_jest_in_scripts_only_is_not_a_signal(tmp_repo):
    _write(
        tmp_repo,
        "package.json",
        '{"scripts": {"test": "jest"}, "dependencies": {}}',
    )
    assert detect_test_command(str(tmp_repo)) is None


def test_package_json_invalid_json_is_not_a_signal(tmp_repo):
    _write(tmp_repo, "package.json", '{"devDependencies": {"jest": ')
    assert detect_test_command(str(tmp_repo)) is None


def test_makefile_indented_test_colon_is_not_a_target(tmp_repo):
    # "test:" inside a recipe line is output text, not a make target.
    _write(tmp_repo, "Makefile", "all:\n\techo test: done\n")
    assert detect_test_command(str(tmp_repo)) is None


def test_makefile_tests_plural_target_is_not_the_test_target(tmp_repo):
    _write(tmp_repo, "Makefile", "tests:\n\t./run_tests\n")
    assert detect_test_command(str(tmp_repo)) is None


def test_manage_py_without_django_dependency_is_not_a_signal(tmp_repo):
    _write(tmp_repo, "manage.py", "#!/usr/bin/env python\n")
    _write(tmp_repo, "requirements.txt", "flask==3.0\n")
    assert detect_test_command(str(tmp_repo)) is None


def test_django_dependency_without_manage_py_is_not_a_signal(tmp_repo):
    _write(tmp_repo, "requirements.txt", "Django==4.2\n")
    assert detect_test_command(str(tmp_repo)) is None
