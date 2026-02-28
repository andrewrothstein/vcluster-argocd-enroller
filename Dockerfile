# Build stage - build the wheel using uv
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build the wheel
RUN uv build --wheel --out-dir /dist

# Runtime stage - use uv base image
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

LABEL org.opencontainers.image.title="vcluster-argocd-enroller"
LABEL org.opencontainers.image.description="Kubernetes operator that automatically enrolls vCluster instances in ArgoCD"
LABEL org.opencontainers.image.source="https://github.com/andrewrothstein/vcluster-argocd-enroller"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Copy the wheel from builder
COPY --from=builder /dist/*.whl /tmp/

# Create non-root user (kopf/asyncio calls getpwuid which needs a passwd entry)
RUN useradd --uid 1000 --no-create-home --shell /sbin/nologin appuser

# Install the wheel and its dependencies
RUN uv venv && \
    uv pip install --no-cache /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Run as non-root
USER 1000

# Run the operator via the installed console script
CMD ["vcluster-argocd-enroller"]
