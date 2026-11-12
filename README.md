# vCluster ArgoCD Enroller

[![CI](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml/badge.svg)](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml)
[![License](https://img.shields.io/github/license/andrewrothstein/vcluster-argocd-enroller)](https://github.com/andrewrothstein/vcluster-argocd-enroller/blob/main/LICENSE)

A Kubernetes operator that automatically enrolls [vCluster](https://www.vcluster.com/) instances in [ArgoCD](https://argo-cd.readthedocs.io/) for GitOps management.

## How It Works

The operator watches for vCluster StatefulSets (labeled `app=vcluster`) across your cluster. For each one it:

1. Reads the TLS credentials from the vCluster secret (`vc-{name}`)
2. Creates an ArgoCD cluster secret `vcluster-{namespace}-{name}` in the ArgoCD namespace, which registers the
   vCluster in ArgoCD as cluster `{namespace}-{name}` at `https://{name}.{namespace}`
3. Re-syncs every few minutes: rotated vCluster credentials are picked up, and a changed or removed ArgoCD secret
   is restored
4. Removes the ArgoCD secret when the vCluster is deleted

This lets ArgoCD immediately discover and deploy to new vClusters without manual cluster registration. An
Application targets a vCluster with `destination.name: {namespace}-{name}`.

The namespace is part of every name, so vClusters that share a name in different namespaces are enrolled side by
side. The operator only ever replaces or deletes secrets it wrote for that same vCluster (it labels them
`vcluster-argocd-enroller/namespace` and `vcluster-argocd-enroller/name`); a secret of the same name that belongs to
something else is left alone and reported in the logs.

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
| `operator.vclusterNamespace` | Namespace where the operator is deployed (vClusters are watched cluster-wide) | `vcluster-system` |
| `operator.serverURL` | Address ArgoCD reaches each vCluster at (`{name}` and `{namespace}` are replaced) | `https://{name}.{namespace}` |
| `operator.resyncInterval` | Seconds between re-syncs of each vCluster | `300` |
| `operator.logLevel` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `replicaCount` | Operator replicas; more than one needs `leaderElection.enabled` | `1` |
| `leaderElection.enabled` | Run one active replica and keep the others on standby | `false` |
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

See the [Helm chart README](helm/vcluster-argocd-enroller/README.md) for the full list of configurable parameters, HA setup, and network policies.

## CLI

The operator also provides CLI subcommands for manual management:

- **`check`** -- Show vCluster enrollment status across namespaces
- **`enroll <name> <namespace>`** -- Manually enroll a vCluster in ArgoCD
- **`unenroll <name> <namespace> --confirm`** -- Remove a vCluster from ArgoCD (a running operator enrolls it again
  on its next re-sync, for as long as the vCluster exists)
- **`verify <name> <namespace>`** -- Connect to the vCluster with the credentials enrolled in ArgoCD. Run it inside
  the cluster, where the vCluster's Service resolves

## Upgrading from 1.x

2.0.0 changes what ArgoCD sees, so review anything that refers to an enrolled vCluster:

| | 1.x | 2.0.0 |
| - | --- | ----- |
| ArgoCD cluster name (`destination.name`) | `{name}` | `{namespace}-{name}` |
| ArgoCD cluster secret | `vcluster-{name}` | `vcluster-{namespace}-{name}` |
| Cluster address (`destination.server`) | `https://{name}.{namespace}.svc.cluster.local` | `https://{name}.{namespace}` |

- The operator migrates by itself: on startup it writes the new secret for every vCluster and removes the 1.x secret
  it had written for it. Applications and ApplicationSets that name a cluster or its address must be updated to match.
- The address changed because the certificate a vCluster serves is not valid for
  `{name}.{namespace}.svc.cluster.local`, so ArgoCD could not verify it. If your vClusters list that name in
  `controlPlane.proxy.extraSANs` and you want to keep the 1.x address, set
  `operator.serverURL: "https://{name}.{namespace}.svc.cluster.local"`.
- `replicaCount` above 1 (and `autoscaling.enabled`) is now refused unless `leaderElection.enabled` is set. Helm does
  not install CRDs on upgrade, so apply `helm/vcluster-argocd-enroller/crds/` first when enabling it on an existing
  release.
- The `--standalone` flag is gone (peering is off unless `--peering-name` is given), and `unenroll` takes the
  namespace as its second argument, like `enroll`.

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
