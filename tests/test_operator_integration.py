#!/usr/bin/env python3
"""
Integration tests for vCluster ArgoCD Enroller operator.
"""

import base64
from datetime import UTC, datetime
from unittest.mock import Mock

import kopf
import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException


@pytest.fixture(autouse=True)
def reset_k8s_state():
    """Reset the lazy-init state before each test."""
    import vcluster_argocd_enroller.operator as op

    op._k8s_configured = False
    op._core_v1_api = None
    op._apps_v1_api = None
    yield
    op._k8s_configured = False
    op._core_v1_api = None
    op._apps_v1_api = None


@pytest.fixture
def k8s_mocked():
    """Mock Kubernetes API clients via the lazy-init path."""
    import vcluster_argocd_enroller.operator as op

    mock_core = Mock()
    mock_apps = Mock()
    # No vCluster has a second workload unless a test says so
    mock_apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    mock_apps.read_namespaced_stateful_set.side_effect = ApiException(status=404)

    # Pre-set the lazy-init globals so _ensure_k8s() returns our mocks
    op._k8s_configured = True
    op._core_v1_api = mock_core
    op._apps_v1_api = mock_apps

    yield mock_core, mock_apps


def create_vcluster_statefulset(name="test-cluster", namespace="vcluster-test"):
    """Create a mock vCluster StatefulSet resource."""
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "vcluster", "release": name},
            "uid": "test-uid-12345",
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "vcluster"}},
        },
    }


def create_vcluster_secret(ca=b"fake-ca-data"):
    """Create a mock vCluster secret with kubeconfig data."""
    return Mock(
        data={
            "certificate-authority": base64.b64encode(ca).decode("utf-8"),
            "client-certificate": base64.b64encode(b"fake-cert-data").decode("utf-8"),
            "client-key": base64.b64encode(b"fake-key-data").decode("utf-8"),
        }
    )


def enrolled_secret(name="test-cluster", namespace="vcluster-test", ca=b"fake-ca-data", labels=None, project=None):
    """Create the ArgoCD secret the operator writes for a vCluster, as the API returns it."""
    from vcluster_argocd_enroller.operator import build_argocd_secret

    body = build_argocd_secret(name, namespace, create_vcluster_secret(ca), labels=labels, project=project)
    metadata = client.V1ObjectMeta(labels=body["metadata"]["labels"], annotations=body["metadata"].get("annotations"))
    return client.V1Secret(metadata=metadata, data=body["data"])


def legacy_secret(name="test-cluster", namespace="vcluster-test"):
    """Create an ArgoCD secret as written before the namespace was part of its name and labels."""
    secret = enrolled_secret(name, namespace)
    secret.metadata.labels = {"argocd.argoproj.io/secret-type": "cluster", "vcluster-operator": "true"}
    server = f"https://{name}.{namespace}.svc.cluster.local"
    secret.data = {**secret.data, "server": base64.b64encode(server.encode("utf-8")).decode("utf-8")}
    return secret


def serve_secrets(mock_core, secrets):
    """Make read_namespaced_secret answer from a {(namespace, name): secret} mapping, and 404 otherwise."""

    def read(name, namespace):
        try:
            return secrets[(namespace, name)]
        except KeyError:
            raise ApiException(status=404) from None

    mock_core.read_namespaced_secret.side_effect = read


def handler_args(name="test-cluster", namespace="vcluster-test", plural="statefulsets", annotations=None):
    """Build the keyword arguments kopf passes to the handlers of a vCluster's StatefulSet or Deployment."""
    workload = create_vcluster_statefulset(name, namespace)
    return {
        "name": name,
        "namespace": namespace,
        "meta": workload["metadata"],
        "annotations": annotations or {},
        "spec": workload["spec"],
        "resource": Mock(plural=plural),
    }


def workload(labels=None, deleting=False):
    """Create a StatefulSet or Deployment as the API returns it, as far as the operator looks at it."""
    return Mock(
        metadata=client.V1ObjectMeta(
            labels={"app": "vcluster"} if labels is None else labels,
            deletion_timestamp=datetime.now(UTC) if deleting else None,
        )
    )


class TestOperatorHandlers:
    """Test the operator handlers."""

    def test_vcluster_created_handler(self, k8s_mocked):
        """Test that vcluster creation triggers ArgoCD enrollment."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        result = operator.vcluster_created(**handler_args())

        mock_core.create_namespaced_secret.assert_called_once()
        call_args = mock_core.create_namespaced_secret.call_args

        assert call_args[0][0] == "argocd"  # namespace
        secret_body = call_args[0][1]

        # The namespace is part of the secret name, the ArgoCD cluster name and the labels
        assert secret_body["metadata"]["name"] == "vcluster-vcluster-test-test-cluster"
        assert secret_body["metadata"]["namespace"] == "argocd"
        assert secret_body["metadata"]["labels"] == {
            "argocd.argoproj.io/secret-type": "cluster",
            "vcluster-operator": "true",
            "vcluster-argocd-enroller/namespace": "vcluster-test",
            "vcluster-argocd-enroller/name": "test-cluster",
        }
        assert base64.b64decode(secret_body["data"]["name"]).decode("utf-8") == "vcluster-test-test-cluster"
        server = base64.b64decode(secret_body["data"]["server"]).decode("utf-8")
        # The name vcluster's serving certificate is valid for
        assert server == "https://test-cluster.vcluster-test"
        assert "config" in secret_body["data"]

        assert result is None

    def test_same_name_in_two_namespaces_enrolls_twice(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {("team-a", "vc-shared"): create_vcluster_secret(), ("team-b", "vc-shared"): create_vcluster_secret()},
        )

        operator.vcluster_created(**handler_args("shared", "team-a"))
        operator.vcluster_created(**handler_args("shared", "team-b"))

        names = [call[0][1]["metadata"]["name"] for call in mock_core.create_namespaced_secret.call_args_list]
        assert names == ["vcluster-team-a-shared", "vcluster-team-b-shared"]

    def test_server_url_is_configurable(self, k8s_mocked, monkeypatch):
        """SERVER_URL lets vClusters with extra SANs keep the address enrolled by 1.x."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        monkeypatch.setattr(operator, "SERVER_URL", "https://{name}.{namespace}.svc.cluster.local:443")
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        operator.vcluster_created(**handler_args())

        secret_body = mock_core.create_namespaced_secret.call_args[0][1]
        server = base64.b64decode(secret_body["data"]["server"]).decode("utf-8")
        assert server == "https://test-cluster.vcluster-test.svc.cluster.local:443"

    def test_up_to_date_secret_is_not_rewritten(self, k8s_mocked):
        """A re-sync that finds nothing changed must not touch the ArgoCD secret."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret(),
            },
        )

        operator.vcluster_resync(name="test-cluster", namespace="vcluster-test")

        mock_core.create_namespaced_secret.assert_not_called()
        mock_core.replace_namespaced_secret.assert_not_called()
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_rotated_credentials_are_synced(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(ca=b"rotated-ca-data"),
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret(),
            },
        )

        operator.vcluster_resync(name="test-cluster", namespace="vcluster-test")

        name, namespace, body = mock_core.replace_namespaced_secret.call_args[0]
        assert (name, namespace) == ("vcluster-vcluster-test-test-cluster", "argocd")
        assert body["data"] == enrolled_secret(ca=b"rotated-ca-data").data
        mock_core.create_namespaced_secret.assert_not_called()

    def test_vcluster_updated_handler(self, k8s_mocked):
        """Test that vcluster update triggers ArgoCD re-enrollment."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        result = operator.vcluster_updated(**handler_args())

        mock_core.create_namespaced_secret.assert_called_once()
        assert result is None

    @pytest.mark.parametrize(
        "other",
        [
            enrolled_secret("cluster", "vcluster-test-test"),  # another vCluster whose name collides
            client.V1Secret(metadata=client.V1ObjectMeta(labels={}), data=enrolled_secret().data),  # not ours
        ],
        ids=["another-vcluster", "not-the-operators"],
    )
    def test_secret_of_something_else_is_not_overwritten(self, k8s_mocked, other):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-vcluster-test-test-cluster"): other,
            },
        )

        with pytest.raises(kopf.TemporaryError):
            operator.vcluster_created(**handler_args())

        mock_core.create_namespaced_secret.assert_not_called()
        mock_core.replace_namespaced_secret.assert_not_called()
        mock_core.delete_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize("failing", ["create_namespaced_secret", "replace_namespaced_secret"])
    def test_failed_write_is_retried(self, k8s_mocked, failing):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        secrets = {("vcluster-test", "vc-test-cluster"): create_vcluster_secret(ca=b"rotated-ca-data")}
        if failing == "replace_namespaced_secret":
            secrets[("argocd", "vcluster-vcluster-test-test-cluster")] = enrolled_secret()
        serve_secrets(mock_core, secrets)
        getattr(mock_core, failing).side_effect = ApiException(status=500)

        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_created(**handler_args())

        assert exc_info.value.delay == 60

    def test_legacy_secret_is_migrated(self, k8s_mocked):
        """An enrollment under the old namespace-less name is replaced by the new one."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-test-cluster"): legacy_secret(),
            },
        )

        operator.vcluster_resumed(**handler_args())

        assert mock_core.create_namespaced_secret.call_args[0][1]["metadata"]["name"] == (
            "vcluster-vcluster-test-test-cluster"
        )
        mock_core.delete_namespaced_secret.assert_called_once_with(name="vcluster-test-cluster", namespace="argocd")

    @pytest.mark.parametrize(
        "legacy",
        [
            legacy_secret(namespace="other-namespace"),  # the same-named vCluster elsewhere wrote it last
            client.V1Secret(metadata=client.V1ObjectMeta(labels={}), data=legacy_secret().data),  # not ours
            enrolled_secret("cluster", "test"),  # current enrollment of test/cluster, which has the same name
        ],
        ids=["same-name-elsewhere", "not-the-operators", "another-vcluster"],
    )
    def test_legacy_named_secret_of_something_else_is_kept(self, k8s_mocked, legacy):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-test-cluster"): legacy,
            },
        )

        operator.vcluster_resumed(**handler_args())

        mock_core.create_namespaced_secret.assert_called_once()
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_vcluster_deleted_handler(self, k8s_mocked):
        """Test that vcluster deletion triggers ArgoCD cleanup."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret()})

        result = operator.vcluster_deleted(**handler_args())

        mock_core.delete_namespaced_secret.assert_called_once_with(
            name="vcluster-vcluster-test-test-cluster", namespace="argocd"
        )
        assert result is None

    def test_deletion_removes_unmigrated_legacy_secret(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-test-cluster"): legacy_secret()})

        operator.vcluster_deleted(**handler_args())

        mock_core.delete_namespaced_secret.assert_called_once_with(name="vcluster-test-cluster", namespace="argocd")

    def test_deletion_leaves_other_vclusters_enrolled(self, k8s_mocked):
        """Deleting a vCluster must not take a same-named or name-colliding vCluster's enrollment with it."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret("cluster", "vcluster-test-test"),
                ("argocd", "vcluster-test-cluster"): legacy_secret(namespace="other-namespace"),
            },
        )

        operator.vcluster_deleted(**handler_args())

        mock_core.delete_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize("plural", ["statefulsets", "deployments"])
    def test_both_workload_kinds_are_enrolled(self, k8s_mocked, plural):
        """vcluster runs its control plane as a Deployment when it keeps no state of its own."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        operator.vcluster_created(**handler_args(plural=plural))

        secret_body = mock_core.create_namespaced_secret.call_args[0][1]
        assert secret_body["metadata"]["name"] == "vcluster-vcluster-test-test-cluster"

    def test_handlers_watch_statefulsets_and_deployments_of_the_apps_group(self):
        """Every handler serves both kinds, and neither the StatefulSets nor Deployments of another API group."""
        from vcluster_argocd_enroller import operator

        registry = kopf.get_default_registry()
        handlers = [*registry._changing.get_all_handlers(), *registry._spawning.get_all_handlers()]
        watched: dict[str, set[tuple]] = {}
        for handler in handlers:
            assert handler.selector is not None
            watched.setdefault(handler.id, set()).add((handler.selector.group, handler.selector.any_name))
            assert handler.labels == operator.VCLUSTER_LABELS

        assert watched == {
            handler_id: {("apps", "statefulsets"), ("apps", "deployments")}
            for handler_id in (
                "vcluster_created",
                "vcluster_resumed",
                "vcluster_updated",
                "vcluster_resync",
                "vcluster_deleted",
            )
        }

    @pytest.mark.parametrize(
        "plural,read_other",
        [("statefulsets", "read_namespaced_deployment"), ("deployments", "read_namespaced_stateful_set")],
    )
    def test_replaced_workload_keeps_the_enrollment(self, k8s_mocked, plural, read_other):
        """A vCluster switching between StatefulSet and Deployment is not unenrolled by the old one's deletion."""
        from vcluster_argocd_enroller import operator

        mock_core, mock_apps = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret()})
        getattr(mock_apps, read_other).side_effect = None
        getattr(mock_apps, read_other).return_value = workload()

        operator.vcluster_deleted(**handler_args(plural=plural))

        getattr(mock_apps, read_other).assert_called_once_with(name="test-cluster", namespace="vcluster-test")
        mock_core.delete_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize(
        "other",
        [workload(deleting=True), workload(labels={"app": "something-else"}), workload(labels={})],
        ids=["being-deleted-too", "not-a-vcluster", "unlabelled"],
    )
    def test_other_workload_that_is_not_a_live_vcluster_does_not_keep_the_enrollment(self, k8s_mocked, other):
        from vcluster_argocd_enroller import operator

        mock_core, mock_apps = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret()})
        mock_apps.read_namespaced_deployment.side_effect = None
        mock_apps.read_namespaced_deployment.return_value = other

        operator.vcluster_deleted(**handler_args())

        mock_core.delete_namespaced_secret.assert_called_once_with(
            name="vcluster-vcluster-test-test-cluster", namespace="argocd"
        )

    def test_failed_lookup_of_the_other_workload_is_retried(self, k8s_mocked):
        """Not knowing whether the vCluster lives on, the enrollment is neither removed nor given up on."""
        from vcluster_argocd_enroller import operator

        mock_core, mock_apps = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret()})
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=500)

        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_deleted(**handler_args())

        assert not isinstance(exc_info.value, kopf.PermanentError)
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_vcluster_with_prefix_name(self, k8s_mocked):
        """Test handling of vcluster with 'vcluster-' prefix in StatefulSet name."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-vcluster-my-cluster"): create_vcluster_secret()})

        operator.vcluster_created(**handler_args("vcluster-my-cluster"))

        # vcluster names its secret and Service after the release, prefix included
        secret_body = mock_core.create_namespaced_secret.call_args[0][1]
        assert secret_body["metadata"]["name"] == "vcluster-vcluster-test-vcluster-my-cluster"
        server = base64.b64decode(secret_body["data"]["server"]).decode("utf-8")
        assert server == "https://vcluster-my-cluster.vcluster-test"

    def test_missing_vcluster_secret_temporary_error(self, k8s_mocked):
        """Test that missing vcluster secret causes temporary retry."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)

        # Should raise TemporaryError for retry
        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_created(**handler_args())

        mock_core.read_namespaced_secret.assert_called_once_with(name="vc-test-cluster", namespace="vcluster-test")
        assert "Failed to read vcluster secret" in str(exc_info.value)
        assert exc_info.value.delay == 60

    def test_missing_vcluster_secret_logs_warning_not_error(self, k8s_mocked, caplog):
        """A 404 right after the StatefulSet appears is expected ordering, so it must not log ERROR."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)

        with caplog.at_level("WARNING"), pytest.raises(kopf.TemporaryError):
            operator.vcluster_created(**handler_args())

        records = [r for r in caplog.records if "reading vcluster secret" in r.getMessage()]
        assert records and all(r.levelname == "WARNING" for r in records)

    def test_non_404_read_error_still_logs_error(self, k8s_mocked, caplog):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.read_namespaced_secret.side_effect = ApiException(status=500)

        with caplog.at_level("WARNING"), pytest.raises(kopf.TemporaryError):
            operator.vcluster_created(**handler_args())

        assert any(r.levelname == "ERROR" and "reading vcluster secret" in r.getMessage() for r in caplog.records)

    def test_configure_uses_annotation_storage(self):
        from vcluster_argocd_enroller import operator

        settings = kopf.OperatorSettings()
        operator.configure(settings=settings)

        assert isinstance(settings.persistence.progress_storage, kopf.AnnotationsProgressStorage)
        assert isinstance(settings.persistence.diffbase_storage, kopf.AnnotationsDiffBaseStorage)

    def test_argocd_secret_already_deleted(self, k8s_mocked):
        """Test graceful handling when ArgoCD secret is already deleted."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)

        result = operator.vcluster_deleted(**handler_args())

        mock_core.delete_namespaced_secret.assert_not_called()
        assert result is None

    def test_failed_deletion_is_retried_not_abandoned(self, k8s_mocked):
        """A failed cleanup is retried (kopf gives up after DELETE_RETRIES), never raised as permanent."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret()})
        mock_core.delete_namespaced_secret.side_effect = ApiException(status=500)

        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_deleted(**handler_args())

        assert not isinstance(exc_info.value, kopf.PermanentError)

    def test_malformed_vcluster_secret(self, k8s_mocked):
        """A vcluster secret missing its credentials is retried: it can be completed later."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): Mock(data={"some-field": "value"})})

        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_created(**handler_args())

        assert "Failed to parse vcluster secret" in str(exc_info.value)
        mock_core.create_namespaced_secret.assert_not_called()

    def test_argocd_namespace_from_env(self, k8s_mocked):
        """Test that ARGOCD_NAMESPACE env var is respected."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        # The default is "argocd"
        operator.vcluster_created(**handler_args())

        call_args = mock_core.create_namespaced_secret.call_args
        assert call_args[0][0] == "argocd"


ARGOCD_SECRET = ("argocd", "vcluster-vcluster-test-test-cluster")
VC_SECRET = ("vcluster-test", "vc-test-cluster")


class TestEnrollmentAnnotations:
    """Test what the annotations on a vCluster's workload do to its enrollment."""

    def test_labels_and_project_reach_the_argocd_secret(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})
        annotations = {
            "vcluster-argocd-enroller/labels": "env=dev, example.com/team=payments,empty=",
            "vcluster-argocd-enroller/project": "payments",
        }

        operator.vcluster_created(**handler_args(annotations=annotations))

        body = mock_core.create_namespaced_secret.call_args[0][1]
        assert body["metadata"]["labels"] == {
            "env": "dev",
            "example.com/team": "payments",
            "empty": "",
            "argocd.argoproj.io/secret-type": "cluster",
            "vcluster-operator": "true",
            "vcluster-argocd-enroller/namespace": "vcluster-test",
            "vcluster-argocd-enroller/name": "test-cluster",
        }
        assert body["metadata"]["annotations"] == {
            "vcluster-argocd-enroller/copied-labels": "empty,env,example.com/team"
        }
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "payments"

    def test_unannotated_vcluster_gets_neither_project_nor_annotations(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})

        operator.vcluster_created(**handler_args())

        body = mock_core.create_namespaced_secret.call_args[0][1]
        assert "project" not in body["data"]
        assert "annotations" not in body["metadata"]

    def test_labels_that_cannot_be_copied_are_left_out(self, k8s_mocked, caplog):
        """A bad entry costs the vCluster that label, not its enrollment, and never an ownership label."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})
        asked = "env=dev,no-value,bad key=x,tier=not/valid,vcluster-argocd-enroller/name=other,vcluster-operator=false"

        with caplog.at_level("WARNING"):
            operator.vcluster_created(**handler_args(annotations={"vcluster-argocd-enroller/labels": asked}))

        labels = mock_core.create_namespaced_secret.call_args[0][1]["metadata"]["labels"]
        assert labels["env"] == "dev"
        assert labels["vcluster-argocd-enroller/name"] == "test-cluster"
        assert labels["vcluster-operator"] == "true"
        assert set(labels) == {
            "env",
            "argocd.argoproj.io/secret-type",
            "vcluster-operator",
            "vcluster-argocd-enroller/namespace",
            "vcluster-argocd-enroller/name",
        }
        warnings = [r.getMessage() for r in caplog.records if "Ignoring a label" in r.getMessage()]
        assert len(warnings) == 5 and all("vcluster-test/test-cluster" in w for w in warnings)

    def test_changed_labels_and_project_are_synced(self, k8s_mocked):
        """A label that is no longer asked for goes; what something else put on the secret stays."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        existing = enrolled_secret(labels={"env": "dev", "tier": "gold"}, project="payments")
        existing.metadata.labels["argocd.argoproj.io/kubernetes-version"] = "1.35"
        existing.metadata.annotations["managed-by"] = "argocd.argoproj.io"
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: existing})
        annotations = {"vcluster-argocd-enroller/labels": "env=prod", "vcluster-argocd-enroller/project": "platform"}

        operator.vcluster_updated(**handler_args(annotations=annotations))

        body = mock_core.replace_namespaced_secret.call_args[0][2]
        assert body["metadata"]["labels"] == {
            "env": "prod",
            "argocd.argoproj.io/kubernetes-version": "1.35",
            "argocd.argoproj.io/secret-type": "cluster",
            "vcluster-operator": "true",
            "vcluster-argocd-enroller/namespace": "vcluster-test",
            "vcluster-argocd-enroller/name": "test-cluster",
        }
        assert body["metadata"]["annotations"] == {
            "managed-by": "argocd.argoproj.io",
            "vcluster-argocd-enroller/copied-labels": "env",
        }
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "platform"

    def test_dropped_annotations_clear_the_labels_and_project(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        existing = enrolled_secret(labels={"env": "dev"}, project="payments")
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: existing})

        operator.vcluster_updated(**handler_args())

        body = mock_core.replace_namespaced_secret.call_args[0][2]
        assert body["metadata"]["labels"] == enrolled_secret().metadata.labels
        assert body["metadata"]["annotations"] == {}
        assert body["data"] == enrolled_secret().data

    def test_secret_in_sync_with_the_annotations_is_not_rewritten(self, k8s_mocked):
        """Neither the copied labels nor the ones something else added make a re-sync write."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        existing = enrolled_secret(labels={"env": "dev"}, project="payments")
        existing.metadata.labels["argocd.argoproj.io/kubernetes-version"] = "1.35"
        existing.metadata.annotations["managed-by"] = "argocd.argoproj.io"
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: existing})
        annotations = {"vcluster-argocd-enroller/labels": "env=dev", "vcluster-argocd-enroller/project": "payments"}

        operator.vcluster_resync(**handler_args(annotations=annotations))

        mock_core.create_namespaced_secret.assert_not_called()
        mock_core.replace_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize("handler", ["vcluster_created", "vcluster_resumed", "vcluster_updated", "vcluster_resync"])
    @pytest.mark.parametrize("value", ["false", " False "])
    def test_opted_out_vcluster_is_not_enrolled(self, k8s_mocked, handler, value):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})

        result = getattr(operator, handler)(**handler_args(annotations={"vcluster-argocd-enroller/enroll": value}))

        mock_core.create_namespaced_secret.assert_not_called()
        mock_core.delete_namespaced_secret.assert_not_called()
        assert result is None

    def test_opting_out_removes_the_enrollment(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                VC_SECRET: create_vcluster_secret(),
                ARGOCD_SECRET: enrolled_secret(),
                ("argocd", "vcluster-test-cluster"): legacy_secret(),
            },
        )

        operator.vcluster_updated(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        deleted = [call.kwargs["name"] for call in mock_core.delete_namespaced_secret.call_args_list]
        assert deleted == ["vcluster-vcluster-test-test-cluster", "vcluster-test-cluster"]
        mock_core.replace_namespaced_secret.assert_not_called()

    def test_opting_out_leaves_the_secret_of_something_else(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {ARGOCD_SECRET: enrolled_secret("cluster", "vcluster-test-test")})

        operator.vcluster_updated(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        mock_core.delete_namespaced_secret.assert_not_called()

    def test_failed_removal_after_opting_out_is_retried(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {ARGOCD_SECRET: enrolled_secret()})
        mock_core.delete_namespaced_secret.side_effect = ApiException(status=500)

        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_updated(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        assert exc_info.value.delay == 60

    @pytest.mark.parametrize("value,warns", [("true", False), ("no", True), ("", True)])
    def test_only_false_opts_out(self, k8s_mocked, caplog, value, warns):
        """Anything but false enrolls, and anything but true or false is pointed out."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})

        with caplog.at_level("WARNING"):
            operator.vcluster_created(**handler_args(annotations={"vcluster-argocd-enroller/enroll": value}))

        mock_core.create_namespaced_secret.assert_called_once()
        assert any("neither true nor false" in r.getMessage() for r in caplog.records) == warns

    def test_deleting_an_opted_out_vcluster_finds_nothing_to_remove(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {})

        operator.vcluster_deleted(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        mock_core.delete_namespaced_secret.assert_not_called()


class TestUtilityFunctions:
    """Test utility functions."""

    @pytest.mark.parametrize(
        "entry,label",
        [
            ("env=dev", ("env", "dev")),
            (" env = dev ", ("env", "dev")),
            ("example.com/team=payments", ("example.com/team", "payments")),
            ("flag=", ("flag", "")),
            ("A_b.c-1=V_2.x-y", ("A_b.c-1", "V_2.x-y")),
        ],
    )
    def test_parse_label(self, entry, label):
        from vcluster_argocd_enroller.operator import parse_label

        assert parse_label(entry) == label

    @pytest.mark.parametrize(
        "entry",
        [
            "env",  # no value
            "=dev",  # no key
            "-env=dev",  # must start with an alphanumeric
            "a/b/c=dev",  # one prefix at most
            "Example.com/team=x",  # the prefix is a lower-case DNS name
            "/team=x",  # empty prefix
            f"{'k' * 64}=x",  # name too long
            f"k={'v' * 64}",  # value too long
            "env=de v",
            "env=dev=prod",
            "argocd.argoproj.io/secret-type=repository",  # the operator's own
            "vcluster-argocd-enroller/namespace=elsewhere",
        ],
    )
    def test_parse_label_rejects_what_the_secret_cannot_carry(self, entry):
        from vcluster_argocd_enroller.operator import parse_label

        with pytest.raises(ValueError):
            parse_label(entry)

    def test_vc_name_is_workload_name(self):
        """The vcluster name is the StatefulSet (Helm release) name, prefix or not."""
        from vcluster_argocd_enroller.operator import vc_name

        assert vc_name("my-cluster") == "my-cluster"
        assert vc_name("vcluster-my-cluster") == "vcluster-my-cluster"

    def test_ar_secret_name(self):
        """Test ArgoCD secret name generation."""
        from vcluster_argocd_enroller.operator import ar_secret_name, argocd_cluster_name, legacy_ar_secret_name

        assert argocd_cluster_name("team-a", "my-cluster") == "team-a-my-cluster"
        assert ar_secret_name("team-a", "my-cluster") == "vcluster-team-a-my-cluster"
        assert ar_secret_name("team-b", "my-cluster") == "vcluster-team-b-my-cluster"
        assert legacy_ar_secret_name("my-cluster") == "vcluster-my-cluster"

    def test_encode_decode(self):
        """Test base64 encoding/decoding functions."""
        from vcluster_argocd_enroller.operator import decode, encode

        test_string = "test-data-123"
        encoded = encode(test_string)
        decoded = decode(encoded)
        assert decoded == test_string

        # Test with special characters
        special = "https://test.example.com:8443/api"
        assert decode(encode(special)) == special
