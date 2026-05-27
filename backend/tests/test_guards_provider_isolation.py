"""
test_guards_provider_isolation.py
Static source guards for LLM provider isolation.

Rules enforced:
  - google.generativeai may only be imported in backend/llm/providers/gemini.py
  - anthropic SDK may only be imported in backend/llm/providers/anthropic.py
  - openai SDK may only be imported in backend/llm/providers/openai.py
    and backend/llm/providers/deepseek.py (DeepSeek uses the OpenAI-compatible API)
  - All listed pipeline files must not import provider SDKs directly
  - triage, planner, coder must call complete_for_role from backend.llm
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).parent.parent
PIPELINE_ROOT = BACKEND_ROOT / "pipeline"
GEMINI_PROVIDER = BACKEND_ROOT / "llm" / "providers" / "gemini.py"
ANTHROPIC_PROVIDER = BACKEND_ROOT / "llm" / "providers" / "anthropic.py"
OPENAI_PROVIDER = BACKEND_ROOT / "llm" / "providers" / "openai.py"
DEEPSEEK_PROVIDER = BACKEND_ROOT / "llm" / "providers" / "deepseek.py"


@pytest.mark.unit
def test_gemini_sdk_import_only_in_provider_adapter():
    """Only backend/llm/providers/gemini.py may import google.generativeai at runtime."""
    gemini_source = GEMINI_PROVIDER.read_text(encoding="utf-8")
    assert "google.generativeai" in gemini_source, (
        "backend/llm/providers/gemini.py no longer imports google.generativeai. "
        "Update this guard if the SDK was intentionally removed from the adapter."
    )

    violations = []
    for py_file in sorted(BACKEND_ROOT.rglob("*.py")):
        if py_file == GEMINI_PROVIDER:
            continue
        if py_file.is_relative_to(BACKEND_ROOT / "tests"):
            continue
        source = py_file.read_text(encoding="utf-8")
        if "google.generativeai" in source:
            violations.append(str(py_file.relative_to(BACKEND_ROOT)))

    assert violations == [], (
        "Runtime backend files outside backend/llm/providers/gemini.py "
        "import google.generativeai:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.unit
def test_anthropic_sdk_import_only_in_provider_adapter():
    """Only backend/llm/providers/anthropic.py may import the anthropic SDK at runtime."""
    anthropic_source = ANTHROPIC_PROVIDER.read_text(encoding="utf-8")
    assert "import anthropic" in anthropic_source, (
        "backend/llm/providers/anthropic.py no longer imports anthropic. "
        "Update this guard if the SDK was intentionally removed from the adapter."
    )

    violations = []
    for py_file in sorted(BACKEND_ROOT.rglob("*.py")):
        if py_file == ANTHROPIC_PROVIDER:
            continue
        if py_file.is_relative_to(BACKEND_ROOT / "tests"):
            continue
        source = py_file.read_text(encoding="utf-8")
        if "import anthropic" in source:
            violations.append(str(py_file.relative_to(BACKEND_ROOT)))

    assert violations == [], (
        "Runtime backend files outside backend/llm/providers/anthropic.py "
        "import anthropic SDK:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "pipeline_file",
    [
        pytest.param("triage.py", id="triage"),
        pytest.param("planner.py", id="planner"),
        pytest.param("coder.py", id="coder"),
        pytest.param("orchestrator.py", id="orchestrator"),
        pytest.param("chunked_orchestrator.py", id="chunked_orchestrator"),
    ],
)
def test_pipeline_files_do_not_import_gemini_sdk(pipeline_file):
    source = (PIPELINE_ROOT / pipeline_file).read_text(encoding="utf-8")
    assert "google.generativeai" not in source, (
        f"{pipeline_file} must not import google.generativeai directly"
    )
    assert "genai" not in source, (
        f"{pipeline_file} must not reference genai (Gemini SDK alias)"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "pipeline_file",
    [
        pytest.param("triage.py", id="triage"),
        pytest.param("planner.py", id="planner"),
        pytest.param("coder.py", id="coder"),
        pytest.param("orchestrator.py", id="orchestrator"),
        pytest.param("chunked_orchestrator.py", id="chunked_orchestrator"),
    ],
)
def test_pipeline_files_do_not_import_anthropic_sdk(pipeline_file):
    source = (PIPELINE_ROOT / pipeline_file).read_text(encoding="utf-8")
    assert "import anthropic" not in source, (
        f"{pipeline_file} must not import the anthropic SDK directly"
    )


@pytest.mark.unit
def test_openai_sdk_import_only_in_provider_adapters():
    """Only openai.py and deepseek.py provider adapters may import the openai SDK at runtime.
    DeepSeek uses the OpenAI-compatible API and reuses the openai SDK client."""
    openai_source = OPENAI_PROVIDER.read_text(encoding="utf-8")
    assert "import openai" in openai_source, (
        "backend/llm/providers/openai.py no longer imports openai. "
        "Update this guard if the SDK was intentionally removed from the adapter."
    )

    deepseek_source = DEEPSEEK_PROVIDER.read_text(encoding="utf-8")
    assert "import openai" in deepseek_source, (
        "backend/llm/providers/deepseek.py no longer imports openai. "
        "Update this guard if the DeepSeek adapter no longer uses the OpenAI-compatible client."
    )

    _openai_adapters = {OPENAI_PROVIDER, DEEPSEEK_PROVIDER}

    violations = []
    for py_file in sorted(BACKEND_ROOT.rglob("*.py")):
        if py_file in _openai_adapters:
            continue
        if py_file.is_relative_to(BACKEND_ROOT / "tests"):
            continue
        source = py_file.read_text(encoding="utf-8")
        if "import openai" in source:
            violations.append(str(py_file.relative_to(BACKEND_ROOT)))

    assert violations == [], (
        "Runtime backend files outside openai/deepseek provider adapters "
        "import openai SDK:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "pipeline_file",
    [
        pytest.param("triage.py", id="triage"),
        pytest.param("planner.py", id="planner"),
        pytest.param("coder.py", id="coder"),
        pytest.param("orchestrator.py", id="orchestrator"),
        pytest.param("chunked_orchestrator.py", id="chunked_orchestrator"),
    ],
)
def test_pipeline_files_do_not_import_openai_sdk(pipeline_file):
    source = (PIPELINE_ROOT / pipeline_file).read_text(encoding="utf-8")
    assert "import openai" not in source, (
        f"{pipeline_file} must not import the openai SDK directly"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "role_file",
    [
        pytest.param("triage.py", id="triage"),
        pytest.param("planner.py", id="planner"),
        pytest.param("coder.py", id="coder"),
    ],
)
def test_pipeline_roles_use_backend_llm_api(role_file):
    """triage, planner, and coder must use complete_for_role from backend.llm."""
    source = (PIPELINE_ROOT / role_file).read_text(encoding="utf-8")
    assert "complete_for_role" in source, (
        f"{role_file} must call complete_for_role from backend.llm"
    )
    assert "from backend.llm" in source, (
        f"{role_file} must import from backend.llm, not a direct provider SDK"
    )
