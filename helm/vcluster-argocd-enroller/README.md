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
| `replicaCount` | Number of operator replicas | `1` |
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
| `operator.logLevel` | Log level (DEBUG, INFO, WARNING, ERROR) | `INFO` |
| `operator.useEmbeddedCode` | Use embedded operator code | `true` |
| `rbac.create` | Create RBAC resources | `true` |
| `autoscaling.enabled` | Enable HPA | `false` |
| `podDisruptionBudget.enabled` | Enable PDB | `false` |
| `networkPolicy.enabled` | Enable NetworkPolicy | `true` |
| `networkPolicy.healthPort` | Health probe port allowed for ingress | `8080` |
| `networkPolicy.apiServer.cidr` | CIDR allowed for API server egress | `0.0.0.0/0` |
| `networkPolicy.apiServer.port` | API server egress port | `443` |
| `networkPolicy.apiServer.additionalPorts` | Extra API server egress ports | `[6443]` |
| `monitoring.enabled` | Enable Prometheus metrics | `false` |

### Using External Operator Code

If you want to use a custom operator implementation:

1. Set `operator.useEmbeddedCode` to `false`
2. Create a ConfigMap with your operator code
3. Set `operator.externalCodeConfigMap` to your ConfigMap name

Example:

```bash
kubectl create configmap custom-operator-code \
  --from-file=operator.py=./my-custom-operator.py \
  -n vcluster-system

helm install vcluster-argocd-enroller ./helm/vcluster-argocd-enroller \
  --namespace vcluster-system \
  --set operator.useEmbeddedCode=false \
  --set operator.externalCodeConfigMap=custom-operator-code
```

## Uninstallation

```bash
helm uninstall vcluster-argocd-enroller -n vcluster-system
```

## Advanced Configuration

### High Availability

For production deployments, consider:

```yaml
replicaCount: 2

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

### Resource Optimization

For large deployments with many vClusters:

```yaml
resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 200m
    memory: 256Mi

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 5
  targetCPUUtilizationPercentage: 70
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

### Monitoring

Enable Prometheus monitoring:

```yaml
monitoring:
  enabled: true
  serviceMonitor:
    enabled: true
    interval: 30s
    labels:
      prometheus: kube-prometheus
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
