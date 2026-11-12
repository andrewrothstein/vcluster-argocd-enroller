# vCluster ArgoCD Enroller Helm Chart

This Helm chart deploys the vCluster ArgoCD Enroller operator, which automatically enrolls vCluster instances in ArgoCD.

## Installation

### Prerequisites

- Kubernetes 1.19+
- Helm 3.2.0+
- ArgoCD installed in the cluster
- vCluster instances to be enrolled

### Add Helm Repository (when published)

```bash
helm repo add vcluster-argocd-enroller https://andrewrothstein.github.io/vcluster-argocd-enroller
helm repo update
```

### Install from Local Chart

```bash
helm install vcluster-argocd-enroller ./helm/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --create-namespace
```

### Install with Custom Values

```bash
helm install vcluster-argocd-enroller ./helm/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --create-namespace \
  --set operator.argoCDNamespace=argocd \
  --set operator.logLevel=DEBUG
```

## Configuration

The following table lists the configurable parameters and their default values:

| Parameter | Description | Default |
| --------- | ----------- | ------- |
| `replicaCount` | Number of operator replicas; more than one needs `leaderElection.enabled` | `1` |
| `leaderElection.enabled` | Run one active replica and keep the others on standby (needs the ClusterKopfPeering CRD) | `false` |
| `image.repository` | Image repository | `ghcr.io/andrewrothstein/vcluster-argocd-enroller` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `image.tag` | Image tag (defaults to chart appVersion) | `""` |
| `imagePullSecrets` | Image pull secrets | `[]` |
| `serviceAccount.create` | Create service account | `true` |
| `serviceAccount.annotations` | Service account annotations | `{}` |
| `serviceAccount.name` | Service account name | `""` |
| `podSecurityContext` | Pod security context | See values.yaml |
| `securityContext` | Container security context | See values.yaml |
| `resources.limits.cpu` | CPU limit | `200m` |
| `resources.limits.memory` | Memory limit | `256Mi` |
| `resources.requests.cpu` | CPU request | `100m` |
| `resources.requests.memory` | Memory request | `128Mi` |
| `operator.vclusterNamespace` | Namespace where the operator is deployed (vClusters are watched cluster-wide) | `vcluster-system` |
| `operator.argoCDNamespace` | ArgoCD namespace | `argocd` |
| `operator.serverURL` | Address ArgoCD reaches each vCluster at; `{name}` and `{namespace}` are replaced | `https://{name}.{namespace}` |
| `operator.resyncInterval` | Seconds between re-syncs of each vCluster (credential rotation, drift) | `300` |
| `operator.logLevel` | Log level (DEBUG, INFO, WARNING, ERROR) | `INFO` |
| `rbac.create` | Create RBAC resources | `true` |
| `autoscaling.enabled` | Enable HPA (needs `leaderElection.enabled`; extra replicas are standbys) | `false` |
| `podDisruptionBudget.enabled` | Enable PDB | `false` |
| `networkPolicy.enabled` | Enable NetworkPolicy | `true` |
| `networkPolicy.healthPort` | Port the operator serves health probes on, allowed for ingress | `8080` |
| `networkPolicy.apiServer.cidr` | CIDR allowed for API server egress | `0.0.0.0/0` |
| `networkPolicy.apiServer.port` | API server egress port | `443` |
| `networkPolicy.apiServer.additionalPorts` | Extra API server egress ports | `[6443]` |

### Per-vCluster Annotations

Some settings belong to a vCluster rather than to the operator. They are annotations on the vCluster's StatefulSet
or Deployment (`controlPlane.statefulSet.annotations` in the vcluster chart):

| Annotation | Effect |
| ---------- | ------ |
| `vcluster-argocd-enroller/labels` | Labels for the ArgoCD cluster secret, as `key=value,key=value` |
| `vcluster-argocd-enroller/project` | The ArgoCD project the cluster is scoped to |
| `vcluster-argocd-enroller/enroll` | `"false"` keeps the vCluster out of ArgoCD |

See the [project README](https://github.com/andrewrothstein/vcluster-argocd-enroller#per-vcluster-settings) for
the details.

## Uninstallation

```bash
helm uninstall vcluster-argocd-enroller -n vcluster-system
```

## Advanced Configuration

### High Availability

The operator does its work in one replica. With leader election the others are standbys: one takes over as soon as
the active replica shuts down, or within about a minute of it disappearing without notice. Extra replicas add
availability, not throughput, and without `leaderElection.enabled` the chart refuses to render more than one.

```yaml
replicaCount: 2

leaderElection:
  enabled: true

podDisruptionBudget:
  enabled: true
  minAvailable: 1

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchExpressions:
          - key: app.kubernetes.io/name
            operator: In
            values:
            - vcluster-argocd-enroller
        topologyKey: kubernetes.io/hostname
```

Leader election uses a [kopf peering](https://kopf.readthedocs.io/en/stable/peering/) object, a
`ClusterKopfPeering` named after the release, whose CRD ships in this chart's `crds/` directory:

- `helm install` installs the CRD. `helm upgrade` does not, so when enabling leader election on a release that was
  installed from a chart older than 2.0.0, apply it first:
  `kubectl apply -f helm/vcluster-argocd-enroller/crds/clusterkopfpeering.yaml`
- ArgoCD installs the CRD along with the chart; if the first sync complains that `ClusterKopfPeering` is unknown, add
  the `SkipDryRunOnMissingResource=true` sync option.

### Resource Optimization

For large deployments with many vClusters, give the active replica more room:

```yaml
resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 200m
    memory: 256Mi
```

### Network Policies

The NetworkPolicy is enabled by default. It allows DNS, egress to the Kubernetes API server, and
ingress on the health port for kubelet probes. The API server is not a pod on k3s, k3d, RKE2 and
most managed clusters, so it is allowed by `ipBlock` and port rather than by pod selector. The
default CIDR `0.0.0.0/0` works everywhere; tighten it to your API endpoint where you know it:

```yaml
networkPolicy:
  enabled: true
  apiServer:
    cidr: 10.0.0.5/32   # your API server address
    port: 443
    additionalPorts: [6443]  # some distributions NAT the ClusterIP to 6443
  egress: []  # additional egress rules if needed
```

## Troubleshooting

### Check Operator Logs

```bash
kubectl logs -l app.kubernetes.io/name=vcluster-argocd-enroller -n vcluster-system
```

### Verify RBAC Permissions

```bash
kubectl auth can-i get secrets --as=system:serviceaccount:vcluster-system:vcluster-argocd-enroller -n vcluster-system
kubectl auth can-i create secrets --as=system:serviceaccount:vcluster-system:vcluster-argocd-enroller -n argocd
```

### Verify an Enrollment

Connect to a vCluster with the credentials ArgoCD was given, from inside the cluster:

```bash
kubectl run verify-enrollment --rm -i --restart=Never -n vcluster-system \
  --image=ghcr.io/andrewrothstein/vcluster-argocd-enroller:<version> \
  --overrides='{"spec":{"serviceAccountName":"vcluster-argocd-enroller"}}' \
  --command -- vcluster-argocd-enroller verify <vcluster-name> <vcluster-namespace>
```

### Debug Mode

Enable debug logging:

```bash
helm upgrade vcluster-argocd-enroller ./helm/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --reuse-values \
  --set operator.logLevel=DEBUG
```

## Support

For issues and feature requests, please visit: https://github.com/andrewrothstein/vcluster-argocd-enroller/issues
