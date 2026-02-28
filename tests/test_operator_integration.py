#!/usr/bin/env python3
"""
Integration tests for vCluster ArgoCD Enroller operator.
"""

import base64
from unittest.mock import Mock

import kopf
import pytest
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


def create_vcluster_secret(name="test-cluster", namespace="vcluster-test"):
    """Create a mock vCluster secret with kubeconfig data."""
    return Mock(
        data={
            "certificate-authority": base64.b64encode(b"fake-ca-data").decode("utf-8"),
            "client-certificate": base64.b64encode(b"fake-cert-data").decode("utf-8"),
            "client-key": base64.b64encode(b"fake-key-data").decode("utf-8"),
        }
    )


class TestOperatorHandlers:
    """Test the operator handlers."""

    def test_vcluster_created_handler(self, k8s_mocked):
        """Test that vcluster creation triggers ArgoCD enrollment."""
        from vcluster_argocd_enroller import operator

        mock_core, mock_apps = k8s_mocked

        # Setup mock to return vcluster secret
        vcluster_secret = create_vcluster_secret()
        mock_core.read_namespaced_secret.return_value = vcluster_secret

        # Create StatefulSet resource
        statefulset = create_vcluster_statefulset()

        result = operator.vcluster_created(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
            meta=statefulset["metadata"],
            spec=statefulset["spec"],
        )

        # Verify secret was read
        mock_core.read_namespaced_secret.assert_called_once_with(name="vc-test-cluster", namespace="vcluster-test")

        # Verify ArgoCD secret was created
        mock_core.create_namespaced_secret.assert_called_once()
        call_args = mock_core.create_namespaced_secret.call_args

        assert call_args[0][0] == "argocd"  # namespace
        secret_body = call_args[0][1]

        # Verify secret structure
        assert secret_body["metadata"]["name"] == "vcluster-test-cluster"
        assert secret_body["metadata"]["namespace"] == "argocd"
        assert "argocd.argoproj.io/secret-type" in secret_body["metadata"]["labels"]
        assert secret_body["metadata"]["labels"]["argocd.argoproj.io/secret-type"] == "cluster"
        assert secret_body["metadata"]["labels"]["vcluster-operator"] == "true"

        # Verify secret data
        assert "name" in secret_body["data"]
        assert "server" in secret_body["data"]
        assert "config" in secret_body["data"]

        assert result == {"status": "Success"}

    def test_vcluster_created_idempotent_on_conflict(self, k8s_mocked):
        """Test that 409 Conflict on create falls back to replace."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        vcluster_secret = create_vcluster_secret()
        mock_core.read_namespaced_secret.return_value = vcluster_secret
        mock_core.create_namespaced_secret.side_effect = ApiException(status=409)

        statefulset = create_vcluster_statefulset()

        result = operator.vcluster_created(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
            meta=statefulset["metadata"],
            spec=statefulset["spec"],
        )

        # Should have fallen back to replace
        mock_core.replace_namespaced_secret.assert_called_once()
        assert result == {"status": "Success"}

    def test_vcluster_updated_handler(self, k8s_mocked):
        """Test that vcluster update triggers ArgoCD re-enrollment."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        vcluster_secret = create_vcluster_secret()
        mock_core.read_namespaced_secret.return_value = vcluster_secret

        statefulset = create_vcluster_statefulset()

        result = operator.vcluster_updated(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
            meta=statefulset["metadata"],
            spec=statefulset["spec"],
        )

        mock_core.create_namespaced_secret.assert_called_once()
        assert result == {"status": "Success"}

    def test_vcluster_deleted_handler(self, k8s_mocked):
        """Test that vcluster deletion triggers ArgoCD cleanup."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        statefulset = create_vcluster_statefulset()

        result = operator.vcluster_deleted(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
        )

        # Verify ArgoCD secret was deleted
        mock_core.delete_namespaced_secret.assert_called_once_with(name="vcluster-test-cluster", namespace="argocd")
        assert result == {"status": "Success"}

    def test_vcluster_with_prefix_name(self, k8s_mocked):
        """Test handling of vcluster with 'vcluster-' prefix in StatefulSet name."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        vcluster_secret = create_vcluster_secret()
        mock_core.read_namespaced_secret.return_value = vcluster_secret

        statefulset = create_vcluster_statefulset(name="vcluster-my-cluster")

        operator.vcluster_created(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
            meta=statefulset["metadata"],
            spec=statefulset["spec"],
        )

        # Should look for secret without the prefix
        mock_core.read_namespaced_secret.assert_called_once_with(name="vc-my-cluster", namespace="vcluster-test")

        # ArgoCD secret should be created with correct name
        call_args = mock_core.create_namespaced_secret.call_args
        secret_body = call_args[0][1]
        assert secret_body["metadata"]["name"] == "vcluster-my-cluster"

    def test_missing_vcluster_secret_temporary_error(self, k8s_mocked):
        """Test that missing vcluster secret causes temporary retry."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)

        statefulset = create_vcluster_statefulset()

        # Should raise TemporaryError for retry
        with pytest.raises(kopf.TemporaryError) as exc_info:
            operator.vcluster_created(
                name=statefulset["metadata"]["name"],
                namespace=statefulset["metadata"]["namespace"],
                meta=statefulset["metadata"],
                spec=statefulset["spec"],
            )

        assert "Failed to read vcluster secret" in str(exc_info.value)
        assert exc_info.value.delay == 60

    def test_argocd_secret_already_deleted(self, k8s_mocked):
        """Test graceful handling when ArgoCD secret is already deleted."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        mock_core.delete_namespaced_secret.side_effect = ApiException(status=404)

        statefulset = create_vcluster_statefulset()

        # Should handle 404 gracefully
        result = operator.vcluster_deleted(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
        )

        # Returns None on 404
        assert result is None

    def test_malformed_vcluster_secret(self, k8s_mocked):
        """Test handling of malformed vcluster secret."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        # Setup mock with malformed secret (missing required fields)
        bad_secret = Mock(data={"some-field": "value"})
        mock_core.read_namespaced_secret.return_value = bad_secret

        statefulset = create_vcluster_statefulset()

        # Should raise PermanentError for malformed secret
        with pytest.raises(kopf.PermanentError) as exc_info:
            operator.vcluster_created(
                name=statefulset["metadata"]["name"],
                namespace=statefulset["metadata"]["namespace"],
                meta=statefulset["metadata"],
                spec=statefulset["spec"],
            )

        assert "Failed to parse vcluster secret" in str(exc_info.value)

    def test_argocd_namespace_from_env(self, k8s_mocked):
        """Test that ARGOCD_NAMESPACE env var is respected."""
        from vcluster_argocd_enroller import operator

        mock_core, _ = k8s_mocked

        vcluster_secret = create_vcluster_secret()
        mock_core.read_namespaced_secret.return_value = vcluster_secret

        statefulset = create_vcluster_statefulset()

        # The default is "argocd"
        operator.vcluster_created(
            name=statefulset["metadata"]["name"],
            namespace=statefulset["metadata"]["namespace"],
            meta=statefulset["metadata"],
            spec=statefulset["spec"],
        )

        call_args = mock_core.create_namespaced_secret.call_args
        assert call_args[0][0] == "argocd"


class TestUtilityFunctions:
    """Test utility functions."""

    def test_vc_name_without_prefix(self):
        """Test vcluster name extraction without prefix."""
        from vcluster_argocd_enroller.operator import vc_name

        assert vc_name("my-cluster") == "my-cluster"
        assert vc_name("test-123") == "test-123"

    def test_vc_name_with_prefix(self):
        """Test vcluster name extraction with vcluster- prefix."""
        from vcluster_argocd_enroller.operator import vc_name

        assert vc_name("vcluster-my-cluster") == "my-cluster"
        assert vc_name("vcluster-test-123") == "test-123"

    def test_ar_secret_name(self):
        """Test ArgoCD secret name generation."""
        from vcluster_argocd_enroller.operator import ar_secret_name

        assert ar_secret_name("my-cluster") == "vcluster-my-cluster"
        assert ar_secret_name("test") == "vcluster-test"

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
