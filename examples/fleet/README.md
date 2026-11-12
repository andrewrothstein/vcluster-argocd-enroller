# A vCluster fleet with ArgoCD ApplicationSets

The enroller registers vClusters in ArgoCD; it does not create them. This example shows how the two halves fit
together so that one list in Git drives a fleet of vClusters, each one bootstrapped by ArgoCD as soon as it is up:

| File | What it does |
| ---- | ------------ |
| [`vclusters.yaml`](vclusters.yaml) | An ApplicationSet with one Application per vCluster. Each installs the vcluster chart into its own namespace of the host cluster |
| [`bootstrap.yaml`](bootstrap.yaml) | An ApplicationSet with one Application per *enrolled* vCluster. Each deploys a workload into its vCluster |

Nothing connects the two but the enroller:

1. `vclusters.yaml` creates a vCluster, annotated with the labels it wants in ArgoCD
2. The enroller sees its StatefulSet and writes the ArgoCD cluster secret `vcluster-{namespace}-{name}`, with
   those labels
3. The cluster generator in `bootstrap.yaml` sees the secret and creates an Application for the new cluster
4. ArgoCD deploys into the vCluster

## Try it

You need ArgoCD (its default install includes the ApplicationSet controller) and the enroller, in the cluster that
will host the vClusters: ArgoCD reaches each vCluster at its in-cluster Service address, `https://{name}.{namespace}`.

```bash
kubectl apply -f examples/fleet/

kubectl -n argocd get applications
kubectl -n argocd get secrets -l vcluster-operator=true -L env
```

A minute or two later there are four Applications: `vcluster-team-a-alpha` and `vcluster-team-b-beta` on the host
cluster, and `bootstrap-team-a-alpha` and `bootstrap-team-b-beta` inside the vClusters.

`task k3d-fleet` runs all of this, teardown included, in the k3d test cluster (see [BUILDING.md](../../BUILDING.md)).

## Adding a vCluster

Add an element to the list in `vclusters.yaml`:

```yaml
- name: gamma
  namespace: team-c
  env: dev
```

- **One vCluster per namespace.** vcluster refuses to start a second one in a namespace that already has one
- ArgoCD knows the vCluster as `{namespace}-{name}`, here `team-c-gamma`. That is `{{.name}}` in the cluster
  generator, and what an Application puts in `destination.name`
- The Helm release name is the vCluster's name. It can differ from the Application's name, which has to be unique
  in the ArgoCD namespace
- The enroller puts a finalizer and an annotation of its own on the vCluster's StatefulSet. ArgoCD does not count
  them as drift: the Application stays `Synced`, with `selfHeal` on

## Selecting and templating with labels

The vcluster chart copies `controlPlane.statefulSet.annotations` onto the vCluster's StatefulSet (or Deployment),
where the enroller reads them:

```yaml
controlPlane:
  statefulSet:
    annotations:
      vcluster-argocd-enroller/labels: 'env={{.env}}'
```

Every `key=value` in that annotation becomes a label on the cluster secret. The cluster generator can select on it
(`matchLabels`, `matchExpressions`) and template with it (`{{index .metadata.labels "env"}}`). Change the annotation
and the labels follow; `bootstrap.yaml` then adds or drops Applications to match.

Two more annotations work the same way: `vcluster-argocd-enroller/project` makes the cluster
[project-scoped](https://argo-cd.readthedocs.io/en/stable/user-guide/projects/#project-scoped-repositories-and-clusters)
(the cluster generator exposes it as `{{.project}}`), and `vcluster-argocd-enroller/enroll: "false"` keeps a
vCluster out of ArgoCD altogether.

## Removing a vCluster

Remove its element from the list. Then, in order:

1. The ApplicationSet deletes the Application, whose `resources-finalizer.argocd.argoproj.io` finalizer makes
   ArgoCD delete the vCluster
2. The enroller removes the cluster secret
3. The cluster generator drops the cluster, and the bootstrap Application with it

The bootstrap Applications deliberately have no resources finalizer. By the time one is deleted its vCluster is
gone, along with everything in it; a finalizer would leave the Application waiting on a cluster ArgoCD can no
longer reach.

Two things stay behind in the host cluster: the namespace that `CreateNamespace=true` made, and in it the
vCluster's data volume (`data-{name}-0`; Kubernetes keeps a StatefulSet's volume claims). A vCluster recreated
under the same name in that namespace is given the old volume. Delete the namespace for a clean slate.
