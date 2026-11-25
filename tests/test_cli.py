"""
Tests for the vCluster ArgoCD Enroller CLI.
"""

import base64
import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from kubernetes import client
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


def enrolled_secret(name, namespace, labels=None, annotations=None):
    """Create the ArgoCD secret of a vCluster as the API returns it, with any extra labels and annotations."""
    from vcluster_argocd_enroller.operator import build_argocd_secret

    body = build_argocd_secret(name, namespace, vcluster_secret())
    metadata = client.V1ObjectMeta(labels={**body["metadata"]["labels"], **(labels or {})}, annotations=annotations)
    return client.V1Secret(metadata=metadata, data=body["data"])


def serve_secrets(mock_core, secrets):
    """Make read_namespaced_secret answer from a {(namespace, name): secret} mapping, and 404 otherwise."""

    def read(name, namespace):
        try:
            return secrets[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found") from None

    mock_core.read_namespaced_secret.side_effect = read


def applied(mock_core):
    """Return the (name, namespace, body) of the one server-side apply made."""
    (call,) = mock_core.patch_namespaced_secret.call_args_list
    assert call.kwargs == {
        "field_manager": "vcluster-argocd-enroller",
        "force": True,
        "_content_type": "application/apply-patch+yaml",
    }
    return call.args


def vcluster_workload(name, namespace, replicas, ready, annotations=None):
    """Create a mock vCluster StatefulSet or Deployment, as far as `check` looks at it."""
    workload = Mock(metadata=client.V1ObjectMeta(name=name, namespace=namespace, annotations=annotations))
    # Not a constructor argument: Mock(spec=...) means something else
    workload.spec = Mock(replicas=replicas)
    workload.status = Mock(ready_replicas=ready)
    return workload


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

    def test_metrics_are_served_only_when_given_a_port(self, kopf_mocked):
        with patch("prometheus_client.start_http_server") as start:
            cli.run()
            start.assert_not_called()

            cli.run(metrics_port=9090)
            start.assert_called_once_with(9090)

    @pytest.mark.parametrize("namespace,swept", [(None, ()), ("team-a", ("team-a",))])
    def test_orphan_sweep_keeps_to_the_namespace_watched(self, kopf_mocked, monkeypatch, namespace, swept):
        from vcluster_argocd_enroller import operator

        monkeypatch.setattr(operator, "WATCHED_NAMESPACES", ("stale",))

        cli.run(namespace=namespace)

        assert operator.WATCHED_NAMESPACES == swept

    @pytest.mark.parametrize(
        "options",
        [{"namespace": "team-a", "peering_name": "enroller"}, {"clusterwide": False}],
        ids=["namespace-with-peering", "no-clusterwide-alone"],
    )
    def test_combinations_kopf_would_not_honour_are_refused(self, kopf_mocked, options):
        """kopf idles without a namespaced peering object, and watches everything when told neither scope."""
        mock_run, _ = kopf_mocked

        with pytest.raises(SystemExit) as exc:
            cli.run(**options)

        assert exc.value.code == 1
        mock_run.assert_not_called()

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


class TestClientConfiguration:
    """Test how the commands find the cluster."""

    def test_falls_back_to_the_kubeconfig_outside_a_cluster(self, core_v1):
        from kubernetes import config

        with (
            patch("kubernetes.config.load_incluster_config", side_effect=config.ConfigException("not in a pod")),
            patch("kubernetes.config.load_kube_config") as load_kube_config,
        ):
            cli.unenroll("my-cluster", "team-a", confirm=True)

        load_kube_config.assert_called_once_with()

    def test_no_configuration_exits(self, core_v1, capsys):
        from kubernetes import config

        with (
            patch("kubernetes.config.load_incluster_config", side_effect=config.ConfigException("not in a pod")),
            patch("kubernetes.config.load_kube_config", side_effect=config.ConfigException("no kubeconfig")),
            pytest.raises(SystemExit) as exc,
        ):
            cli.unenroll("my-cluster", "team-a", confirm=True)

        assert exc.value.code == 1
        assert "no kubeconfig" in capsys.readouterr().out
        core_v1.read_namespaced_secret.assert_not_called()


class TestCheck:
    """Test the enrollment overview."""

    @pytest.fixture
    def apps_v1(self):
        """Serve one vCluster running as a StatefulSet and one running as a Deployment."""
        mock_apps = Mock()
        statefulset = vcluster_workload("stateful", "team-a", replicas=1, ready=1)
        deployment = vcluster_workload("stateless", "team-b", replicas=2, ready=None)
        for listing in (mock_apps.list_stateful_set_for_all_namespaces, mock_apps.list_namespaced_stateful_set):
            listing.return_value = Mock(items=[statefulset])
        for listing in (mock_apps.list_deployment_for_all_namespaces, mock_apps.list_namespaced_deployment):
            listing.return_value = Mock(items=[deployment])
        with patch("kubernetes.client.AppsV1Api", return_value=mock_apps):
            yield mock_apps

    def test_lists_statefulset_and_deployment_vclusters(self, core_v1, apps_v1, capsys):
        serve_secrets(
            core_v1,
            {
                ("team-a", "vc-stateful"): vcluster_secret(),
                ("gitops", "vcluster-team-a-stateful"): enrolled_secret("stateful", "team-a"),
            },
        )

        cli.check()

        apps_v1.list_stateful_set_for_all_namespaces.assert_called_once_with(label_selector="app=vcluster")
        apps_v1.list_deployment_for_all_namespaces.assert_called_once_with(label_selector="app=vcluster")
        core_v1.read_namespaced_secret.assert_any_call(name="vcluster-team-a-stateful", namespace="gitops")
        core_v1.read_namespaced_secret.assert_any_call(name="vcluster-team-b-stateless", namespace="gitops")
        output = capsys.readouterr().out
        rows = {line.split()[3]: line.split()[5:] for line in output.splitlines() if "team-" in line}
        assert rows["stateful"] == ["1/1", "│", "Y", "│", "Y", "│"]
        assert rows["stateless"] == ["0/2", "│", "N", "│", "N", "│"]

    def test_secret_of_something_else_is_not_an_enrollment(self, core_v1, apps_v1, capsys):
        """Two vClusters can map to one secret name: team-a/stateful and team/a-stateful."""
        serve_secrets(core_v1, {("gitops", "vcluster-team-a-stateful"): enrolled_secret("a-stateful", "team")})

        cli.check()

        assert capsys.readouterr().out.count("N (name taken)") == 1

    def test_failed_lookup_is_not_reported_as_missing(self, core_v1, apps_v1, capsys):
        core_v1.read_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")

        cli.check()

        # Both secrets of both vClusters
        assert capsys.readouterr().out.count("? (403 Forbidden)") == 4

    def test_opted_out_vcluster_is_marked(self, core_v1, apps_v1, capsys):
        opted_out = vcluster_workload(
            "private", "team-c", replicas=1, ready=1, annotations={"vcluster-argocd-enroller/enroll": "false"}
        )
        apps_v1.list_stateful_set_for_all_namespaces.return_value.items.append(opted_out)

        cli.check()

        assert capsys.readouterr().out.count("opted out") == 1

    def test_namespace_limits_both_listings(self, core_v1, apps_v1):
        cli.check(namespace="team-b")

        apps_v1.list_namespaced_stateful_set.assert_called_once_with(namespace="team-b", label_selector="app=vcluster")
        apps_v1.list_namespaced_deployment.assert_called_once_with(namespace="team-b", label_selector="app=vcluster")
        apps_v1.list_deployment_for_all_namespaces.assert_not_called()


class TestEnroll:
    """Test manual enrollment."""

    def test_creates_secret_in_configured_argocd_namespace(self, core_v1):
        serve_secrets(core_v1, {("team-a", "vc-my-cluster"): vcluster_secret()})

        cli.enroll("my-cluster", "team-a")

        _, namespace, body = applied(core_v1)
        assert namespace == "gitops"
        assert body["metadata"] == {
            "name": "vcluster-team-a-my-cluster",
            "namespace": "gitops",
            "labels": {
                "argocd.argoproj.io/secret-type": "cluster",
                "vcluster-operator": "true",
                "vcluster-argocd-enroller/namespace": "team-a",
                "vcluster-argocd-enroller/name": "my-cluster",
            },
        }
        assert base64.b64decode(body["data"]["name"]).decode("utf-8") == "team-a-my-cluster"
        server = base64.b64decode(body["data"]["server"]).decode("utf-8")
        assert server == "https://my-cluster.team-a"

    def test_labels_and_project_are_set(self, core_v1):
        serve_secrets(core_v1, {("team-a", "vc-my-cluster"): vcluster_secret()})

        cli.enroll("my-cluster", "team-a", label=["env=dev", "example.com/team=payments"], project="payments")

        _, _, body = applied(core_v1)
        assert body["metadata"]["labels"]["env"] == "dev"
        assert body["metadata"]["labels"]["example.com/team"] == "payments"
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "payments"

    @pytest.mark.parametrize("label", ["env", "vcluster-operator=false"])
    def test_invalid_label_exits_before_touching_the_cluster(self, core_v1, label):
        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a", label=[label])

        assert exc.value.code == 1
        core_v1.read_namespaced_secret.assert_not_called()
        core_v1.patch_namespaced_secret.assert_not_called()

    def test_already_enrolled_is_left_alone(self, core_v1):
        serve_secrets(core_v1, {("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("my-cluster", "team-a")})

        cli.enroll("my-cluster", "team-a")

        core_v1.read_namespaced_secret.assert_called_once_with(name="vcluster-team-a-my-cluster", namespace="gitops")
        core_v1.patch_namespaced_secret.assert_not_called()

    def test_failed_enrollment_check_is_not_treated_as_missing(self, core_v1):
        core_v1.read_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a")

        assert exc.value.code == 1
        core_v1.patch_namespaced_secret.assert_not_called()

    def test_force_replaces_existing_secret(self, core_v1):
        """As the operator does it, with an apply of what is asked for now: the API server keeps what something
        else put on the secret and drops the labels no longer applied."""
        existing = enrolled_secret("my-cluster", "team-a", labels={"env": "dev", "added-by": "someone"})
        existing.metadata.resource_version = "7"
        serve_secrets(
            core_v1,
            {
                ("team-a", "vc-my-cluster"): vcluster_secret(),
                ("gitops", "vcluster-team-a-my-cluster"): existing,
            },
        )

        cli.enroll("my-cluster", "team-a", force=True, label=["tier=gold"])

        name, namespace, body = applied(core_v1)
        assert (name, namespace) == ("vcluster-team-a-my-cluster", "gitops")
        assert body["metadata"]["labels"]["tier"] == "gold"
        assert not {"env", "added-by"} & body["metadata"]["labels"].keys()
        assert body["metadata"]["resourceVersion"] == "7"
        core_v1.delete_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize("force", [False, True])
    def test_secret_of_something_else_is_never_overwritten(self, core_v1, capsys, force):
        """Two vClusters can map to one secret name: team-a/my-cluster and team/a-my-cluster."""
        serve_secrets(
            core_v1,
            {
                ("team-a", "vc-my-cluster"): vcluster_secret(),
                ("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("a-my-cluster", "team"),
            },
        )

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a", force=force)

        assert exc.value.code == 1
        assert "not overwriting it" in capsys.readouterr().out
        core_v1.patch_namespaced_secret.assert_not_called()

    def test_failed_write_exits(self, core_v1):
        serve_secrets(core_v1, {("team-a", "vc-my-cluster"): vcluster_secret()})
        core_v1.patch_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a")

        assert exc.value.code == 1

    def test_malformed_vcluster_secret_exits(self, core_v1):
        serve_secrets(core_v1, {("team-a", "vc-my-cluster"): Mock(data={})})

        with pytest.raises(SystemExit) as exc:
            cli.enroll("my-cluster", "team-a")

        assert exc.value.code == 1
        core_v1.patch_namespaced_secret.assert_not_called()


class TestUnenroll:
    """Test manual removal."""

    def test_deletes_secret_in_configured_argocd_namespace(self, core_v1):
        serve_secrets(core_v1, {("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("my-cluster", "team-a")})

        cli.unenroll("my-cluster", "team-a", confirm=True)

        core_v1.delete_namespaced_secret.assert_called_once_with(name="vcluster-team-a-my-cluster", namespace="gitops")

    def test_secret_written_by_1x_is_deleted_too(self, core_v1):
        legacy = enrolled_secret("my-cluster", "team-a")
        legacy.metadata.labels = {"argocd.argoproj.io/secret-type": "cluster", "vcluster-operator": "true"}
        legacy.data["server"] = base64.b64encode(b"https://my-cluster.team-a.svc.cluster.local").decode("utf-8")
        serve_secrets(
            core_v1,
            {
                ("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("my-cluster", "team-a"),
                ("gitops", "vcluster-my-cluster"): legacy,
            },
        )

        cli.unenroll("my-cluster", "team-a", confirm=True)

        deleted = [call.kwargs["name"] for call in core_v1.delete_namespaced_secret.call_args_list]
        assert deleted == ["vcluster-team-a-my-cluster", "vcluster-my-cluster"]

    def test_secret_of_something_else_is_left_alone(self, core_v1, capsys):
        """Two vClusters can map to one secret name: team-a/my-cluster and team/a-my-cluster."""
        serve_secrets(core_v1, {("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("a-my-cluster", "team")})

        cli.unenroll("my-cluster", "team-a", confirm=True)

        core_v1.delete_namespaced_secret.assert_not_called()
        assert "not found in ArgoCD" in capsys.readouterr().out

    def test_missing_secret_is_not_an_error(self, core_v1):
        serve_secrets(core_v1, {})

        cli.unenroll("my-cluster", "team-a", confirm=True)

        core_v1.delete_namespaced_secret.assert_not_called()

    def test_failed_removal_exits(self, core_v1):
        serve_secrets(core_v1, {("gitops", "vcluster-team-a-my-cluster"): enrolled_secret("my-cluster", "team-a")})
        core_v1.delete_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")

        with pytest.raises(SystemExit) as exc:
            cli.unenroll("my-cluster", "team-a", confirm=True)

        assert exc.value.code == 1


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
