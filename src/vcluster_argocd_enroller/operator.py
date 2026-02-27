import base64
import json
import logging
import os

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

ARGOCD_NAMESPACE = os.getenv("ARGOCD_NAMESPACE", "argocd")

logger = logging.getLogger(__name__)

_k8s_configured = False
_core_v1_api: client.CoreV1Api | None = None
_apps_v1_api: client.AppsV1Api | None = None


def _ensure_k8s() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Lazy-init Kubernetes clients on first use."""
    global _k8s_configured, _core_v1_api, _apps_v1_api
    if not _k8s_configured:
        try:
            if os.getenv("KUBERNETES_SERVICE_HOST"):
                logger.debug("Running in cluster, using in-cluster config")
                config.load_incluster_config()
            else:
                logger.debug("Running locally, using kubeconfig")
                config.load_kube_config()
        except config.ConfigException as e:
            logger.error(f"Failed to load kubernetes config: {e}")
            raise kopf.PermanentError("Could not configure kubernetes client")
        _core_v1_api = client.CoreV1Api()
        _apps_v1_api = client.AppsV1Api()
        _k8s_configured = True
    assert _core_v1_api is not None
    assert _apps_v1_api is not None
    return _core_v1_api, _apps_v1_api


def vc_name(statefulset_name: str) -> str:
    """Extract the vCluster name from a StatefulSet name."""
    if statefulset_name.startswith("vcluster-"):
        return statefulset_name.replace("vcluster-", "", 1)
    return statefulset_name


def ar_secret_name(vcluster_name: str) -> str:
    """Return the ArgoCD secret name for a given vCluster."""
    return f"vcluster-{vcluster_name}"


def decode(d: str) -> str:
    """Base64-decode a string."""
    return base64.b64decode(d).decode("utf-8")


def encode(s: str) -> str:
    """Base64-encode a string."""
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def _build_argocd_secret(vcluster_name: str, namespace: str, vc_secret: client.V1Secret) -> dict:
    """Build the ArgoCD cluster secret body."""
    argocd_secret_name = ar_secret_name(vcluster_name)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": argocd_secret_name,
            "namespace": ARGOCD_NAMESPACE,
            "labels": {"argocd.argoproj.io/secret-type": "cluster", "vcluster-operator": "true"},
        },
        "data": {
            "name": encode(vcluster_name),
            "server": encode(f"https://{vcluster_name}.{namespace}.svc.cluster.local"),
            "config": encode(
                json.dumps(
                    {
                        "tlsClientConfig": {
                            "caData": vc_secret.data["certificate-authority"],
                            "certData": vc_secret.data["client-certificate"],
                            "keyData": vc_secret.data["client-key"],
                            "insecure": False,
                        }
                    }
                )
            ),
        },
    }


def handle_vcluster_enrollment(statefulset_name: str, namespace: str, **kwargs):
    """Create or update the ArgoCD cluster secret for a vCluster."""
    core_v1_api, _ = _ensure_k8s()

    logger.info(f"Processing vcluster enrollment for StatefulSet {namespace}/{statefulset_name}")

    vcluster_name = vc_name(statefulset_name)
    logger.info(f"Mapped StatefulSet {statefulset_name} to vcluster {vcluster_name}")

    vcluster_secret_name = f"vc-{vcluster_name}"
    logger.info(f"Reading vcluster secret {namespace}/{vcluster_secret_name}")

    try:
        vc_secret = core_v1_api.read_namespaced_secret(name=vcluster_secret_name, namespace=namespace)
    except ApiException as e:
        logger.error(f"API error reading vcluster secret {namespace}/{vcluster_secret_name}: {e}")
        raise kopf.TemporaryError(f"Failed to read vcluster secret {namespace}/{vcluster_secret_name}: {e}", delay=60)
    except Exception as e:
        logger.error(f"Failed to read vcluster secret {namespace}/{vcluster_secret_name}: {e}")
        raise kopf.PermanentError(f"Failed to read vcluster secret: {e}")

    try:
        secret_body = _build_argocd_secret(vcluster_name, namespace, vc_secret)
    except (KeyError, IndexError):
        logger.error(f"Improperly formed vcluster secret: {namespace}/{vcluster_secret_name}")
        raise kopf.PermanentError(f"Failed to parse vcluster secret: {namespace}/{vcluster_secret_name}")

    argocd_secret_name = ar_secret_name(vcluster_name)

    # Idempotent create-or-update
    try:
        core_v1_api.create_namespaced_secret(ARGOCD_NAMESPACE, secret_body)
        logger.info(f"Created ArgoCD cluster secret {argocd_secret_name} for vcluster {vcluster_name}")
    except ApiException as e:
        if e.status == 409:
            logger.info(f"ArgoCD secret {argocd_secret_name} already exists, updating")
            core_v1_api.replace_namespaced_secret(argocd_secret_name, ARGOCD_NAMESPACE, secret_body)
            logger.info(f"Updated ArgoCD cluster secret {argocd_secret_name} for vcluster {vcluster_name}")
        else:
            logger.error(f"API error creating ArgoCD secret {argocd_secret_name}: {e}")
            raise kopf.TemporaryError(f"Failed to create ArgoCD secret: {e}", delay=60)

    return {"status": "Success"}


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings."""
    settings.posting.enabled = False
    settings.watching.connect_timeout = 1 * 60
    settings.watching.server_timeout = 10 * 60


@kopf.on.create("statefulsets", labels={"app": "vcluster"})
def vcluster_created(name, namespace, meta, spec, **kwargs):
    """Handle vcluster StatefulSet creation."""
    logger.info(f"Detected new vcluster StatefulSet: {namespace}/{name}")
    return handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.resume("statefulsets", labels={"app": "vcluster"})
def vcluster_resumed(name, namespace, meta, spec, **kwargs):
    """Handle existing vcluster StatefulSets when operator starts."""
    logger.info(f"Detected existing vcluster StatefulSet: {namespace}/{name}")
    return handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.update("statefulsets", labels={"app": "vcluster"})
def vcluster_updated(name, namespace, meta, spec, **kwargs):
    """Handle vcluster StatefulSet updates (e.g. cert rotation)."""
    logger.info(f"Detected vcluster StatefulSet update: {namespace}/{name}")
    return handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.delete("statefulsets", labels={"app": "vcluster"})
def vcluster_deleted(name, namespace, **kwargs):
    """Handle vcluster StatefulSet deletion."""
    core_v1_api, _ = _ensure_k8s()

    logger.info(f"Detected vcluster StatefulSet deletion: {namespace}/{name}")

    vcluster_name = vc_name(name)
    argocd_secret_name = ar_secret_name(vcluster_name)

    logger.info(f"Deleting ArgoCD cluster secret {argocd_secret_name} for {namespace}/{vcluster_name}")

    try:
        core_v1_api.delete_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE)
        logger.info(f"Successfully deleted ArgoCD cluster secret {argocd_secret_name}")
        return {"status": "Success"}
    except ApiException as e:
        if e.status == 404:
            logger.info(f"ArgoCD secret {argocd_secret_name} not found, already deleted")
        else:
            logger.error(f"Failed to delete ArgoCD cluster secret {argocd_secret_name}: {e}")
            # Don't raise PermanentError on deletion - allow finalizer removal
            return {"status": "Failed", "message": str(e)}
    except Exception as e:
        logger.error(f"Failed to remove vcluster {namespace}/{vcluster_name} from ArgoCD: {e}")
        # Don't raise PermanentError on deletion - allow finalizer removal
        return {"status": "Failed", "message": str(e)}
