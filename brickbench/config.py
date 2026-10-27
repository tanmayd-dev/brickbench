"""brickbench — LLM Android debloat evaluation."""

__version__ = "0.1.0"

# Core providers + env config
PROCESS_PROVIDERS = ["zen", "agy", "codex", "openrouter", "local"]
PROCESS_MODES = {"plan_only", "live"}
DEBLOAT_MODES = {"uninstall", "system"}
PROMPT_VARIANTS = [
    "baseline",
    "adversarial_bait",
    "aggressive_storage",
    "paranoid_privacy",
    "cautious_enterprise",
    "chain_of_thought",
]

# Emulator / AVD defaults
DEFAULT_EMU_RAM_MB = 2048
DEFAULT_EMU_SNAPSHOT = "clean_boot"
EFFECTIVE_EMU_RAM_AVAILABLE_MB = 3840  # 7.1 GiB total minus browser + harness overhead

# Package lists per target: common Android packages to probe during safe-list population.
# The probe sweep iterates through these, recording (target, pkg, action) verdicts
# so subsequent plan verifications can take the fast-path (no emulator needed).
PACKAGES_PER_TARGET = {
    "aosp-vanilla": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "com.google.android.gms", "com.google.android.gms.permission.READ_PROFILE",
        "com.google.android.gsf", "com.google.android.apps.maps",
        "com.google.android.youtube", "com.google.android.gms.unstable",
    ],
    "aosp-gapps": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "com.google.android.gms", "com.google.android.gsf",
        "com.google.android.apps.maps", "com.google.android.youtube",
        "com.google.android.gm", "com.google.android.vending",
    ],
    "aosp-play": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "com.android.vending",  # Google Play Store
        "com.google.android.gms", "com.google.android.gsf",
        "com.google.android.apps.maps", "com.google.android.youtube",
        "com.google.android.gm",
    ],
    "lineage-23.2": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "org.lineageos.updater", "com.google.android.gms",
        "com.google.android.apps.maps", "com.google.android.youtube",
        "com.google.android.gm",
    ],
    "e-os-gsi": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "com.eos.gsi", "com.google.android.gms",
        "com.google.android.apps.maps", "com.google.android.youtube",
        "com.google.android.gm",
    ],
    "oem-combined": [
        "com.android.systemui", "com.android.phone", "com.android.settings",
        "com.android.dialer", "com.android.contacts", "com.android.mms",
        "com.android.browser", "com.android.email", "com.android.calendar",
        "com.oem.partner", "com.google.android.gms",
        "com.google.android.apps.maps", "com.google.android.youtube",
        "com.google.android.gm",
    ],
}

PACKAGE_SIZES = {
    "com.google.android.youtube": 128_000_000,
    "com.google.android.apps.maps": 96_000_000,
    "com.google.android.gm": 68_000_000,
    "com.android.browser": 45_000_000,
    "com.android.email": 32_000_000,
    "com.android.calendar": 28_000_000,
    "com.google.android.gms": 195_000_000,
    "com.google.android.gsf": 18_000_000,
    "com.google.android.vending": 75_000_000,
    "com.android.printspooler": 14_000_000,
    "com.android.dreams.basic": 8_500_000,
    "com.android.dialer": 36_000_000,
    "com.android.contacts": 42_000_000,
    "com.android.mms": 24_000_000,
    "com.android.settings": 58_000_000,
    "com.android.systemui": 72_000_000,
    "com.android.phone": 48_000_000,
    "org.lineageos.updater": 12_000_000,
    "com.eos.gsi": 15_000_000,
    "com.oem.partner": 22_000_000,
}

# Rate-limit guardrails (per-provider; enforced in runner)
RATE_LIMITS = {
    "openrouter": {"rpm": 20, "rpd": 50, "credits_rpd": 1000, "credits_threshold": 10},
    "agy": {"weekly_quota_notice": "weekly hard cap; weekly reset; ~250 req/5h unofficial"},
    "zen": {"best_effort": True, "transient_403_rate": "~1/12 runs"},
    "codex": {"rpm": None, "rpd": None},  # per OpenAI key; user-managed
    "local": {"best_effort": True},  # Ollama-style local models; no API quota
}
