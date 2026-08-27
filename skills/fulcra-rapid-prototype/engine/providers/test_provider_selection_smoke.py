"""
Standalone smoke test for provider auto-selection logic (no network calls
-- this only exercises harness.providers.select_provider's environment-
variable detection, so it's safe/fast to run every time, unlike the
per-provider test_*_smoke.py scripts which hit a real API).

Run directly:

    .venv/bin/python -m harness.providers.test_provider_selection_smoke
"""

import os

from harness.providers import NoProviderConfiguredError, select_provider


# Every env var any detection path reads, so each test case can start
# from a genuinely clean slate regardless of what's set in the real
# environment/.env this test happens to run in.
_RELEVANT_VARS = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_CLOUD_PROJECT",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
]


class _clean_env:
    """Context manager: clears every relevant var, restores the real
    environment on exit (including vars that didn't exist before)."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _RELEVANT_VARS}
        for k in _RELEVANT_VARS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    print("--- Test 1: Claude Code OAuth wins over everything else ---")
    with _clean_env():
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "fake-token"
        os.environ["GEMINI_API_KEY"] = "fake-key"
        os.environ["OPENAI_API_KEY"] = "fake-key"
        assert select_provider() == "anthropic"
    print("OK")

    print("\n--- Test 2: Gemini ADC (GOOGLE_CLOUD_PROJECT) wins over API keys ---")
    with _clean_env():
        os.environ["GOOGLE_CLOUD_PROJECT"] = "some-project"
        os.environ["GEMINI_API_KEY"] = "fake-key"
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        assert select_provider() == "gemini"
    print("OK")

    print("\n--- Test 3: falls back to GEMINI_API_KEY when no OAuth/ADC present ---")
    with _clean_env():
        os.environ["GEMINI_API_KEY"] = "fake-key"
        assert select_provider() == "gemini"
    print("OK")

    print("\n--- Test 4: falls back to ANTHROPIC_API_KEY ---")
    with _clean_env():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        assert select_provider() == "anthropic"
    print("OK")

    print("\n--- Test 5: falls back to OPENAI_API_KEY (last resort, no OAuth path) ---")
    with _clean_env():
        os.environ["OPENAI_API_KEY"] = "fake-key"
        assert select_provider() == "openai"
    print("OK")

    print("\n--- Test 6: raises a clear, actionable error when nothing is configured ---")
    with _clean_env():
        try:
            select_provider()
            raise AssertionError("expected NoProviderConfiguredError")
        except NoProviderConfiguredError as exc:
            assert "claude setup-token" in str(exc)
            assert "gcloud auth application-default login" in str(exc)
    print("OK")

    print("\nAll provider-selection smoke checks passed.")


if __name__ == "__main__":
    main()
