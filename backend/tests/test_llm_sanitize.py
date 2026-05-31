import pytest

from backend.llm.sanitize import sanitize_for_log

pytestmark = pytest.mark.unit


def test_sanitize_redacts_provider_keys_and_bearer_tokens():
    text = (
        "gemini AIzaSyA123456789012345678901234567890 "
        "openai sk-abcdefghijklmnopqrstuvwxyz123456 "
        "anthropic sk-ant-abcdefghijklmnopqrstuvwxyz123456 "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    )

    sanitized = sanitize_for_log(text)

    assert "AIzaSyA" not in sanitized
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "abcdefghijklmnopqrstuvwxyz123456" not in sanitized
    assert sanitized.count("[REDACTED]") >= 4


def test_sanitize_redacts_secret_context_tokens():
    raw = "secret=abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
    sanitized = sanitize_for_log(raw)

    assert "abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in sanitized
    assert "secret=[REDACTED]" in sanitized


def test_sanitize_keeps_plain_commit_hash():
    commit = "0123456789abcdef0123456789abcdef01234567"
    text = f"commit {commit} failed tests"

    assert sanitize_for_log(text) == text


def test_sanitize_redacts_commit_like_value_near_secret_context():
    commit_like = "0123456789abcdef0123456789abcdef01234567"
    sanitized = sanitize_for_log(f"token: {commit_like}")

    assert commit_like not in sanitized
    assert "[REDACTED]" in sanitized


# All tokens below are fake, fixed-shape strings — not real credentials.
_FAKE_GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_GHO = "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_GHS = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_GHU = "ghu_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_GHR = "ghr_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_FAKE_PAT = "github_pat_" + "11ABCDEFG0_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9"


@pytest.mark.parametrize(
    "token",
    [_FAKE_GHP, _FAKE_GHO, _FAKE_GHS, _FAKE_GHU, _FAKE_GHR, _FAKE_PAT],
)
def test_sanitize_redacts_github_tokens(token):
    sanitized = sanitize_for_log(f"git push failed: {token}")

    assert token not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_redacts_tokenized_github_https_remote():
    raw = f"fatal: unable to access https://{_FAKE_GHP}@github.com/owner/repo.git"
    sanitized = sanitize_for_log(raw)

    assert _FAKE_GHP not in sanitized
    assert "https://[REDACTED]@github.com/owner/repo.git" in sanitized


def test_sanitize_redacts_username_token_github_https_remote():
    raw = f"remote: https://octocat:{_FAKE_PAT}@github.com/owner/repo.git"
    sanitized = sanitize_for_log(raw)

    assert _FAKE_PAT not in sanitized
    assert "octocat" not in sanitized
    assert "https://[REDACTED]@github.com/owner/repo.git" in sanitized


def test_sanitize_keeps_plain_github_url_without_credentials():
    url = "https://github.com/owner/repo.git"
    text = f"failed to push to {url}"

    assert sanitize_for_log(text) == text


def test_sanitize_keeps_ssh_github_remote():
    # SSH remotes carry no token; the leading git@ must not be destroyed.
    url = "git@github.com:owner/repo.git"
    text = f"failed to push to {url}"

    assert sanitize_for_log(text) == text
