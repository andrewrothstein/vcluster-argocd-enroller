# vCluster ArgoCD Enroller

[![CI](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml/badge.svg)](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml)
[![License](https://img.shields.io/github/license/andrewrothstein/vcluster-argocd-enroller)](https://github.com/andrewrothstein/vcluster-argocd-enroller/blob/main/LICENSE)

A Kubernetes operator that automatically enrolls [vCluster](https://www.vcluster.com/) instances in [ArgoCD](https://argo-cd.readthedocs.io/) for GitOps management.

## How It Works

The operator watches for vCluster StatefulSets (labeled `app=vcluster`) across your cluster. When a vCluster is created or updated, it:

1. Reads the TLS credentials from the vCluster secret (`vc-{name}`)
2. Creates an ArgoCD-compatible cluster secret (`vcluster-{name}`) in the ArgoCD namespace
3. Cleans up the ArgoCD secret when the vCluster is deleted

This lets ArgoCD immediately discover and deploy to new vClusters without manual cluster registration.

## Prerequisites

- Kubernetes cluster (1.19+)
- [ArgoCD](https://argo-cd.readthedocs.io/en/stable/getting_started/) installed
- [vCluster](https://www.vcluster.com/docs/getting-started/setup) instances deployed with default naming conventions

## Quick Start

Install via Helm from the OCI registry:

```bash
helm install vcluster-argocd-enroller \
  oci://ghcr.io/andrewrothstein/charts/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --create-namespace
```

The operator will immediately begin watching for vCluster StatefulSets and creating the corresponding ArgoCD cluster secrets.

## Configuration

Key Helm values:

| Parameter | Description | Default |
| --------- | ----------- | ------- |
| `operator.argoCDNamespace` | ArgoCD namespace | `argocd` |
| `operator.vclusterNamespace` | Namespace for vCluster instances | `vcluster-system` |
| `operator.logLevel` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `resources.requests.cpu` | CPU request | `100m` |
| `resources.requests.memory` | Memory request | `128Mi` |
| `resources.limits.cpu` | CPU limit | `200m` |
| `resources.limits.memory` | Memory limit | `256Mi` |

Example with custom values:

```bash
helm install vcluster-argocd-enroller \
  oci://ghcr.io/andrewrothstein/charts/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --create-namespace \
  --set operator.argoCDNamespace=argocd \
  --set operator.logLevel=DEBUG
```

See the [Helm chart README](helm/vcluster-argocd-enroller/README.md) for the full list of configurable parameters, HA setup, network policies, and monitoring options.

## CLI

The operator also provides CLI subcommands for manual management:

- **`check`** -- Show vCluster enrollment status across namespaces
- **`enroll <name> <namespace>`** -- Manually enroll a vCluster in ArgoCD
- **`unenroll <name> --confirm`** -- Remove a vCluster from ArgoCD

## Troubleshooting

**Check operator logs:**

```bash
kubectl logs -l app.kubernetes.io/name=vcluster-argocd-enroller -n vcluster-system
```

**Verify RBAC permissions:**

```bash
kubectl auth can-i get secrets \
  --as=system:serviceaccount:vcluster-system:vcluster-argocd-enroller \
  -n vcluster-system

kubectl auth can-i create secrets \
  --as=system:serviceaccount:vcluster-system:vcluster-argocd-enroller \
  -n argocd
```

**Enable debug logging:**

```bash
helm upgrade vcluster-argocd-enroller \
  oci://ghcr.io/andrewrothstein/charts/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --reuse-values \
  --set operator.logLevel=DEBUG
```

## Links

- [BUILDING.md](BUILDING.md) -- Developer guide (building, testing, project structure)
- [Helm Chart README](helm/vcluster-argocd-enroller/README.md) -- Full chart configuration reference
- [Issues](https://github.com/andrewrothstein/vcluster-argocd-enroller/issues) -- Bug reports and feature requests
