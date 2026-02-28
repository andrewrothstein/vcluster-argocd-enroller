# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Kopf-based Kubernetes operator (Python) that watches for vCluster StatefulSets (label `app=vcluster`) and automatically creates/deletes corresponding ArgoCD cluster secrets. The operator extracts TLS credentials from vCluster secrets (`vc-{name}`) and creates ArgoCD-compatible cluster secrets (`vcluster-{name}`) in a configurable ArgoCD namespace.

## Build & Dev Commands

```bash
uv sync                              # Install dependencies
uv sync --all-extras                  # Install with dev deps
uv run vcluster-argocd-enroller      # Run operator
uv run vcluster-argocd-enroller --dev --verbose  # Dev mode
task build                           # Docker build
task helm-lint                       # Lint Helm chart
task helm-template                   # Render templates
```

## Testing

```bash
uv run pytest tests/ -v                                          # All tests
uv run pytest tests/ -v -m "not e2e"                             # Skip e2e (CI default)
uv run pytest tests/ -k test_name -v -s                          # Single test
uv run pytest tests/ --cov=vcluster_argocd_enroller --cov-report=term-missing  # Coverage
```

Markers: `integration`, `e2e` (requires real cluster), `slow`, `cli`, `operator`.

## Linting

Pre-commit runs ruff (lint + format), mypy, and hadolint. Config: `line-length=120`, `target-version="py313"`, ruff selects `E,F,I,N,W`.

## Architecture

- **`src/vcluster_argocd_enroller/operator.py`** — Core logic. Kopf handlers (`@kopf.on.create`, `@kopf.on.resume`, `@kopf.on.update`, `@kopf.on.delete`) on StatefulSets with `app=vcluster` label. Uses lazy K8s client init via `_ensure_k8s()` (safe to import without a kubeconfig). `ARGOCD_NAMESPACE` read from env var (default `"argocd"`). Create-or-update is idempotent (catches 409 Conflict, falls back to `replace_namespaced_secret`).
- **`src/vcluster_argocd_enroller/cli.py`** — Cyclopts CLI. `run` (default) launches kopf with `--liveness=http://0.0.0.0:8080/healthz` and `--all-namespaces`. Also: `check`, `enroll`, `unenroll` subcommands.
- **`src/vcluster_argocd_enroller/__init__.py`** — Exports `__version__` only. No eager imports.
- **`helm/vcluster-argocd-enroller/`** — Helm chart. Deploys as a Deployment with ClusterRole for secrets + statefulsets RBAC. Probes hit kopf's `/healthz` on port 8080.
- **Tests** — `test_operator_integration.py` uses lazy-init fixtures that pre-set `_k8s_configured`/`_core_v1_api`/`_apps_v1_api` globals. `test_e2e.py` needs a live cluster.

## CI/CD (GitHub Actions)

- **ci.yaml** — Lint, test (Python 3.13), docker build+Trivy, helm lint, kubeconform validation, k3d integration test on PRs.
- **release.yaml** — Triggered by `v*` tags. Builds multi-arch (amd64/arm64) image, pushes to `ghcr.io`. Packages Helm chart to `oci://ghcr.io/{owner}/charts/`.
- **dev.yaml** — Pushes `edge`/branch-tagged images on main/develop push.

## Key Conventions

- Version is tracked in three places: `__init__.py`, `pyproject.toml`, `Chart.yaml` (CI updates Chart.yaml at release time via sed).
- The operator uses `kopf.TemporaryError(delay=60)` for retryable failures and `kopf.PermanentError` for non-retryable ones.
- Deletion handlers never raise PermanentError (to avoid blocking finalizer removal).
- Helm chart deploys into `operator.vclusterNamespace` (default: `vcluster-system`), not a separate operator namespace.
- Python >= 3.13 required. Docker base image: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`.
