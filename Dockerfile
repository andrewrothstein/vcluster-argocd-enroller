# Build stage - install the project and its locked dependencies into a venv
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:6ff812e34a0e14b4b8bc9ddf25e77b3e16bac6e0c4d9b67641f40366fa382d4c AS builder

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install exactly what uv.lock pins, so the image runs the dependency set CI tested
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage - use uv base image
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:6ff812e34a0e14b4b8bc9ddf25e77b3e16bac6e0c4d9b67641f40366fa382d4c

LABEL org.opencontainers.image.title="vcluster-argocd-enroller"
LABEL org.opencontainers.image.description="Kubernetes operator that automatically enrolls vCluster instances in ArgoCD"
LABEL org.opencontainers.image.source="https://github.com/andrewrothstein/vcluster-argocd-enroller"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Create non-root user (kopf/asyncio calls getpwuid which needs a passwd entry)
RUN useradd --uid 1000 --no-create-home --shell /sbin/nologin appuser

# Copy the venv from builder (same base image, so its interpreter path still resolves)
COPY --from=builder /app/.venv /app/.venv

# Environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Run as non-root
USER 1000

# Run the operator via the installed console script
CMD ["vcluster-argocd-enroller"]
