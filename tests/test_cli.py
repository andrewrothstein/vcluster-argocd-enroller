"""
Tests for the vCluster ArgoCD Enroller CLI.
"""

import base64
import json
import logging
from pathlib import Path
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
        # No peering unless asked for: kopf must not join a peering object it happens to find
        assert kwargs["standalone"] is True
        assert kwargs["peering_name"] is None

    def test_namespace_disables_clusterwide(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(namespace="team-a")

        kwargs = mock_run.call_args.kwargs
        assert kwargs["clusterwide"] is False
        assert kwargs["namespaces"] == ["team-a"]

    def test_peering_name_enables_leader_election(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(peering_name="enroller")

        kwargs = mock_run.call_args.kwargs
        assert kwargs["standalone"] is False
        assert kwargs["peering_name"] == "enroller"

    def test_replicas_draw_different_priorities(self, kopf_mocked):
        """kopf pauses every peer of equal priority, so replicas of one Deployment must not tie."""
        mock_run, _ = kopf_mocked

        for _ in range(20):
            cli.run(peering_name="enroller")

        priorities = [call.kwargs["priority"] for call in mock_run.call_args_list]
        assert len(set(priorities)) == len(priorities)
        assert all(0 <= priority < cli.MAX_PRIORITY for priority in priorities)

    def test_explicit_priority_is_kept(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(peering_name="enroller", priority=7)

        assert mock_run.call_args.kwargs["priority"] == 7

    def test_dev_takes_priority(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(dev=True, peering_name="enroller", priority=7)

        assert mock_run.call_args.kwargs["priority"] == cli.MAX_PRIORITY

    def test_health_port_moves_liveness_endpoint(self, kopf_mocked):
        mock_run, _ = kopf_mocked

        cli.run(health_port=9090)

        assert mock_run.call_args.kwargs["liveness_endpoint"] == "http://0.0.0.0:9090/healthz"

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

    def test_peering_and_health_port_read_from_env(self, monkeypatch):
        """The chart sets PEERING_NAME with leaderElection.enabled, and HEALTH_PORT always."""
        monkeypatch.setenv("PEERING_NAME", "enroller")
        monkeypatch.setenv("HEALTH_PORT", "9090")

        _, bound, _ = cli.app.parse_args([])

        assert bound.arguments["peering_name"] == "enroller"
        assert bound.arguments["health_port"] == 9090


class TestEnroll:
    """Test manual enrollment."""

    def test_creates_secret_in_configured_argocd_namespace(self, core_v1):
        core_v1.read_namespaced_secret.side_effect = [ApiException(status=404), vcluster_secret()]

        cli.enroll("my-cluster", "team-a")

        kwargs = core_v1.create_namespaced_secret.call_args.kwargs
        assert kwargs["namespace"] == "gitops"
        assert kwargs["body"]["metadata"] == {
            "name": "vcluster-team-a-my-cluster",
            "namespace": "gitops",
            "labels": {
                "argocd.argoproj.io/secret-type": "cluster",
                "vcluster-operator": "true",
                "vcluster-argocd-enroller/namespace": "team-a",
                "vcluster-argocd-enroller/name": "my-cluster",
            },
        }
        assert base64.b64decode(kwargs["body"]["data"]["name"]).decode("utf-8") == "team-a-my-cluster"
        server = base64.b64decode(kwargs["body"]["data"]["server"]).decode("utf-8")
        assert server == "https://my-cluster.team-a"

    def test_already_enrolled_is_left_alone(self, core_v1):
        cli.enroll("my-cluster", "team-a")

        core_v1.read_namespaced_secret.assert_called_once_with(name="vcluster-team-a-my-cluster", namespace="gitops")
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
        assert kwargs["name"] == "vcluster-team-a-my-cluster"
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
        cli.unenroll("my-cluster", "team-a", confirm=True)

        core_v1.delete_namespaced_secret.assert_called_once_with(name="vcluster-team-a-my-cluster", namespace="gitops")

    def test_missing_secret_is_not_an_error(self, core_v1):
        core_v1.delete_namespaced_secret.side_effect = ApiException(status=404)

        cli.unenroll("my-cluster", "team-a", confirm=True)


class TestVerify:
    """Test checking enrolled credentials against the vCluster."""

    @pytest.fixture
    def api_client(self):
        """Capture the client configuration `verify` connects with, and what its credential files hold."""
        seen = {}

        def connect(configuration):
            seen["host"] = configuration.host
            seen["files"] = {
                key: Path(getattr(configuration, key)).read_text() for key in ("ssl_ca_cert", "cert_file", "key_file")
            }
            return Mock()

        with (
            patch("kubernetes.client.ApiClient", side_effect=connect),
            patch("kubernetes.client.VersionApi") as version_api,
        ):
            version_api.return_value.get_code.return_value = Mock(git_version="v1.33.0")
            yield seen, version_api.return_value

    def test_connects_with_the_enrolled_credentials(self, core_v1, api_client):
        from vcluster_argocd_enroller.operator import build_argocd_secret

        seen, _ = api_client
        core_v1.read_namespaced_secret.return_value = Mock(
            data=build_argocd_secret("my-cluster", "team-a", vcluster_secret())["data"]
        )

        cli.verify("my-cluster", "team-a")

        core_v1.read_namespaced_secret.assert_called_once_with(name="vcluster-team-a-my-cluster", namespace="gitops")
        assert seen["host"] == "https://my-cluster.team-a"
        assert seen["files"] == {
            "ssl_ca_cert": "fake-ca-data",
            "cert_file": "fake-cert-data",
            "key_file": "fake-key-data",
        }

    def test_unreachable_vcluster_exits(self, core_v1, api_client):
        from vcluster_argocd_enroller.operator import build_argocd_secret

        _, version_api = api_client
        core_v1.read_namespaced_secret.return_value = Mock(
            data=build_argocd_secret("my-cluster", "team-a", vcluster_secret())["data"]
        )
        version_api.get_code.side_effect = ConnectionError("certificate verify failed")

        with pytest.raises(SystemExit) as exc:
            cli.verify("my-cluster", "team-a")

        assert exc.value.code == 1

    def test_not_enrolled_exits(self, core_v1, api_client):
        _, version_api = api_client
        core_v1.read_namespaced_secret.side_effect = ApiException(status=404, reason="Not Found")

        with pytest.raises(SystemExit) as exc:
            cli.verify("my-cluster", "team-a")

        assert exc.value.code == 1
        version_api.get_code.assert_not_called()

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"server": "aHR0cHM6Ly94", "config": base64.b64encode(b"not json").decode()},
            {
                "server": "aHR0cHM6Ly94",
                "config": base64.b64encode(json.dumps({"tlsClientConfig": {}}).encode()).decode(),
            },
        ],
        ids=["empty", "config-not-json", "no-credentials"],
    )
    def test_malformed_argocd_secret_exits(self, core_v1, api_client, data):
        _, version_api = api_client
        core_v1.read_namespaced_secret.return_value = Mock(data=data)

        with pytest.raises(SystemExit) as exc:
            cli.verify("my-cluster", "team-a")

        assert exc.value.code == 1
        version_api.get_code.assert_not_called()
