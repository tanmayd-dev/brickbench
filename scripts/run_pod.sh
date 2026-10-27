#!/usr/bin/env bash
set -eo pipefail

if ! command -v podman >/dev/null 2>&1; then
    echo "❌ Error: 'podman' is not installed or not in PATH." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "🔨 Building container image (brickbench:latest)..."
podman build --network host -t brickbench:latest -f Containerfile .

# Ensure host-side mount directories exist
mkdir -p data/runs dist

ENV_ARG=""
if [ -f .env ]; then
    ENV_ARG="--env-file .env"
fi

echo "🚀 Running container..."
# shellcheck disable=SC2086
podman run --network host --rm -v "$(pwd)/data:/workspace/data:z" -v "$(pwd)/dist:/workspace/dist:z" ${ENV_ARG} brickbench:latest "$@"
