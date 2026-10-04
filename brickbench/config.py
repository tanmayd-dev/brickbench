"""brickbench — LLM Android debloat evaluation."""

__version__ = "0.1.0"

# Core providers + env config
PROCESS_PROVIDERS = ["zen", "agy", "codex", "openrouter"]
PROCESS_MODES = {"plan_only", "live"}
DEBLOAT_MODES = {"uninstall", "system"}

# Emulator / AVD defaults
DEFAULT_EMU_RAM_MB = 2048
DEFAULT_EMU_SNAPSHOT = "clean_boot"
EFFECTIVE_EMU_RAM_AVAILABLE_MB = 3840  # 7.1 GiB total minus browser + harness overhead
EFFECTIVE_EMU_RAM_AVAILABLE_MB_QUERY = "SELECT value FROM pragma_table_info WHERE name='avail_mem'"  # placeholder

# Rate-limit guardrails (per-provider; enforced in runner)
RATE_LIMITS = {
    "openrouter": {"rpm": 20, "rpd": 50, "credits_rpd": 1000, "credits_threshold": 10},
    "agy": {"weekly_quota_notice": "weekly hard cap; weekly reset; ~250 req/5h unofficial"},
    "zen": {"best_effort": True, "transient_403_rate": "~1/12 runs"},
    "codex": {"rpm": None, "rpd": None},  # per OpenAI key; user-managed
}