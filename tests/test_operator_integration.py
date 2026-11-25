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

    # Every write is a patch. The API answers an apply with the secret, at a new version when it changed.
    mock_core.patch_namespaced_secret.return_value = api_secret(resource_version="2")

    # Pre-set the lazy-init globals so _ensure_k8s() returns our mocks
    op._k8s_configured = True
    op._core_v1_api = mock_core
    op._apps_v1_api = mock_apps

    yield mock_core, mock_apps


def api_secret(resource_version):
    """Create what the API answers a patch with, as far as the operator looks at it."""
    return client.V1Secret(metadata=client.V1ObjectMeta(resource_version=resource_version))


def applies(mock_core):
    """Return the (name, namespace, body) of every server-side apply made."""
    calls = mock_core.patch_namespaced_secret.call_args_list
    return [call.args for call in calls if call.kwargs["_content_type"] == "application/apply-patch+yaml"]


def applied(mock_core):
    """Return the secret applied, the one time something was."""
    ((_, _, body),) = applies(mock_core)
    return body


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
    metadata = client.V1ObjectMeta(labels=body["metadata"]["labels"], annotations={}, resource_version="1")
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

        ((name, namespace, secret_body),) = applies(mock_core)
        assert (name, namespace) == ("vcluster-vcluster-test-test-cluster", "argocd")
        # Applied as the operator's field manager, taking back fields something else changed
        kwargs = mock_core.patch_namespaced_secret.call_args.kwargs
        assert (kwargs["field_manager"], kwargs["force"]) == ("vcluster-argocd-enroller", True)
        # Only what the operator sets: no type, no annotations, and no version of a secret that is not there yet
        assert set(secret_body) == {"apiVersion", "kind", "metadata", "data"}
        assert set(secret_body["metadata"]) == {"name", "namespace", "labels"}

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

        names = [body["metadata"]["name"] for _, _, body in applies(mock_core)]
        assert names == ["vcluster-team-a-shared", "vcluster-team-b-shared"]

    def test_server_url_is_configurable(self, k8s_mocked, monkeypatch):
        """SERVER_URL lets vClusters with extra SANs keep the address enrolled by 1.x."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        monkeypatch.setattr(operator, "SERVER_URL", "https://{name}.{namespace}.svc.cluster.local:443")
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        operator.vcluster_created(**handler_args())

        secret_body = applied(mock_core)
        server = base64.b64decode(secret_body["data"]["server"]).decode("utf-8")
        assert server == "https://test-cluster.vcluster-test.svc.cluster.local:443"

    def test_up_to_date_secret_is_not_reported_as_updated(self, k8s_mocked, caplog):
        """An apply that changes nothing leaves the secret at its version: the API server does the comparing."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.patch_namespaced_secret.return_value = api_secret(resource_version="1")
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret(),
            },
        )

        with caplog.at_level("DEBUG"):
            operator.vcluster_resync(name="test-cluster", namespace="vcluster-test")

        messages = [r.getMessage() for r in caplog.records]
        assert any("is up to date" in m for m in messages)
        assert not any(m.startswith(("Created", "Updated", "Took over")) for m in messages)
        assert len(mock_core.patch_namespaced_secret.call_args_list) == 1
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_rotated_credentials_are_synced(self, k8s_mocked, caplog):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(ca=b"rotated-ca-data"),
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret(),
            },
        )

        with caplog.at_level("INFO"):
            operator.vcluster_resync(name="test-cluster", namespace="vcluster-test")

        body = applied(mock_core)
        assert body["data"] == enrolled_secret(ca=b"rotated-ca-data").data
        # On top of the version that was read: a secret changed or replaced in between is a conflict to retry
        assert body["metadata"]["resourceVersion"] == "1"
        assert any(r.getMessage().startswith("Updated ArgoCD cluster secret") for r in caplog.records)

    def test_vcluster_updated_handler(self, k8s_mocked):
        """Test that vcluster update triggers ArgoCD re-enrollment."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        result = operator.vcluster_updated(**handler_args())

        assert len(applies(mock_core)) == 1
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

        assert not applies(mock_core)
        mock_core.delete_namespaced_secret.assert_not_called()

    @pytest.mark.parametrize("status", [409, 500], ids=["changed-in-between", "server-error"])
    @pytest.mark.parametrize("enrolled", [False, True], ids=["create", "update"])
    def test_failed_write_is_retried(self, k8s_mocked, status, enrolled):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        secrets = {("vcluster-test", "vc-test-cluster"): create_vcluster_secret(ca=b"rotated-ca-data")}
        if enrolled:
            secrets[("argocd", "vcluster-vcluster-test-test-cluster")] = enrolled_secret()
        serve_secrets(mock_core, secrets)
        mock_core.patch_namespaced_secret.side_effect = ApiException(status=status)

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

        assert applied(mock_core)["metadata"]["name"] == ("vcluster-vcluster-test-test-cluster")
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

        assert len(applies(mock_core)) == 1
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

        secret_body = applied(mock_core)
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
        secret_body = applied(mock_core)
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
        assert not applies(mock_core)

    def test_argocd_namespace_from_env(self, k8s_mocked):
        """Test that ARGOCD_NAMESPACE env var is respected."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})

        # The default is "argocd"
        operator.vcluster_created(**handler_args())

        ((_, namespace, _),) = applies(mock_core)
        assert namespace == "argocd"


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

        body = applied(mock_core)
        assert body["metadata"]["labels"] == {
            "env": "dev",
            "example.com/team": "payments",
            "empty": "",
            "argocd.argoproj.io/secret-type": "cluster",
            "vcluster-operator": "true",
            "vcluster-argocd-enroller/namespace": "vcluster-test",
            "vcluster-argocd-enroller/name": "test-cluster",
        }
        assert "annotations" not in body["metadata"]
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "payments"

    def test_unannotated_vcluster_gets_neither_project_nor_annotations(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})

        operator.vcluster_created(**handler_args())

        body = applied(mock_core)
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

        labels = applied(mock_core)["metadata"]["labels"]
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
        """Only what is asked for now is applied. The API server drops what was applied before and no longer is,
        and keeps what something else put on the secret: `task k3d-smoke` checks that against a real one."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        existing = enrolled_secret(labels={"env": "dev", "tier": "gold"}, project="payments")
        existing.metadata.labels["argocd.argoproj.io/kubernetes-version"] = "1.35"
        existing.metadata.annotations["managed-by"] = "argocd.argoproj.io"
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: existing})
        annotations = {"vcluster-argocd-enroller/labels": "env=prod", "vcluster-argocd-enroller/project": "platform"}

        operator.vcluster_updated(**handler_args(annotations=annotations))

        body = applied(mock_core)
        assert body["metadata"]["labels"] == {
            "env": "prod",
            "argocd.argoproj.io/secret-type": "cluster",
            "vcluster-operator": "true",
            "vcluster-argocd-enroller/namespace": "vcluster-test",
            "vcluster-argocd-enroller/name": "test-cluster",
        }
        assert "annotations" not in body["metadata"]
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "platform"

    def test_dropped_annotations_clear_the_labels_and_project(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        existing = enrolled_secret(labels={"env": "dev"}, project="payments")
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: existing})

        operator.vcluster_updated(**handler_args())

        body = applied(mock_core)
        assert body["metadata"]["labels"] == enrolled_secret().metadata.labels
        assert body["data"] == enrolled_secret().data

    @pytest.mark.parametrize("handler", ["vcluster_created", "vcluster_resumed", "vcluster_updated", "vcluster_resync"])
    @pytest.mark.parametrize("value", ["false", " False "])
    def test_opted_out_vcluster_is_not_enrolled(self, k8s_mocked, handler, value):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret()})

        result = getattr(operator, handler)(**handler_args(annotations={"vcluster-argocd-enroller/enroll": value}))

        assert not applies(mock_core)
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

        assert len(applies(mock_core)) == 1
        assert any("neither true nor false" in r.getMessage() for r in caplog.records) == warns

    def test_deleting_an_opted_out_vcluster_finds_nothing_to_remove(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {})

        operator.vcluster_deleted(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        mock_core.delete_namespaced_secret.assert_not_called()


def managed(manager, operation, labels=(), annotations=(), data=(), extra=None):
    """Create a managed-fields entry: who the API server says set which fields of a secret."""
    fields = {
        "f:metadata": {
            "f:labels": {".": {}, **{f"f:{key}": {} for key in labels}},
            "f:annotations": {".": {}, **{f"f:{key}": {} for key in annotations}},
        },
        "f:data": {".": {}, **{f"f:{key}": {} for key in data}},
        **(extra or {}),
    }
    return client.V1ManagedFieldsEntry(
        manager=manager,
        operation=operation,
        api_version="v1",
        fields_type="FieldsV1",
        fields_v1=fields,
        time=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )


OWN_LABELS = (
    "argocd.argoproj.io/secret-type",
    "vcluster-operator",
    "vcluster-argocd-enroller/namespace",
    "vcluster-argocd-enroller/name",
)


class TestFieldsWrittenBeforeServerSideApply:
    """Test handing the fields of a secret written by 2.2 or earlier over to the operator's field manager.

    Left with the old manager, a label or project dropped later would stay on the secret.
    """

    def secret_from_2_2(self):
        """A 2.2 enrollment with copied labels and a project, also labelled by kubectl and by another Python tool."""
        secret = enrolled_secret(labels={"env": "dev", "tier": "gold"}, project="payments")
        secret.metadata.labels.update({"by-kubectl": "x", "by-python-tool": "y"})
        secret.metadata.annotations = {"vcluster-argocd-enroller/copied-labels": "env,tier", "note": "kept"}
        secret.metadata.managed_fields = [
            managed(
                "OpenAPI-Generator",
                "Update",
                labels=(*OWN_LABELS, "env", "tier", "by-python-tool"),
                annotations=("vcluster-argocd-enroller/copied-labels",),
                data=("name", "server", "config", "project"),
                extra={"f:type": {}},
            ),
            managed("kubectl-label", "Update", labels=("by-kubectl",), annotations=("note",)),
        ]
        return secret

    def test_fields_the_operator_wrote_are_taken_over(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import _adopted_managed_fields

        kubectl, ours = _adopted_managed_fields(self.secret_from_2_2())

        assert kubectl["manager"] == "kubectl-label" and kubectl["time"] == "2026-11-01T12:00:00Z"
        assert kubectl["fieldsV1"]["f:metadata"]["f:labels"] == {".": {}, "f:by-kubectl": {}}
        assert (ours["manager"], ours["operation"]) == ("vcluster-argocd-enroller", "Apply")
        # Its own labels and the ones it copied, but not the label another Python client set under the same name
        assert set(ours["fieldsV1"]["f:metadata"]["f:labels"]) == {f"f:{key}" for key in (*OWN_LABELS, "env", "tier")}
        assert set(ours["fieldsV1"]["f:metadata"]["f:annotations"]) == {"f:vcluster-argocd-enroller/copied-labels"}
        assert set(ours["fieldsV1"]["f:data"]) == {".", "f:name", "f:server", "f:config", "f:project"}
        # Not the type: the operator does not apply one
        assert set(ours["fieldsV1"]) == {"f:metadata", "f:data"}

    def test_fields_already_applied_are_kept(self, k8s_mocked):
        """A downgrade to 2.2 and back leaves both managers on the secret."""
        from vcluster_argocd_enroller.operator import _adopted_managed_fields

        secret = self.secret_from_2_2()
        secret.metadata.managed_fields.append(
            managed("vcluster-argocd-enroller", "Apply", labels=(*OWN_LABELS, "zone"), data=("name",))
        )

        *others, ours = _adopted_managed_fields(secret)

        assert [entry["manager"] for entry in others] == ["kubectl-label"]
        assert {"f:zone", "f:env", "f:tier"} <= set(ours["fieldsV1"]["f:metadata"]["f:labels"])

    @pytest.mark.parametrize(
        "managed_fields",
        [
            None,
            [],
            [managed("vcluster-argocd-enroller", "Apply", labels=OWN_LABELS), managed("kubectl-label", "Update")],
        ],
        ids=["unknown", "none", "applied"],
    )
    def test_nothing_to_take_over(self, k8s_mocked, managed_fields):
        from vcluster_argocd_enroller.operator import _adopted_managed_fields

        secret = enrolled_secret()
        secret.metadata.managed_fields = managed_fields

        assert _adopted_managed_fields(secret) is None

    def test_sync_takes_the_fields_over_before_applying(self, k8s_mocked, caplog):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: self.secret_from_2_2()})
        mock_core.patch_namespaced_secret.side_effect = [api_secret("2"), api_secret("3")]

        with caplog.at_level("INFO"):
            operator.vcluster_resync(**handler_args(annotations={"vcluster-argocd-enroller/labels": "env=dev"}))

        takeover, apply = mock_core.patch_namespaced_secret.call_args_list
        assert takeover.kwargs == {"_content_type": "application/merge-patch+json"}
        name, namespace, patch = takeover.args
        assert (name, namespace) == ("vcluster-vcluster-test-test-cluster", "argocd")
        # Only the bookkeeping changes, and only if the secret is still the one that was read
        assert set(patch) == {"metadata"} and set(patch["metadata"]) == {"resourceVersion", "managedFields"}
        assert patch["metadata"]["resourceVersion"] == "1"
        assert [entry["manager"] for entry in patch["metadata"]["managedFields"]] == [
            "kubectl-label",
            "vcluster-argocd-enroller",
        ]
        # The apply follows on the version the takeover left, without the dropped label, the project or the annotation
        body = applied(mock_core)
        assert apply.kwargs["_content_type"] == "application/apply-patch+yaml"
        assert body["metadata"]["resourceVersion"] == "2"
        assert "tier" not in body["metadata"]["labels"] and "project" not in body["data"]
        assert any(r.getMessage().startswith("Took over the fields") for r in caplog.records)

    def test_failed_takeover_is_retried_without_applying(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {VC_SECRET: create_vcluster_secret(), ARGOCD_SECRET: self.secret_from_2_2()})
        mock_core.patch_namespaced_secret.side_effect = ApiException(status=409)

        with pytest.raises(kopf.TemporaryError):
            operator.vcluster_resync(**handler_args())

        assert not applies(mock_core)


class TestAllowlists:
    """Test limiting what a workload's annotations may ask for."""

    ANNOTATIONS = {
        "vcluster-argocd-enroller/labels": "env=dev,example.com/team=payments,owner=mallory",
        "vcluster-argocd-enroller/project": "team-payments",
    }

    def enroll(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {("vcluster-test", "vc-test-cluster"): create_vcluster_secret()})
        operator.vcluster_created(**handler_args(annotations=self.ANNOTATIONS))
        return applied(mock_core)

    def test_everything_is_allowed_by_default(self, k8s_mocked):
        body = self.enroll(k8s_mocked)

        assert {"env", "example.com/team", "owner"} <= body["metadata"]["labels"].keys()
        assert base64.b64decode(body["data"]["project"]).decode("utf-8") == "team-payments"

    def test_label_keys_outside_the_allowlist_are_left_out(self, k8s_mocked, monkeypatch, caplog):
        from vcluster_argocd_enroller import operator

        monkeypatch.setattr(operator, "ALLOWED_LABELS", ("env", "example.com/*"))

        with caplog.at_level("WARNING"):
            body = self.enroll(k8s_mocked)

        assert body["metadata"]["labels"]["env"] == "dev"
        assert body["metadata"]["labels"]["example.com/team"] == "payments"
        assert "owner" not in body["metadata"]["labels"]
        assert [r.getMessage() for r in caplog.records if "'owner' is not a label key" in r.getMessage()]

    @pytest.mark.parametrize(
        "patterns,project",
        [(("team-*",), "team-payments"), (("team-payments", "other"), "team-payments"), (("platform",), None)],
        ids=["pattern", "exact", "not-allowed"],
    )
    def test_project_outside_the_allowlist_is_left_out(self, k8s_mocked, monkeypatch, caplog, patterns, project):
        from vcluster_argocd_enroller import operator

        monkeypatch.setattr(operator, "ALLOWED_PROJECTS", patterns)

        with caplog.at_level("WARNING"):
            body = self.enroll(k8s_mocked)

        enrolled = base64.b64decode(body["data"]["project"]).decode("utf-8") if "project" in body["data"] else None
        assert enrolled == project
        assert any("Ignoring the project" in r.getMessage() for r in caplog.records) == (project is None)

    def test_disallowed_label_already_copied_is_removed(self, k8s_mocked, monkeypatch):
        """Tightening the allowlist takes effect on vClusters enrolled before it."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        monkeypatch.setattr(operator, "ALLOWED_LABELS", ("env",))
        serve_secrets(
            mock_core,
            {
                ("vcluster-test", "vc-test-cluster"): create_vcluster_secret(),
                ("argocd", "vcluster-vcluster-test-test-cluster"): enrolled_secret(
                    labels={"env": "dev", "owner": "mallory"}
                ),
            },
        )

        operator.vcluster_resync(
            **handler_args(annotations={"vcluster-argocd-enroller/labels": "env=dev,owner=mallory"})
        )

        body = applied(mock_core)
        assert "owner" not in body["metadata"]["labels"]
        assert body["metadata"]["labels"]["env"] == "dev"

    @pytest.mark.parametrize(
        "value,expected",
        [(None, ()), ("", ()), (" , ", ()), ("env", ("env",)), (" env , example.com/* ,", ("env", "example.com/*"))],
        ids=["unset", "empty", "blank", "one", "several"],
    )
    def test_allowlist_is_read_from_a_comma_separated_variable(self, monkeypatch, value, expected):
        from vcluster_argocd_enroller.operator import _env_list

        if value is None:
            monkeypatch.delenv("ALLOWED_LABELS", raising=False)
        else:
            monkeypatch.setenv("ALLOWED_LABELS", value)

        assert _env_list("ALLOWED_LABELS") == expected


class TestOrphanSweep:
    """Test the removal of enrollments whose vCluster is gone."""

    @staticmethod
    def enrollments(mock_core, *vclusters):
        """Enroll (namespace, name) vClusters in ArgoCD, and return their secrets by name."""
        secrets = {}
        for namespace, name in vclusters:
            secret = enrolled_secret(name, namespace)
            secret.metadata.name = f"vcluster-{namespace}-{name}"
            secret.metadata.uid = f"uid-{namespace}-{name}"
            secrets[secret.metadata.name] = secret

        def read(name, namespace):
            if name not in secrets:
                raise ApiException(status=404)
            return secrets[name]

        mock_core.list_namespaced_secret.return_value = Mock(items=list(secrets.values()))
        mock_core.read_namespaced_secret.side_effect = read
        return secrets

    @staticmethod
    def workload(labels):
        return Mock(metadata=client.V1ObjectMeta(labels=labels))

    def test_enrollment_is_removed_the_second_time_its_vcluster_is_missing(self, k8s_mocked, caplog):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        self.enrollments(mock_core, ("team-a", "gone"))

        suspects = sweep_orphans(mock_core, mock_apps)

        assert suspects == {"uid-team-a-gone"}
        mock_core.delete_namespaced_secret.assert_not_called()
        mock_core.list_namespaced_secret.assert_called_once_with(
            namespace="argocd", label_selector="vcluster-operator=true"
        )

        with caplog.at_level("INFO"):
            assert sweep_orphans(mock_core, mock_apps, suspects=suspects) == set()

        mock_core.delete_namespaced_secret.assert_called_once_with(name="vcluster-team-a-gone", namespace="argocd")
        assert "Removing the enrollment of team-a/gone" in caplog.text

    @pytest.mark.parametrize("kind", ["stateful_set", "deployment"])
    def test_enrollment_of_a_running_vcluster_is_kept(self, k8s_mocked, kind):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        self.enrollments(mock_core, ("team-a", "alive"))
        read = getattr(mock_apps, f"read_namespaced_{kind}")
        read.side_effect = None
        read.return_value = self.workload({"app": "vcluster"})

        assert sweep_orphans(mock_core, mock_apps, suspects={"uid-team-a-alive"}) == set()

        read.assert_called_once_with(name="alive", namespace="team-a")
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_workload_that_lost_its_label_no_longer_counts(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        self.enrollments(mock_core, ("team-a", "unlabelled"))
        mock_apps.read_namespaced_stateful_set.side_effect = None
        mock_apps.read_namespaced_stateful_set.return_value = self.workload({"release": "unlabelled"})

        sweep_orphans(mock_core, mock_apps, suspects={"uid-team-a-unlabelled"})

        mock_core.delete_namespaced_secret.assert_called_once()

    def test_vcluster_that_cannot_be_looked_up_keeps_its_enrollment(self, k8s_mocked, caplog):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        self.enrollments(mock_core, ("team-a", "unknown"), ("team-b", "gone"))
        mock_apps.read_namespaced_deployment.side_effect = lambda name, namespace: (_ for _ in ()).throw(
            ApiException(status=403 if namespace == "team-a" else 404, reason="Forbidden")
        )

        suspects = sweep_orphans(mock_core, mock_apps, suspects={"uid-team-a-unknown"})

        # Not deleted, not even suspected: and the next enrollment is still looked at
        assert suspects == {"uid-team-b-gone"}
        mock_core.delete_namespaced_secret.assert_not_called()
        assert "Keeping the enrollment of team-a/unknown: 403 Forbidden" in caplog.text

    def test_only_watched_namespaces_are_swept(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        self.enrollments(mock_core, ("team-a", "gone"), ("team-b", "gone"))

        suspects = sweep_orphans(mock_core, mock_apps, namespaces=("team-a",), suspects={"uid-team-b-gone"})

        assert suspects == {"uid-team-a-gone"}
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_recreated_enrollment_is_given_time_again(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        secrets = self.enrollments(mock_core, ("team-a", "restored"))
        suspects = sweep_orphans(mock_core, mock_apps)
        secrets["vcluster-team-a-restored"].metadata.uid = "another"

        assert sweep_orphans(mock_core, mock_apps, suspects=suspects) == {"another"}
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_secrets_that_are_not_labelled_enrollments_are_left_alone(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        secrets = self.enrollments(mock_core, ("team-a", "legacy"), ("team-a", "renamed"))
        legacy, renamed = secrets.values()
        legacy.metadata.labels = {"argocd.argoproj.io/secret-type": "cluster", "vcluster-operator": "true"}
        renamed.metadata.name = "a-copy"

        assert sweep_orphans(mock_core, mock_apps, suspects={legacy.metadata.uid, renamed.metadata.uid}) == set()

        mock_apps.read_namespaced_stateful_set.assert_not_called()
        mock_core.delete_namespaced_secret.assert_not_called()

    def test_sweep_goes_on_after_a_failure(self, k8s_mocked, monkeypatch, caplog):
        """A sweep that cannot list the secrets is tried again, with the namespaces and suspects it had."""
        import asyncio

        from vcluster_argocd_enroller import operator

        mock_core, mock_apps = k8s_mocked
        calls = []

        def sweep(core, apps, namespaces, suspects):
            calls.append((namespaces, set(suspects)))
            if len(calls) == 1:
                return {"uid"}
            if len(calls) == 2:
                raise ApiException(status=500, reason="Internal Server Error")
            raise asyncio.CancelledError

        monkeypatch.setattr(operator, "sweep_orphans", sweep)
        monkeypatch.setattr(operator, "RESYNC_INTERVAL", 0)
        monkeypatch.setattr(operator, "WATCHED_NAMESPACES", ("team-a",))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(operator._sweep_periodically())

        assert calls == [(("team-a",), set()), (("team-a",), {"uid"}), (("team-a",), {"uid"})]
        assert "500 Internal Server Error" in caplog.text

    @pytest.mark.parametrize("enabled", [True, False])
    def test_sweep_starts_with_the_operator_unless_turned_off(self, monkeypatch, enabled):
        import asyncio

        from vcluster_argocd_enroller import operator

        async def run():
            await operator.start_sweep()
            task = operator._sweep_task
            await operator.stop_sweep()
            return task

        async def forever():
            await asyncio.sleep(3600)

        monkeypatch.setattr(operator, "SWEEP_ORPHANS", enabled)
        monkeypatch.setattr(operator, "_sweep_task", None)
        monkeypatch.setattr(operator, "_sweep_periodically", forever)

        assert (asyncio.run(run()) is not None) is enabled


def metric(name, **labels):
    """Return the current value of one of the operator's metrics."""
    from prometheus_client import REGISTRY

    import vcluster_argocd_enroller.metrics  # noqa: F401

    return REGISTRY.get_sample_value(f"vcluster_argocd_enroller_{name}", labels)


class TestMetrics:
    """Test what the operator counts."""

    def test_version_and_every_outcome_are_there_from_the_start(self):
        from vcluster_argocd_enroller import __version__

        assert metric("info", version=__version__) == 1
        for outcome in ("created", "updated", "unchanged", "opted_out", "failed"):
            assert metric("syncs_total", outcome=outcome) is not None
        for reason in ("deleted", "opted_out", "orphaned"):
            assert metric("enrollments_removed_total", reason=reason) is not None

    @pytest.mark.parametrize(
        "existing,answer,outcome",
        [(False, "2", "created"), (True, "2", "updated"), (True, "1", "unchanged")],
        ids=["created", "updated", "unchanged"],
    )
    def test_syncs_are_counted_by_what_they_did(self, k8s_mocked, existing, answer, outcome):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core, {VC_SECRET: create_vcluster_secret(), **({ARGOCD_SECRET: enrolled_secret()} if existing else {})}
        )
        mock_core.patch_namespaced_secret.return_value = api_secret(resource_version=answer)
        before = metric("syncs_total", outcome=outcome)

        operator.vcluster_resync(**handler_args())

        assert metric("syncs_total", outcome=outcome) == before + 1

    def test_failed_sync_is_counted(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)
        before = metric("syncs_total", outcome="failed")

        with pytest.raises(kopf.TemporaryError):
            operator.vcluster_created(**handler_args())

        assert metric("syncs_total", outcome="failed") == before + 1

    def test_opting_out_counts_the_sync_and_the_secrets_removed(self, k8s_mocked):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(
            mock_core, {ARGOCD_SECRET: enrolled_secret(), ("argocd", "vcluster-test-cluster"): legacy_secret()}
        )
        syncs = metric("syncs_total", outcome="opted_out")
        removed = metric("enrollments_removed_total", reason="opted_out")

        operator.vcluster_updated(**handler_args(annotations={"vcluster-argocd-enroller/enroll": "false"}))

        assert metric("syncs_total", outcome="opted_out") == syncs + 1
        assert metric("enrollments_removed_total", reason="opted_out") == removed + 2

    @pytest.mark.parametrize("enrolled,counted", [(True, 1), (False, 0)])
    def test_deleted_vcluster_counts_the_secrets_removed(self, k8s_mocked, enrolled, counted):
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked
        serve_secrets(mock_core, {ARGOCD_SECRET: enrolled_secret()} if enrolled else {})
        before = metric("enrollments_removed_total", reason="deleted")

        operator.vcluster_deleted(**handler_args())

        assert metric("enrollments_removed_total", reason="deleted") == before + counted

    def test_sweep_reports_what_it_found(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        TestOrphanSweep.enrollments(mock_core, ("team-a", "alive"), ("team-a", "gone"), ("team-b", "new-gone"))
        mock_apps.read_namespaced_stateful_set.side_effect = lambda name, namespace: (
            workload() if name == "alive" else (_ for _ in ()).throw(ApiException(status=404))
        )
        before = metric("enrollments_removed_total", reason="orphaned")

        sweep_orphans(mock_core, mock_apps, suspects={"uid-team-a-gone"})

        assert metric("enrollments") == 2
        assert metric("orphan_suspects") == 1
        assert metric("enrollments_removed_total", reason="orphaned") == before + 1
        assert metric("last_sweep_timestamp_seconds") == pytest.approx(datetime.now(UTC).timestamp(), abs=60)

    def test_failed_sweep_leaves_the_time_of_the_last_one(self, k8s_mocked):
        from vcluster_argocd_enroller.operator import sweep_orphans

        mock_core, mock_apps = k8s_mocked
        TestOrphanSweep.enrollments(mock_core)
        sweep_orphans(mock_core, mock_apps)
        last = metric("last_sweep_timestamp_seconds")
        mock_core.list_namespaced_secret.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            sweep_orphans(mock_core, mock_apps)

        assert metric("last_sweep_timestamp_seconds") == last


class TestConfiguration:
    """Test how the operator reads its settings and finds the cluster."""

    @pytest.mark.parametrize(
        "value,expected",
        [(None, "300"), ("", "300"), ("  ", "300"), ("60", "60"), (" 60 ", "60")],
        ids=["unset", "empty", "blank", "set", "padded"],
    )
    def test_empty_setting_falls_back_to_the_default(self, monkeypatch, value, expected):
        """The chart always sets these variables, so an emptied value arrives as an empty string."""
        from vcluster_argocd_enroller.operator import _env

        if value is None:
            monkeypatch.delenv("RESYNC_INTERVAL", raising=False)
        else:
            monkeypatch.setenv("RESYNC_INTERVAL", value)

        assert _env("RESYNC_INTERVAL", "300") == expected

    @pytest.mark.parametrize("handler", ["vcluster_created", "vcluster_resync", "vcluster_deleted"])
    def test_unconfigurable_client_is_retried_not_given_up_on(self, monkeypatch, handler):
        """A PermanentError would end the re-sync timer, and must never come out of the delete handler."""
        from kubernetes import config

        from vcluster_argocd_enroller import operator

        def no_config():
            raise config.ConfigException("no configuration")

        monkeypatch.setattr(config, "load_incluster_config", no_config)
        monkeypatch.setattr(config, "load_kube_config", no_config)

        with pytest.raises(kopf.TemporaryError) as exc_info:
            getattr(operator, handler)(**handler_args())

        assert not isinstance(exc_info.value, kopf.PermanentError)
        assert operator._k8s_configured is False


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
