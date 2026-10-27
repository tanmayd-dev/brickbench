#!/usr/bin/env bash
set -eo pipefail

# Route custom CLI arguments or arbitrary commands
if [ $# -gt 0 ] && [ "$1" != "run-all" ]; then
    case "$1" in
        -*|init|run|resume|reset|export|report)
            exec brickbench "$@"
            ;;
        *)
            exec "$@"
            ;;
    esac
fi

# Ensure output and data directories exist
mkdir -p data/runs dist

# Determine run mode and target database
if [ -n "${OPENROUTER_API_KEY:-}" ] && [ "$OPENROUTER_API_KEY" != "CHANGE_ME" ]; then
    echo "🔑 OPENROUTER_API_KEY detected. Running evaluation matrix..."
    brickbench init
    brickbench run --modes plan_only --targets aosp-vanilla --providers openrouter
    brickbench resume --max-llm 2 --max-steps 5
    DB="data/runs/brickbench.db"
elif [ -f "data/runs/brickbench.db" ]; then
    echo "📁 Using existing database at data/runs/brickbench.db"
    DB="data/runs/brickbench.db"
else
    echo "ℹ️  No OPENROUTER_API_KEY or existing DB found; using deterministic fixture database."
    DB="data/fixtures/report_fixture.db"
fi

# Check if DB has scored results; if not, fallback to fixture DB for a complete report
SCORED_COUNT=$(python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$DB')
    tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
    if 'run_results' in tables:
        print(conn.execute('SELECT count(*) FROM run_results').fetchone()[0])
    else:
        print(0)
    conn.close()
except Exception:
    print(0)
")

if [ "$SCORED_COUNT" -eq 0 ] && [ "$DB" != "data/fixtures/report_fixture.db" ]; then
    echo "ℹ️  No scored runs found in $DB yet; using deterministic fixture database for report."
    DB="data/fixtures/report_fixture.db"
fi

# Generate outputs
echo "📊 Generating reports to dist/..."
brickbench report --db "$DB" --output dist/index.html --format html
brickbench report --db "$DB" --output dist/report.pdf --format pdf
echo "✅ Done! Generated dist/index.html and dist/report.pdf"
