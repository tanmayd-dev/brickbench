FROM python:3.12-slim-bookworm

# System dependencies for WeasyPrint, font rendering, and network utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-dejavu-core \
    fontconfig \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install Python dependencies first so they remain cached across code/data changes
RUN pip install --no-cache-dir "weasyprint>=60.0" "click>=8.0" "tqdm>=4.60"

# Copy project files
COPY pyproject.toml README.md ./
COPY brickbench/ ./brickbench/
COPY data/ ./data/
COPY scripts/ ./scripts/

# Ensure entrypoint and scripts are executable
RUN chmod +x /workspace/scripts/*.sh || true

# Install brickbench package
RUN pip install --no-cache-dir --no-deps -e .

# Runtime workspace directories
RUN mkdir -p /workspace/data/runs /workspace/dist

ENTRYPOINT ["/workspace/scripts/docker-entrypoint.sh"]
CMD ["run-all"]
