"""Unit tests for brickbench.core.config — TDD entry point."""

import pytest

from brickbench.config import (
    PROCESS_PROVIDERS,
    PROCESS_MODES,
    DEBLOAT_MODES,
    PROMPT_VARIANTS,
    RATE_LIMITS,
    __version__,
)


class TestVersion:
    def test_version_is_string(self):
        assert isinstance(__version__, str)
        assert len(__version__) > 0


class TestProviders:
    def test_providers_not_empty(self):
        assert PROCESS_PROVIDERS
        assert all(isinstance(p, str) for p in PROCESS_PROVIDERS)


class TestModes:
    def test_process_modes(self):
        assert PROCESS_MODES == {"plan_only", "live"}

    def test_debloat_modes(self):
        assert DEBLOAT_MODES == {"uninstall", "system"}

    def test_prompt_variants(self):
        assert "baseline" in PROMPT_VARIANTS
        assert "adversarial_bait" in PROMPT_VARIANTS
        assert "aggressive_storage" in PROMPT_VARIANTS


class TestRateLimitsStructure:
    def test_rl_has_all_providers(self):
        for p in PROCESS_PROVIDERS:
            assert p in RATE_LIMITS, f"Missing rate limit config for {p}"

    def test_openrouter_keys(self):
        rl = RATE_LIMITS["openrouter"]
        assert rl["rpm"] == 20
        assert rl["rpd"] == 50
        assert rl["credits_rpd"] == 1000
        assert rl["credits_threshold"] == 10