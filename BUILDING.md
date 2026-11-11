# vCluster ArgoCD Enroller

A Kubernetes operator with CLI that automatically enrolls vCluster instances in ArgoCD for GitOps management.

## Overview

This operator watches for vCluster StatefulSets and automatically:
1. Extracts the vCluster credentials from its secret
2. Creates an ArgoCD cluster secret with proper authentication, and keeps it in sync
3. Cleans up ArgoCD secrets when vClusters are deleted

## Setup

### Prerequisites

- Python 3.13+
- `uv` package manager
- Kubernetes cluster with ArgoCD installed
- vCluster CLI (optional, for testing)

### Installation

```bash
# Install with uv
uv sync

# Or install as editable package
uv pip install -e .
```

### CLI Usage

```bash
# Run the operator
uv run vcluster-argocd-enroller

# Run in development mode (verbose; with --peering-name it outranks, and so pauses, the deployed instances)
uv run vcluster-argocd-enroller --dev --verbose

# Check vCluster enrollment status
uv run vcluster-argocd-enroller check

# Manually enroll a vCluster
uv run vcluster-argocd-enroller enroll my-vcluster --namespace vcluster-ns

# Remove a vCluster from ArgoCD
uv run vcluster-argocd-enroller unenroll my-vcluster vcluster-ns --confirm

# Connect to a vCluster with the credentials enrolled in ArgoCD (from inside the cluster)
vcluster-argocd-enroller verify my-vcluster vcluster-ns

# Run with specific namespace watching
uv run vcluster-argocd-enroller --namespace my-namespace

# Get help
uv run vcluster-argocd-enroller --help
```

### Quick Development

```bash
# Install dependencies and run in dev mode
uv sync
uv run vcluster-argocd-enroller --dev --verbose
```

### Testing

```bash
# Run unit tests
uv run pytest

# Run unit tests with coverage
uv run pytest --cov=vcluster_argocd_enroller

# Integration test in a throwaway k3d cluster: chart install, fake vClusters, then a real vcluster
# whose enrolled credentials are checked against its API server (needs docker, k3d, kubectl, helm)
task k3d-test

# The same steps one at a time, keeping the cluster around
task k3d-up
task k3d-smoke
task k3d-vcluster
task k3d-down

# Check existing vClusters
uv run vcluster-argocd-enroller check --show-secrets
```

### Building Docker Image

```bash
task build
```

## How It Works

The operator monitors StatefulSets with the label `app: vcluster` and:

1. **On Create/Resume/Update, and every `RESYNC_INTERVAL` seconds**:
   - Extracts the credentials from secret `vc-{vcluster-name}`
   - Creates ArgoCD cluster secret `vcluster-{namespace}-{vcluster-name}` in the ArgoCD namespace, or replaces it
     when its content differs
   - Secret contains cluster endpoint and TLS certificates
   - Removes the `vcluster-{vcluster-name}` secret written by 1.x for the same vCluster

2. **On Delete**:
   - Removes the corresponding ArgoCD cluster secret
   - Cleans up gracefully even if secret doesn't exist

Secrets that the operator did not write for that vCluster are never replaced or deleted.

## Configuration

The operator expects:
- vCluster secrets named: `vc-{vcluster-name}`
- vCluster StatefulSets labeled with: `app=vcluster`

Environment variables (the Helm chart sets them from its values):

| Variable | Meaning | Default |
| -------- | ------- | ------- |
| `ARGOCD_NAMESPACE` | Namespace ArgoCD is installed in | `argocd` |
| `SERVER_URL` | Address ArgoCD reaches a vCluster at; `{name}` and `{namespace}` are replaced | `https://{name}.{namespace}` |
| `RESYNC_INTERVAL` | Seconds between re-syncs of each vCluster | `300` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR` | `INFO` |
| `HEALTH_PORT` | Port serving `/healthz` | `8080` |
| `PEERING_NAME` | kopf peering object to run leader election in; unset means no peering | unset |

## Development

This project uses:
- `uv` for Python dependency management
- `kopf` as the Kubernetes operator framework
- `ruff` for linting and formatting

### Project Structure

```
vcluster-argocd-enroller/
├── src/
│   └── vcluster_argocd_enroller/
│       ├── __init__.py       # Package initialization
│       ├── __main__.py       # Package entry point
│       ├── cli.py            # Cyclopts CLI implementation
│       └── operator.py       # Kopf operator logic
├── tests/
│   ├── __init__.py
│   ├── test_cli.py          # CLI tests with mocked kopf and K8s
│   ├── test_operator_integration.py  # Handler tests with mocked K8s
│   └── k3d/                 # Manifests for the k3d integration test (task k3d-test)
├── pyproject.toml            # Python project configuration (uv)
├── uv.lock                   # Locked dependencies
├── pytest.ini                # Pytest configuration
├── Taskfile.yml              # Task automation
├── Dockerfile                # Container image definition
├── .gitignore                # Git ignore rules
├── README.md                 # User/operator guide
└── BUILDING.md               # This file
```
