import base64
import json
import logging
import os
from typing import cast

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

ARGOCD_NAMESPACE = os.getenv("ARGOCD_NAMESPACE", "argocd")
# Where ArgoCD reaches a vCluster. The certificate vcluster serves covers <name> and <name>.<namespace>,
# but not <name>.<namespace>.svc.cluster.local, unless that is added to its controlPlane.proxy.extraSANs.
SERVER_URL = os.getenv("SERVER_URL", "https://{name}.{namespace}")
# What enrollments pointed at before SERVER_URL existed.
LEGACY_SERVER_URL = "https://{name}.{namespace}.svc.cluster.local"
# Seconds between re-syncs of each vCluster: vcluster rotates vc-<name> without touching the StatefulSet.
RESYNC_INTERVAL = float(os.getenv("RESYNC_INTERVAL", "300"))
# Attempts at removing the ArgoCD secret before a deleted vCluster is let go anyway.
DELETE_RETRIES = 10

OPERATOR_LABEL = "vcluster-operator"
NAMESPACE_LABEL = "vcluster-argocd-enroller/namespace"
NAME_LABEL = "vcluster-argocd-enroller/name"

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
    """Return the vCluster name for a StatefulSet.

    vcluster names the StatefulSet, the Service and the `vc-<name>` secret after the Helm release,
    so the name is used as-is (a release called `vcluster-foo` owns `vc-vcluster-foo`).
    """
    return statefulset_name


def argocd_cluster_name(namespace: str, vcluster_name: str) -> str:
    """Return the name ArgoCD knows the vCluster by (an Application's `destination.name`)."""
    return f"{namespace}-{vcluster_name}"


def ar_secret_name(namespace: str, vcluster_name: str) -> str:
    """Return the ArgoCD secret name for a given vCluster."""
    return f"vcluster-{argocd_cluster_name(namespace, vcluster_name)}"


def legacy_ar_secret_name(vcluster_name: str) -> str:
    """Return the ArgoCD secret name used before the namespace became part of it."""
    return f"vcluster-{vcluster_name}"


def server_url(namespace: str, vcluster_name: str, template: str | None = None) -> str:
    """Return the in-cluster URL of a vCluster's API server."""
    return (template or SERVER_URL).replace("{name}", vcluster_name).replace("{namespace}", namespace)


def decode(d: str | bytes) -> str:
    """Base64-decode a string."""
    return base64.b64decode(d).decode("utf-8")


def encode(s: str) -> str:
    """Base64-encode a string."""
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def owns(secret: client.V1Secret, namespace: str, vcluster_name: str) -> bool:
    """Tell whether an ArgoCD cluster secret is this operator's enrollment of the given vCluster.

    Secrets written before the namespace/name labels existed are matched by the server they point at.
    """
    labels = (secret.metadata.labels if secret.metadata else None) or {}
    if labels.get(OPERATOR_LABEL) != "true":
        return False
    if NAMESPACE_LABEL in labels or NAME_LABEL in labels:
        return labels.get(NAMESPACE_LABEL) == namespace and labels.get(NAME_LABEL) == vcluster_name
    return (secret.data or {}).get("server") == encode(server_url(namespace, vcluster_name, LEGACY_SERVER_URL))


def build_argocd_secret(vcluster_name: str, namespace: str, vc_secret: client.V1Secret) -> dict:
    """Build the ArgoCD cluster secret body."""
    argocd_secret_name = ar_secret_name(namespace, vcluster_name)
    vc_data = vc_secret.data or {}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": argocd_secret_name,
            "namespace": ARGOCD_NAMESPACE,
            "labels": {
                "argocd.argoproj.io/secret-type": "cluster",
                OPERATOR_LABEL: "true",
                NAMESPACE_LABEL: namespace,
                NAME_LABEL: vcluster_name,
            },
        },
        "data": {
            "name": encode(argocd_cluster_name(namespace, vcluster_name)),
            "server": encode(server_url(namespace, vcluster_name)),
            "config": encode(
                json.dumps(
                    {
                        "tlsClientConfig": {
                            "caData": vc_data["certificate-authority"],
                            "certData": vc_data["client-certificate"],
                            "keyData": vc_data["client-key"],
                            "insecure": False,
                        }
                    }
                )
            ),
        },
    }


def _read_argocd_secret(core_v1_api: client.CoreV1Api, name: str) -> client.V1Secret | None:
    """Read a secret from the ArgoCD namespace, or None when there is none."""
    try:
        return core_v1_api.read_namespaced_secret(name=name, namespace=ARGOCD_NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _in_sync(existing: client.V1Secret, desired: dict) -> bool:
    """Tell whether an ArgoCD secret already carries the desired data and labels."""
    labels = (existing.metadata.labels if existing.metadata else None) or {}
    return existing.data == desired["data"] and desired["metadata"]["labels"].items() <= labels.items()


def _delete_if_owned(core_v1_api: client.CoreV1Api, name: str, namespace: str, vcluster_name: str) -> bool:
    """Delete an ArgoCD secret unless it is missing or belongs to something else."""
    secret = _read_argocd_secret(core_v1_api, name)
    if secret is None:
        return False
    if not owns(secret, namespace, vcluster_name):
        logger.warning(f"Leaving ArgoCD secret {name} alone: it does not belong to {namespace}/{vcluster_name}")
        return False
    try:
        core_v1_api.delete_namespaced_secret(name=name, namespace=ARGOCD_NAMESPACE)
    except ApiException as e:
        if e.status != 404:
            raise
    return True


def handle_vcluster_enrollment(statefulset_name: str, namespace: str, **kwargs):
    """Create or update the ArgoCD cluster secret for a vCluster."""
    core_v1_api, _ = _ensure_k8s()

    vcluster_name = vc_name(statefulset_name)
    vcluster_secret_name = f"vc-{vcluster_name}"
    logger.debug(f"Syncing vcluster {namespace}/{vcluster_name} from secret {vcluster_secret_name}")

    try:
        vc_secret = core_v1_api.read_namespaced_secret(name=vcluster_secret_name, namespace=namespace)
    except ApiException as e:
        # 404 is expected right after the StatefulSet appears: vcluster writes vc-<name> a little later.
        log = logger.warning if e.status == 404 else logger.error
        log(f"API error reading vcluster secret {namespace}/{vcluster_secret_name}: {e.status} {e.reason}")
        raise kopf.TemporaryError(
            f"Failed to read vcluster secret {namespace}/{vcluster_secret_name}: {e.status} {e.reason}",
            delay=60,
        )

    try:
        secret_body = build_argocd_secret(vcluster_name, namespace, vc_secret)
    except KeyError as e:
        # Retried rather than given up on: the secret can be completed (or fixed) later.
        logger.warning(f"vcluster secret {namespace}/{vcluster_secret_name} has no {e} entry")
        raise kopf.TemporaryError(
            f"Failed to parse vcluster secret {namespace}/{vcluster_secret_name}: no {e} entry", delay=60
        )

    argocd_secret_name = ar_secret_name(namespace, vcluster_name)

    try:
        existing = _read_argocd_secret(core_v1_api, argocd_secret_name)
        if existing is None:
            core_v1_api.create_namespaced_secret(ARGOCD_NAMESPACE, cast(client.V1Secret, secret_body))
            logger.info(f"Created ArgoCD cluster secret {argocd_secret_name} for vcluster {namespace}/{vcluster_name}")
        elif not owns(existing, namespace, vcluster_name):
            logger.error(f"ArgoCD secret {argocd_secret_name} exists but is not {namespace}/{vcluster_name}'s")
            raise kopf.TemporaryError(
                f"ArgoCD secret {argocd_secret_name} belongs to something else; not overwriting it", delay=60
            )
        elif _in_sync(existing, secret_body):
            logger.debug(f"ArgoCD cluster secret {argocd_secret_name} is up to date")
        else:
            core_v1_api.replace_namespaced_secret(
                argocd_secret_name, ARGOCD_NAMESPACE, cast(client.V1Secret, secret_body)
            )
            logger.info(f"Updated ArgoCD cluster secret {argocd_secret_name} for vcluster {namespace}/{vcluster_name}")

        # Enrollments made before the namespace was part of the name move over to the new one.
        legacy_secret_name = legacy_ar_secret_name(vcluster_name)
        if _delete_if_owned(core_v1_api, legacy_secret_name, namespace, vcluster_name):
            logger.info(f"Removed legacy ArgoCD cluster secret {legacy_secret_name}, replaced by {argocd_secret_name}")
    except ApiException as e:
        logger.error(f"API error syncing ArgoCD secret {argocd_secret_name}: {e.status} {e.reason}")
        raise kopf.TemporaryError(f"Failed to sync ArgoCD secret {argocd_secret_name}: {e.status} {e.reason}", delay=60)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings."""
    settings.posting.enabled = False
    # Keep kopf's progress/diffbase on annotations: the StatefulSet controller owns .status and drops
    # kopf's patches there, so retries would not survive an operator restart.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage()
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage()
    settings.watching.connect_timeout = 1 * 60
    settings.watching.server_timeout = 10 * 60


@kopf.on.create("statefulsets", labels={"app": "vcluster"})
def vcluster_created(name, namespace, meta, spec, **kwargs):
    """Handle vcluster StatefulSet creation."""
    logger.info(f"Detected new vcluster StatefulSet: {namespace}/{name}")
    # No return value: kopf would persist it into the StatefulSet status, which the controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.resume("statefulsets", labels={"app": "vcluster"})
def vcluster_resumed(name, namespace, meta, spec, **kwargs):
    """Handle existing vcluster StatefulSets when operator starts."""
    logger.info(f"Detected existing vcluster StatefulSet: {namespace}/{name}")
    # No return value: kopf would persist it into the StatefulSet status, which the controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.update("statefulsets", labels={"app": "vcluster"})
def vcluster_updated(name, namespace, meta, spec, **kwargs):
    """Handle vcluster StatefulSet updates."""
    logger.info(f"Detected vcluster StatefulSet update: {namespace}/{name}")
    # No return value: kopf would persist it into the StatefulSet status, which the controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.timer("statefulsets", labels={"app": "vcluster"}, interval=RESYNC_INTERVAL, initial_delay=RESYNC_INTERVAL)
def vcluster_resync(name, namespace, **kwargs):
    """Re-sync periodically, picking up rotated credentials and restoring a changed or removed ArgoCD secret."""
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.delete("statefulsets", labels={"app": "vcluster"}, retries=DELETE_RETRIES)
def vcluster_deleted(name, namespace, **kwargs):
    """Handle vcluster StatefulSet deletion.

    Returns nothing: kopf would persist a return value into the StatefulSet status, which is being dropped.
    """
    core_v1_api, _ = _ensure_k8s()

    logger.info(f"Detected vcluster StatefulSet deletion: {namespace}/{name}")

    vcluster_name = vc_name(name)

    try:
        for secret_name in (ar_secret_name(namespace, vcluster_name), legacy_ar_secret_name(vcluster_name)):
            if _delete_if_owned(core_v1_api, secret_name, namespace, vcluster_name):
                logger.info(f"Deleted ArgoCD cluster secret {secret_name} for {namespace}/{vcluster_name}")
    except ApiException as e:
        logger.error(f"Failed to delete ArgoCD cluster secret for {namespace}/{vcluster_name}: {e.status} {e.reason}")
        # Never a PermanentError here. kopf retries DELETE_RETRIES times, then releases the finalizer anyway.
        raise kopf.TemporaryError(f"Failed to delete ArgoCD cluster secret: {e.status} {e.reason}", delay=30)
