# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/andrewrothstein/vcluster-argocd-enroller/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/andrewrothstein/vcluster-argocd-enroller/releases/tag/v0.1.0
