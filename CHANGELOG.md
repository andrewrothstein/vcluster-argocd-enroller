# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.3.0] - 2026-11-22

### Added
- `operator.allowedLabels` and `operator.allowedProjects` (`ALLOWED_LABELS`, `ALLOWED_PROJECTS`) limit the label
  keys and the projects a vCluster's workload may ask for with its annotations; `*` matches anything. Empty, the
  default, allows everything as before. What is not allowed is ignored with a warning, and the vCluster is
  enrolled without it
- Enrollments whose vCluster is gone are removed: the operator looks for them at startup and every
  `operator.resyncInterval`. That covers a vCluster deleted while the operator was down, one whose workload
  lost its `app=vcluster` label, and one whose deletion the operator gave up on. An enrollment goes the second
  time in a row its vCluster is not found, never when the lookup fails, and with `--namespace` only for
  vClusters of that namespace. Enrollments made with `enroll` are included. `operator.sweepOrphans: false`
  (`SWEEP_ORPHANS`) turns it off

### Changed
- ArgoCD cluster secrets are written with server-side apply, as field manager `vcluster-argocd-enroller`. The
  operator applies a secret's data and labels and nothing else, so finalizers and owner references on it are no
  longer wiped, a label added while the operator was writing is no longer lost, and a write on top of a secret
  that changed in between is retried. The `vcluster-argocd-enroller/copied-labels` annotation is gone: the API
  server keeps track of which labels the operator set
- Secrets written by 2.2 and earlier are taken over on the first sync, in place and without a moment in which a
  label or the secret is missing. After a downgrade to 2.2, labels and a project dropped since stay on the
  secret until the operator is upgraded again
- A label key a vCluster asks for belongs to the operator from then on: a label of that key set by hand is
  overwritten, and removed when the vCluster stops asking for it
- The chart's Role in the ArgoCD namespace no longer grants `update` on secrets

### Fixed
- `--namespace` together with `--peering-name` is refused at startup. The operator used to start and then idle:
  kopf waited for a namespaced peering object, and the chart installs the cluster-wide one
- `--no-clusterwide` without `--namespace` is refused at startup. It used to watch every namespace anyway

## [2.2.1] - 2026-11-17

### Fixed
- `enroll --force` and `unenroll` no longer overwrite or delete an ArgoCD secret that is not the enrollment of
  the vCluster named (two vClusters can map to one secret name, `a-b`/`c` and `a`/`b-c` for example). Both now go
  through the operator's own sync and removal, so `enroll --force` also keeps the labels and annotations something
  else put on the secret, and `unenroll` also removes the secret 1.x wrote
- `check` shows `N` only for a secret that is missing: a failed lookup shows as `? (403 Forbidden)`, and an ArgoCD
  secret of the right name that belongs to something else as `N (name taken)` rather than `Y`
- An empty `SERVER_URL`, `RESYNC_INTERVAL` or `ARGOCD_NAMESPACE` (an emptied `operator.serverURL` or
  `operator.resyncInterval` chart value) falls back to the default. The operator used to crash at start on an
  empty `RESYNC_INTERVAL`, and to enroll every vCluster with an empty server address on an empty `SERVER_URL`
- A Kubernetes client that cannot be configured is retried rather than raised as a permanent error, which would
  have ended the vCluster's re-sync timer and has no place in the delete handler
- The CLI subcommands exit with a message, not a traceback, when there is no cluster configuration to use

## [2.2.0] - 2026-11-12

### Added
- Per-vCluster settings, as annotations on the vCluster's StatefulSet or Deployment:
  - `vcluster-argocd-enroller/labels: key=value,key=value` puts labels on the ArgoCD cluster secret, for
    ApplicationSet cluster generators to select on and template with
  - `vcluster-argocd-enroller/project` sets the ArgoCD project the cluster is scoped to
  - `vcluster-argocd-enroller/enroll: "false"` keeps the vCluster out of ArgoCD, and removes the enrollment the
    operator made for it. `check` marks such a vCluster `(opted out)`
- `enroll --label key=value --project name`
- `examples/fleet`: a vCluster fleet created, enrolled and bootstrapped with ArgoCD ApplicationSets, run against
  a real ArgoCD by `task k3d-fleet` and in CI

### Changed
- Labels and annotations that something else put on an ArgoCD cluster secret (ArgoCD's own
  `auto-label-cluster-info` labels, for example) are kept when the operator replaces the secret, and no longer
  count as a difference to repair

## [2.1.0] - 2026-11-12

### Added
- vClusters whose control plane is a Deployment (external database, deployed or external etcd, no
  persistence) are enrolled like StatefulSet ones. The chart's ClusterRole now covers `deployments`
- A vCluster that changes from a StatefulSet to a Deployment, or back, keeps its enrollment
- `check` lists Deployment-based vClusters

### Fixed
- Only `apps` StatefulSets are watched: resources of the same name in another API group (such as
  OpenKruise's) are no longer picked up

## [2.0.0] - 2026-11-11

### Changed (breaking)
- ArgoCD cluster secrets are named `vcluster-{namespace}-{name}` and the ArgoCD cluster name is
  `{namespace}-{name}`, so same-named vClusters in different namespaces no longer overwrite each other.
  Secrets written by 1.x are migrated automatically
- The enrolled cluster address is `https://{name}.{namespace}`, which the certificate a vCluster serves is
  valid for (`operator.serverURL` restores the 1.x `.svc.cluster.local` address)
- The chart refuses `replicaCount > 1` and autoscaling without `leaderElection.enabled`
- `unenroll` takes the namespace as its second argument; `--standalone` is removed; the unused
  `VCLUSTER_NAMESPACE` environment variable is no longer set

### Added
- Periodic re-sync (`operator.resyncInterval`): rotated vCluster credentials are picked up, and changed or
  removed ArgoCD secrets are restored
- Leader election between replicas through kopf peering (`leaderElection.enabled`)
- `verify` command, checking the enrolled credentials against the vCluster's API server
- CI enrolls a real vcluster in k3d and verifies the connection

### Fixed
- A vCluster secret that is missing its credentials, and a failed cleanup on deletion, are retried instead of
  given up on
- Secrets that do not belong to the operator are never overwritten or deleted
- The health port is configurable end to end (`networkPolicy.healthPort`)

## [1.x]

### Added
- Initial implementation of vCluster ArgoCD Enroller operator
- Automatic detection and enrollment of vCluster instances in ArgoCD
- Docker container image with multi-architecture support (amd64, arm64)
- Helm chart for production deployment with extensive customization options
- GitHub Actions workflows for CI/CD pipeline
- Automated release process with container and Helm chart publishing to ghcr.io
- Python CLI with multiple commands (watch, enroll, remove, list, verify)
- Comprehensive test suite with unit and integration tests
- Pre-commit hooks for code quality
- Task automation with Taskfile
- Support for high availability deployments
- Network policies and security configurations
- Prometheus metrics and monitoring support
- Pod disruption budgets and horizontal pod autoscaling

### Security
- Non-root container execution
- Read-only root filesystem
- Dropped all capabilities
- Security context configurations

### Documentation
- Comprehensive README with installation and usage instructions
- Helm chart documentation with configuration options
- Inline code documentation

## [0.1.0] - TBD

Initial release (placeholder for first tagged version)

[Unreleased]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.3.0...HEAD
[2.3.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.2.1...v2.3.0
[2.2.1]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v1.2.2...v2.0.0
[0.1.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/releases/tag/v0.1.0
