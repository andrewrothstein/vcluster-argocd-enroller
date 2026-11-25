# vCluster ArgoCD Enroller

[![CI](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml/badge.svg)](https://github.com/andrewrothstein/vcluster-argocd-enroller/actions/workflows/ci.yaml)
[![License](https://img.shields.io/github/license/andrewrothstein/vcluster-argocd-enroller)](https://github.com/andrewrothstein/vcluster-argocd-enroller/blob/main/LICENSE)

A Kubernetes operator that automatically enrolls [vCluster](https://www.vcluster.com/) instances in [ArgoCD](https://argo-cd.readthedocs.io/) for GitOps management.

## How It Works

The operator watches for vCluster control planes across your cluster: the StatefulSets and Deployments labeled
`app=vcluster` (vcluster runs as a Deployment when it keeps no state of its own, for example with an external
database or a separately deployed etcd). For each one it:

1. Reads the TLS credentials from the vCluster secret (`vc-{name}`)
2. Creates an ArgoCD cluster secret `vcluster-{namespace}-{name}` in the ArgoCD namespace, which registers the
   vCluster in ArgoCD as cluster `{namespace}-{name}` at `https://{name}.{namespace}`
3. Re-syncs every few minutes: rotated vCluster credentials are picked up, and a changed or removed ArgoCD secret
   is restored
4. Removes the ArgoCD secret when the vCluster is deleted
5. Removes the ArgoCD secrets of vClusters that went unnoticed: deleted while the operator was down, or no
   longer labelled `app=vcluster`

This lets ArgoCD immediately discover and deploy to new vClusters without manual cluster registration. An
Application targets a vCluster with `destination.name: {namespace}-{name}`.

The namespace is part of every name, so vClusters that share a name in different namespaces are enrolled side by
side. The operator only ever writes to or deletes secrets it wrote for that same vCluster (it labels them
`vcluster-argocd-enroller/namespace` and `vcluster-argocd-enroller/name`); a secret of the same name that belongs to
something else is left alone and reported in the logs.

It writes with [server-side apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/), as field
manager `vcluster-argocd-enroller`, and applies the secret's data and labels only. Labels, annotations and
finalizers that something else puts on the secret (ArgoCD's own cluster labels, for one) are not touched.

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

The operator will immediately begin watching for vClusters and creating the corresponding ArgoCD cluster secrets.

## Configuration

Key Helm values:

| Parameter | Description | Default |
| --------- | ----------- | ------- |
| `operator.argoCDNamespace` | ArgoCD namespace | `argocd` |
| `operator.vclusterNamespace` | Namespace where the operator is deployed (vClusters are watched cluster-wide) | `vcluster-system` |
| `operator.serverURL` | Address ArgoCD reaches each vCluster at (`{name}` and `{namespace}` are replaced) | `https://{name}.{namespace}` |
| `operator.resyncInterval` | Seconds between re-syncs of each vCluster | `300` |
| `operator.allowedLabels` | Label keys vClusters may ask for with the `labels` annotation; `*` matches anything, empty allows all | `[]` |
| `operator.allowedProjects` | Projects vClusters may ask for with the `project` annotation; `*` matches anything, empty allows all | `[]` |
| `operator.sweepOrphans` | Remove enrollments whose vCluster is gone or no longer labelled `app=vcluster` | `true` |
| `metrics.enabled` | Serve Prometheus metrics on `/metrics` | `true` |
| `metrics.port` | Port the metrics are served on | `9090` |
| `metrics.podMonitor.enabled` | Create a PodMonitor for the Prometheus Operator | `false` |
| `metrics.podMonitor.interval` | Scrape interval; empty for Prometheus's default | `""` |
| `metrics.podMonitor.labels` | Labels your Prometheus selects PodMonitors by | `{}` |
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

## Per-vCluster Settings

Annotations on a vCluster's StatefulSet or Deployment shape its enrollment:

| Annotation | Effect |
| ---------- | ------ |
| `vcluster-argocd-enroller/labels` | Labels for the ArgoCD cluster secret, as `key=value,key=value`. ApplicationSet cluster generators can select on them and template with them |
| `vcluster-argocd-enroller/project` | The ArgoCD project the cluster is scoped to: the `project` field of the cluster secret, which makes it a [project-scoped cluster](https://argo-cd.readthedocs.io/en/stable/user-guide/projects/#project-scoped-repositories-and-clusters) |
| `vcluster-argocd-enroller/enroll` | `"false"` keeps the vCluster out of ArgoCD |

Set them where the vCluster is defined, in the vcluster chart's values:

```yaml
controlPlane:
  statefulSet:
    annotations:
      vcluster-argocd-enroller/labels: env=dev,team=payments
      vcluster-argocd-enroller/project: payments
```

or on a running vCluster with `kubectl annotate`. Either way the change reaches ArgoCD right away.

A label key a vCluster asks for is the operator's from then on. If the secret already carried a label of that key,
set by hand or by another tool, its value is overwritten; when the vCluster stops asking for the key the label is
removed, not put back to what it was. The same goes for a label of the operator's that something else changes: the
next re-sync sets it back.

### Limiting what a vCluster can ask for

Whoever can edit a vCluster's workload sets these annotations. If your ApplicationSets select clusters by label,
a tenant who labels their vCluster `env=prod` receives whatever is deployed to `env=prod`. The chart's
`operator.allowedLabels` and `operator.allowedProjects` say which label keys and which projects the annotations
may ask for:

```yaml
operator:
  allowedLabels: [team, "example.com/*"]
  allowedProjects: ["team-*"]
```

`*` matches anything, and an empty list (the default) allows everything. A label or project that is not allowed is
ignored, with a warning in the operator's log; the vCluster is still enrolled. The lists limit label keys, not
values: allow only keys whose value a tenant may choose freely. They do not apply to `enroll --label` and
`--project`, which need write access to the ArgoCD namespace anyway.

- Labels and a project that are no longer asked for are removed again. Labels and annotations that something else
  put on the cluster secret are kept
- A label that is not a valid Kubernetes label, or is one of the operator's own (`argocd.argoproj.io/secret-type`,
  `vcluster-operator`, `vcluster-argocd-enroller/namespace`, `vcluster-argocd-enroller/name`), is skipped and
  reported in the logs; the vCluster is enrolled without it
- Opting out removes the enrollment the operator made, and holds until the annotation is removed or set to
  `"true"`. `check` marks such a vCluster `(opted out)`
- Whoever can edit a vCluster's workload can set these. Treat the labels as the vCluster's own claim about itself
  when an ApplicationSet selects on them

## Metrics

The operator serves Prometheus metrics on `/metrics`, port `metrics.port` (9090). Set
`metrics.podMonitor.enabled` for a PodMonitor if you run the Prometheus Operator, or scrape the pods' `metrics`
port your own way.

| Metric | Meaning |
| --- | --- |
| `vcluster_argocd_enroller_syncs_total{outcome}` | Syncs of a vCluster's enrollment: `created`, `updated`, `unchanged`, `opted_out` or `failed` |
| `vcluster_argocd_enroller_enrollments_removed_total{reason}` | ArgoCD secrets deleted: the vCluster was `deleted`, `opted_out`, or `orphaned` (found gone by the sweep) |
| `vcluster_argocd_enroller_enrollments` | ArgoCD secrets the operator looks after, as of the last sweep |
| `vcluster_argocd_enroller_orphan_suspects` | Secrets whose vCluster the last sweep did not find; the next sweep removes them unless it is back |
| `vcluster_argocd_enroller_last_sweep_timestamp_seconds` | When the sweep last completed |
| `vcluster_argocd_enroller_info{version}` | The operator's version |

Worth alerting on: `rate(vcluster_argocd_enroller_syncs_total{outcome="failed"}[15m]) > 0` for a vCluster that
cannot be enrolled (the log says which and why), and a `last_sweep_timestamp_seconds` that stops advancing.
The last three gauges come from the sweep, so they are absent with `operator.sweepOrphans: false`. With leader
election every replica serves metrics, and only the active one counts syncs: sum over the pods.

## Managing a Fleet

The operator enrolls vClusters; creating them is left to whatever you already deploy with.
[examples/fleet](examples/fleet/) shows the whole loop with ArgoCD itself: one ApplicationSet creates the vClusters
from a list, the operator enrolls each with its labels, and a second ApplicationSet deploys into every vCluster
that shows up.

## CLI

The operator also provides CLI subcommands for manual management:

- **`check`** -- Show vCluster enrollment status across namespaces. `N (name taken)` marks a vCluster whose ArgoCD
  secret name is held by something else, `? (...)` a lookup that failed
- **`enroll <name> <namespace> [--label key=value]... [--project name]`** -- Manually enroll a vCluster in ArgoCD.
  A running operator re-syncs the enrollment from the vCluster's annotations, so set labels and the project there
  when the operator is in charge. `--force` re-writes an existing enrollment
- **`unenroll <name> <namespace> --confirm`** -- Remove a vCluster from ArgoCD (a running operator enrolls it again
  on its next re-sync, for as long as the vCluster exists)

`enroll` and `unenroll` write and delete the way the operator does: neither touches an ArgoCD secret that is not
the enrollment of the vCluster named, `--force` or not. A running operator removes an enrollment made with `enroll`
when it finds no StatefulSet or Deployment labelled `app=vcluster` for it (see `operator.sweepOrphans`).

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
- [examples/fleet](examples/fleet/) -- A vCluster fleet created, enrolled and bootstrapped with ArgoCD ApplicationSets
- [Helm Chart README](helm/vcluster-argocd-enroller/README.md) -- Full chart configuration reference
- [Issues](https://github.com/andrewrothstein/vcluster-argocd-enroller/issues) -- Bug reports and feature requests
