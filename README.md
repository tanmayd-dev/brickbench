# brickbench

Evaluation harness that benchmarks LLM-recommended Android debloat actions for **safety and correctness** — propose a plan with any model, verify it against real emulator checks, and score it reproducibly.

## How It Works

1. **Enqueue** — `brickbench run` builds a matrix of `(model × ROM target × prompt variant × mode)` runs in SQLite.
2. **Propose** — each run asks the model (OpenRouter or local Ollama) for a minimal JSON debloat plan (≤ 2 packages).
3. **Verify** — the plan is checked two ways:
   - `plan_only` — fast static check against a learned safe-list + critical-package set (no device needed)
   - `live` — boots an Android emulator snapshot, applies the plan, and runs functional probes (`pm list`, `dumpsys`, service check, launcher start)
4. **Score** — boot success, boot time, critical violations, bytes freed, and tokens used are recorded with a SHA-256 result hash; verified verdicts feed the safe-list for future fast-path lookups.
5. **Export** — dump runs + verdicts to JSON/CSV for analysis.

Targets cover AOSP (vanilla/GApps/Play), LineageOS, /e/OS GSI, and an OEM-combined image. Concurrency is semaphore-gated per emulator with atomic run claiming, so parallel workers never double-execute.

## Tech Stack

- **Core:** Python 3.12+, Click CLI, SQLite (WAL) state store
- **Models:** OpenRouter REST API, Ollama (local models)
- **Verification:** Android SDK emulator + ADB, snapshot boot, functional probes
- **Testing:** pytest (unit + emulator-gated integration tests)

## Getting Started

### Prerequisites

- Python 3.12+
- For `live` mode: Android SDK + emulator, an AVD named `bb_aosp36` with a `clean_boot` snapshot, and `adb` on `PATH`
- For local models: `ollama serve`

### Setup

```bash
cp .env.example .env   # set OPENROUTER_API_KEY
pip install -e .
```

Optional env overrides: `BRICKBENCH_AVD`, `BRICKBENCH_SNAPSHOT`, `BRICKBENCH_EMU_RAM_MB`.

### Run

```bash
brickbench init                                       # create data/runs + SQLite store
brickbench run --providers openrouter local \         # enqueue evaluation matrix
  --modes plan_only live
brickbench resume --max-emulator 1 --max-llm 4        # execute pending runs
brickbench export --format json --output results.json # dump results
```

### Test

```bash
pytest -q               # unit tests
pytest -q -m "not slow" # skip emulator-gated tests
```

## CLI Reference

| Command | Description |
| ------- | ----------- |
| `init` | Scaffold `data/runs` and the SQLite schema (`runs`, `verdicts`) |
| `run` | Enqueue the provider × target × mode matrix |
| `resume` | Execute pending runs with emulator/LLM concurrency limits |
| `reset` | Drop results (confirmation required) |
| `export` | Dump runs + verdicts to JSON or CSV |

## Project Structure

```
brickbench/
├── cli.py        # Click commands: init, run, resume, reset, export
├── config.py     # Version, providers, modes, ROM targets, rate limits
├── runner.py     # Run orchestration, model dispatch, concurrency
└── evaluator.py  # Plan verification (static + emulator) and scoring
tests/unit/       # Config + evaluator tests
```
