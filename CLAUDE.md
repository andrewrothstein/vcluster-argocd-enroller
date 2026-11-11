# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Kopf-based Kubernetes operator (Python) that watches for vCluster StatefulSets (label `app=vcluster`) and automatically creates/deletes corresponding ArgoCD cluster secrets. The operator extracts TLS credentials from vCluster secrets (`vc-{name}`) and creates ArgoCD-compatible cluster secrets (`vcluster-{namespace}-{name}`, ArgoCD cluster name `{namespace}-{name}`) in a configurable ArgoCD namespace, re-syncing them periodically.

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
uv run pytest tests/ -v                                          # All tests (CI default)
uv run pytest tests/ -k test_name -v -s                          # Single test
uv run pytest tests/ --cov=vcluster_argocd_enroller --cov-report=term-missing  # Coverage
task k3d-test                                                    # k3d integration test (up, smoke, real vcluster, down)
```

Markers: `integration`, `slow`, `cli`, `operator`.

The k3d tasks act on the current kubectl context: `k3d-up` creates cluster `enroller-test` and switches to it. Set `KUBECONFIG` to a scratch file to keep your own kubeconfig untouched.

## Linting

Pre-commit runs ruff (lint + format), mypy, and hadolint. Config: `line-length=120`, `target-version="py313"`, ruff selects `E,F,I,N,W`.

## Architecture

- **`src/vcluster_argocd_enroller/operator.py`** — Core logic. Kopf handlers (`@kopf.on.create`, `@kopf.on.resume`, `@kopf.on.update`, `@kopf.on.delete`, plus a `@kopf.timer` re-sync every `RESYNC_INTERVAL` seconds) on StatefulSets with `app=vcluster` label. Uses lazy K8s client init via `_ensure_k8s()` (safe to import without a kubeconfig). `ARGOCD_NAMESPACE` (default `"argocd"`), `SERVER_URL` (default `https://{name}.{namespace}`, the name vcluster's certificate covers) and `RESYNC_INTERVAL` are read from env vars. The sync reads the existing ArgoCD secret and creates it, replaces it only when its data or labels differ, or refuses to touch it when `owns()` says it is not this vCluster's enrollment (ownership = the `vcluster-argocd-enroller/namespace` and `/name` labels). It also deletes the namespace-less `vcluster-{name}` secret that 1.x wrote for the same vCluster.
- **`src/vcluster_argocd_enroller/cli.py`** — Cyclopts CLI. `run` (default) calls `kopf.configure()` + `kopf.run()` on a uvloop loop (liveness on `http://0.0.0.0:$HEALTH_PORT/healthz`, cluster-wide unless `--namespace`); log level comes from `--log-level` / `LOG_LEVEL`. Peering (leader election) is on only with `--peering-name` / `PEERING_NAME`, otherwise kopf runs standalone; each instance draws a random priority, because kopf pauses all peers of equal priority. Also: `check`, `enroll`, `unenroll`, `verify` subcommands, which share `ARGOCD_NAMESPACE`, `ar_secret_name` and `build_argocd_secret` with the operator. `verify` connects to a vCluster with the credentials enrolled in ArgoCD.
- **`src/vcluster_argocd_enroller/__init__.py`** — Exports `__version__` only. No eager imports.
- **`helm/vcluster-argocd-enroller/`** — Helm chart. Deploys as a Deployment with ClusterRole for secrets + statefulsets RBAC. Probes hit kopf's `/healthz` on the `health` container port (`networkPolicy.healthPort`). `leaderElection.enabled` adds a `ClusterKopfPeering` object (CRD in `crds/`), its RBAC rule and the `PEERING_NAME`/`POD_ID` env vars; the chart fails to render `replicaCount > 1` or autoscaling without it.
- **Tests** — `test_cli.py` mocks `kopf.run` and `CoreV1Api`. `test_operator_integration.py` uses lazy-init fixtures that pre-set `_k8s_configured`/`_core_v1_api`/`_apps_v1_api` globals and serves secrets from a dict (`serve_secrets`). `tests/k3d/` holds the fake vClusters for `task k3d-smoke`; `task k3d-vcluster` installs a real vcluster and runs `verify` against it.

## CI/CD (GitHub Actions)

- **ci.yaml** — Lint, test (Python 3.13), docker build+Trivy, helm lint, kubeconform validation, k3d integration test on PRs (`task k3d-install`, `k3d-smoke`, `k3d-vcluster`).
- **release.yaml** — Triggered by `v*` tags. Builds multi-arch (amd64/arm64) image, pushes to `ghcr.io`. Packages Helm chart to `oci://ghcr.io/{owner}/charts/`.
- **dev.yaml** — Pushes `edge`/branch-tagged images on main/develop push.

## Key Conventions

- Version is tracked in three places: `__init__.py`, `pyproject.toml`, `Chart.yaml` (CI updates Chart.yaml at release time via sed).
- The operator uses `kopf.TemporaryError(delay=60)` for retryable failures and `kopf.PermanentError` for non-retryable ones.
- Deletion handlers never raise PermanentError (to avoid blocking finalizer removal). The delete handler has `retries=DELETE_RETRIES`, after which kopf releases the finalizer anyway.
- Handlers return nothing: kopf would write a return value into the StatefulSet status.
- Never replace or delete an ArgoCD secret without checking `owns()` first.
- Helm chart deploys into `operator.vclusterNamespace` (default: `vcluster-system`), not a separate operator namespace.
- Python >= 3.13 required. Docker base image: `ghcr.io/astral-sh/uv:python3.13-trixie-slim`.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Branch Workflow

When starting a new PR branch, always run `git fetch origin` first and create the branch from the up-to-date `origin/main` (or the relevant upstream). Do not branch from a stale local ref.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
