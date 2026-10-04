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

# Rate-limit guardrails (per-provider; enforced in runner)
RATE_LIMITS = {
    "openrouter": {"rpm": 20, "rpd": 50, "credits_rpd": 1000, "credits_threshold": 10},
    "agy": {"weekly_quota_notice": "weekly hard cap; weekly reset; ~250 req/5h unofficial"},
    "zen": {"best_effort": True, "transient_403_rate": "~1/12 runs"},
    "codex": {"rpm": None, "rpd": None},  # per OpenAI key; user-managed
}