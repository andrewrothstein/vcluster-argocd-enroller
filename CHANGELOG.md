# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v1.2.2...v2.0.0
[0.1.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/releases/tag/v0.1.0
