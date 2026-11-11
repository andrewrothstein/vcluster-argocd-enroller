"""
Tests for the vCluster ArgoCD Enroller CLI.
"""

import base64
import logging
from unittest.mock import Mock, patch

import pytest
from kubernetes.client.rest import ApiException

from vcluster_argocd_enroller import cli

pytestmark = pytest.mark.cli


@pytest.fixture
def kopf_mocked():
    """Stop `run` from starting a real operator, and undo its root logger changes."""
    root = logging.getLogger()
    level = root.level
    with patch("kopf.run") as mock_run, patch("kopf.configure") as mock_configure:
        yield mock_run, mock_configure
    root.setLevel(level)


@pytest.fixture
def core_v1(monkeypatch):
    """Mock the CoreV1Api the CLI builds, with ArgoCD living in a non-default namespace."""
    monkeypatch.setattr("vcluster_argocd_enroller.operator.ARGOCD_NAMESPACE", "gitops")
    mock_core = Mock()
    with (
        patch("kubernetes.config.load_incluster_config"),
        patch("kubernetes.client.CoreV1Api", return_value=mock_core),
    ):
        yield mock_core


def vcluster_secret():
    """Create a mock vCluster secret with kubeconfig data."""
    return Mock(
        data={
            "certificate-authority": base64.b64encode(b"fake-ca-data").decode("utf-8"),
            "client-certificate": base64.b64encode(b"fake-cert-data").decode("utf-8"),
            "client-key": base64.b64encode(b"fake-key-data").decode("utf-8"),
        }
    )


class TestRun:
    """Test how `run` drives kopf."""

    def test_defaults_watch_all_namespaces(self, kopf_mocked):
        mock_run, mock_configure = kopf_mocked

        cli.run()

        mock_configure.assert_called_once_with(verbose=False, quiet=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs["clusterwide"] is True
        assert kwargs["namespaces"] == []
        assert kwargs["liveness_endpoint"] == "http://0.0.0.0:8080/healthz"
        assert kwargs["standalone"] is None
        assert kwargs["priority"] is None
        assert kwargs["peering_name"] is None

    def test_namespace_disables_clusterwide(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(namespace="team-a")

        kwargs = mock_run.call_args.kwargs
        assert kwargs["clusterwide"] is False
        assert kwargs["namespaces"] == ["team-a"]

    def test_dev_takes_priority(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(dev=True, standalone=True)

        kwargs = mock_run.call_args.kwargs
        assert kwargs["priority"] == 666
        assert kwargs["standalone"] is True

    @pytest.mark.parametrize(
        "options,configure_kwargs,level",
        [
            ({"verbose": True}, {"verbose": True, "quiet": False}, logging.DEBUG),
            ({"log_level": "DEBUG"}, {"verbose": True, "quiet": False}, logging.DEBUG),
            ({"log_level": "WARNING"}, {"verbose": False, "quiet": True}, logging.WARNING),
            ({"log_level": "ERROR"}, {"verbose": False, "quiet": True}, logging.ERROR),
        ],
    )
    def test_log_level_is_applied(self, kopf_mocked, options, configure_kwargs, level):
        _, mock_configure = kopf_mocked

        cli.run(**options)

        mock_configure.assert_called_once_with(**configure_kwargs)
        assert logging.getLogger().level == level

    def test_log_level_read_from_env(self, monkeypatch):
        """The chart sets LOG_LEVEL from operator.logLevel."""
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        _, bound, _ = cli.app.parse_args([])

        assert bound.arguments["log_level"] == "DEBUG"


class TestEnroll:
    """Test manual enrollment."""

    def test_creates_secret_in_configured_argocd_namespace(self, core_v1):
        core_v1.read_namespaced_secret.side_effect = [ApiException(status=404), vcluster_secret()]

        cli.enroll("my-cluster", "team-a")

        kwargs = core_v1.create_namespaced_secret.call_args.kwargs
        assert kwargs["namespace"] == "gitops"
        assert kwargs["body"]["metadata"] == {
            "name": "vcluster-my-cluster",
            "namespace": "gitops",
            "labels": {"argocd.argoproj.io/secret-type": "cluster", "vcluster-operator": "true"},
        }
        server = base64.b64decode(kwargs["body"]["data"]["server"]).decode("utf-8")
        assert server == "https://my-cluster.team-a.svc.cluster.local"

    def test_already_enrolled_is_left_alone(self, core_v1):
        cli.enroll("my-cluster", "team-a")

        core_v1.read_namespaced_secret.assert_called_once_with(name="vcluster-my-cluster", namespace="gitops")
        core_v1.create_namespaced_secret.assert_not_called()

    def test_failed_enrollment_check_is_not_treated_as_missing(self, core_v1):
        core_v1.read_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a")

        assert exc.value.code == 1
        core_v1.create_namespaced_secret.assert_not_called()

    def test_force_replaces_existing_secret(self, core_v1):
        core_v1.read_namespaced_secret.return_value = vcluster_secret()
        core_v1.create_namespaced_secret.side_effect = ApiException(status=409, reason="Conflict")

        cli.enroll("my-cluster", "team-a", force=True)

        kwargs = core_v1.replace_namespaced_secret.call_args.kwargs
        assert kwargs["name"] == "vcluster-my-cluster"
        assert kwargs["namespace"] == "gitops"
        core_v1.delete_namespaced_secret.assert_not_called()

    def test_malformed_vcluster_secret_exits(self, core_v1):
        core_v1.read_namespaced_secret.side_effect = [ApiException(status=404), Mock(data={})]

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a")

        assert exc.value.code == 1
        core_v1.create_namespaced_secret.assert_not_called()


class TestUnenroll:
    """Test manual removal."""

    def test_deletes_secret_in_configured_argocd_namespace(self, core_v1):
        cli.unenroll("my-cluster", confirm=True)

        core_v1.delete_namespaced_secret.assert_called_once_with(name="vcluster-my-cluster", namespace="gitops")

    def test_missing_secret_is_not_an_error(self, core_v1):
        core_v1.delete_namespaced_secret.side_effect = ApiException(status=404)

        cli.unenroll("my-cluster", confirm=True)
