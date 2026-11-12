import base64
import json
import logging
import os
import re
from collections.abc import Mapping
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
# Seconds between re-syncs of each vCluster: vcluster rotates vc-<name> without touching its workload.
RESYNC_INTERVAL = float(os.getenv("RESYNC_INTERVAL", "300"))
# Attempts at removing the ArgoCD secret before a deleted vCluster is let go anyway.
DELETE_RETRIES = 10

# vcluster runs its control plane as a StatefulSet, or as a Deployment when it keeps no state of its own
# (deployed or external etcd, an external database, persistence turned off). Both carry this label.
VCLUSTER_LABELS = {"app": "vcluster"}
# The watched resources, by plural name. They are named with their API group in the handlers below:
# a bare "statefulsets" would also match the StatefulSets of other groups (OpenKruise's, for one).
WORKLOAD_KINDS = {"statefulsets": "StatefulSet", "deployments": "Deployment"}

OPERATOR_LABEL = "vcluster-operator"
NAMESPACE_LABEL = "vcluster-argocd-enroller/namespace"
NAME_LABEL = "vcluster-argocd-enroller/name"
# The labels every enrollment carries. A vCluster cannot ask for its own value of these.
RESERVED_LABELS = frozenset({"argocd.argoproj.io/secret-type", OPERATOR_LABEL, NAMESPACE_LABEL, NAME_LABEL})

# Annotations on a vCluster's workload that say how it is enrolled.
# "false" keeps the vCluster out of ArgoCD.
ENROLL_ANNOTATION = "vcluster-argocd-enroller/enroll"
# "key=value,key=value": labels for the ArgoCD secret, which ApplicationSet cluster generators select on.
LABELS_ANNOTATION = "vcluster-argocd-enroller/labels"
# The ArgoCD project the cluster is scoped to (the secret's `project` field).
PROJECT_ANNOTATION = "vcluster-argocd-enroller/project"
# On the ArgoCD secret: the keys of the labels copied from the workload, so that a dropped one can be removed.
COPIED_LABELS_ANNOTATION = "vcluster-argocd-enroller/copied-labels"

# What Kubernetes accepts as a label key's prefix, and as its name or a label value.
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_LABEL_NAME = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")

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


def vc_name(workload_name: str) -> str:
    """Return the vCluster name for its StatefulSet or Deployment.

    vcluster names the workload, the Service and the `vc-<name>` secret after the Helm release,
    so the name is used as-is (a release called `vcluster-foo` owns `vc-vcluster-foo`).
    """
    return workload_name


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


def parse_label(entry: str) -> tuple[str, str]:
    """Split a `key=value` label, raising ValueError unless it is one the ArgoCD secret can carry."""
    key, equals, value = (part.strip() for part in entry.partition("="))
    if not equals:
        raise ValueError(f"{entry.strip()!r} is not key=value")
    prefix, slash, name = key.rpartition("/")
    valid_prefix = not slash or (len(prefix) <= 253 and _DNS_SUBDOMAIN.fullmatch(prefix))
    if not (valid_prefix and _LABEL_NAME.fullmatch(name)):
        raise ValueError(f"{key!r} is not a valid label key")
    if value and not _LABEL_NAME.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid label value")
    if key in RESERVED_LABELS:
        raise ValueError(f"{key!r} is set by the operator")
    return key, value


def opted_out(annotations: Mapping[str, str]) -> bool:
    """Tell whether a vCluster's workload asks to be kept out of ArgoCD."""
    return annotations.get(ENROLL_ANNOTATION, "").strip().lower() == "false"


def wanted_labels(annotations: Mapping[str, str], vcluster: str) -> dict[str, str]:
    """Return the labels a vCluster's workload asks for on its ArgoCD secret.

    An entry that cannot be a label is left out and reported: the vCluster is still enrolled without it.
    """
    labels = {}
    for entry in filter(str.strip, annotations.get(LABELS_ANNOTATION, "").split(",")):
        try:
            key, value = parse_label(entry)
        except ValueError as e:
            logger.warning(f"Ignoring a label asked for by {vcluster} in {LABELS_ANNOTATION}: {e}")
        else:
            labels[key] = value
    return labels


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


def build_argocd_secret(
    vcluster_name: str,
    namespace: str,
    vc_secret: client.V1Secret,
    labels: Mapping[str, str] | None = None,
    project: str | None = None,
) -> dict:
    """Build the ArgoCD cluster secret body.

    `labels` are added to the operator's own, and `project` scopes the cluster to an ArgoCD project.
    """
    argocd_secret_name = ar_secret_name(namespace, vcluster_name)
    vc_data = vc_secret.data or {}
    copied = {key: value for key, value in (labels or {}).items() if key not in RESERVED_LABELS}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": argocd_secret_name,
            "namespace": ARGOCD_NAMESPACE,
            "labels": {
                **copied,
                "argocd.argoproj.io/secret-type": "cluster",
                OPERATOR_LABEL: "true",
                NAMESPACE_LABEL: namespace,
                NAME_LABEL: vcluster_name,
            },
            **({"annotations": {COPIED_LABELS_ANNOTATION: ",".join(sorted(copied))}} if copied else {}),
        },
        "data": {
            **({"project": encode(project)} if project else {}),
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


def _keeping_foreign_metadata(existing: client.V1Secret, desired: dict) -> dict:
    """Return the desired ArgoCD secret with the labels and annotations something else put on the existing one.

    ArgoCD labels cluster secrets itself when asked to, and so may anyone with kubectl. The labels this
    operator copied from the workload earlier are not carried over: one that is no longer asked for goes.
    """
    meta = existing.metadata
    labels = dict((meta.labels if meta else None) or {})
    annotations = dict((meta.annotations if meta else None) or {})
    for key in filter(None, annotations.pop(COPIED_LABELS_ANNOTATION, "").split(",")):
        labels.pop(key, None)
    labels.update(desired["metadata"]["labels"])
    annotations.update(desired["metadata"].get("annotations", {}))
    return {**desired, "metadata": {**desired["metadata"], "labels": labels, "annotations": annotations}}


def _in_sync(existing: client.V1Secret, desired: dict) -> bool:
    """Tell whether an ArgoCD secret already carries the desired data, labels and annotations."""
    meta = existing.metadata
    return (
        existing.data == desired["data"]
        and ((meta.labels if meta else None) or {}) == desired["metadata"]["labels"]
        and ((meta.annotations if meta else None) or {}) == desired["metadata"]["annotations"]
    )


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


def _remove_enrollment(core_v1_api: client.CoreV1Api, namespace: str, vcluster_name: str) -> None:
    """Delete the ArgoCD secrets this operator wrote for a vCluster, under the current name and the 1.x one."""
    for secret_name in (ar_secret_name(namespace, vcluster_name), legacy_ar_secret_name(vcluster_name)):
        if _delete_if_owned(core_v1_api, secret_name, namespace, vcluster_name):
            logger.info(f"Deleted ArgoCD cluster secret {secret_name} for {namespace}/{vcluster_name}")


def _lives_on(apps_v1_api: client.AppsV1Api, deleted_plural: str, name: str, namespace: str) -> bool:
    """Tell whether a vCluster whose workload is being deleted still runs as the other kind of workload.

    Changing a vCluster's backing store or persistence replaces its StatefulSet with a Deployment of the
    same name, or the other way round. The replacement keeps the enrollment.
    """
    try:
        other: client.V1Deployment | client.V1StatefulSet
        if deleted_plural == "statefulsets":
            other = apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
        else:
            other = apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    meta = other.metadata
    if meta is None or meta.deletion_timestamp is not None:
        return False
    return VCLUSTER_LABELS.items() <= (meta.labels or {}).items()


def handle_vcluster_enrollment(
    workload_name: str, namespace: str, annotations: Mapping[str, str] | None = None, **kwargs
):
    """Create or update the ArgoCD cluster secret for a vCluster, or remove it when the vCluster opts out.

    `annotations` are those of the vCluster's workload, which say how it wants to be enrolled.
    """
    core_v1_api, _ = _ensure_k8s()

    vcluster_name = vc_name(workload_name)
    annotations = annotations or {}

    if opted_out(annotations):
        logger.debug(f"Not enrolling {namespace}/{vcluster_name}: its workload is annotated {ENROLL_ANNOTATION}=false")
        try:
            _remove_enrollment(core_v1_api, namespace, vcluster_name)
        except ApiException as e:
            logger.error(f"API error removing the enrollment of {namespace}/{vcluster_name}: {e.status} {e.reason}")
            raise kopf.TemporaryError(
                f"Failed to remove the enrollment of {namespace}/{vcluster_name}: {e.status} {e.reason}", delay=60
            )
        return
    if annotations.get(ENROLL_ANNOTATION, "true").strip().lower() != "true":
        logger.warning(
            f"Enrolling {namespace}/{vcluster_name}: its {ENROLL_ANNOTATION} annotation is neither true nor false"
        )

    vcluster_secret_name = f"vc-{vcluster_name}"
    logger.debug(f"Syncing vcluster {namespace}/{vcluster_name} from secret {vcluster_secret_name}")

    try:
        vc_secret = core_v1_api.read_namespaced_secret(name=vcluster_secret_name, namespace=namespace)
    except ApiException as e:
        # 404 is expected right after the workload appears: vcluster writes vc-<name> a little later.
        log = logger.warning if e.status == 404 else logger.error
        log(f"API error reading vcluster secret {namespace}/{vcluster_secret_name}: {e.status} {e.reason}")
        raise kopf.TemporaryError(
            f"Failed to read vcluster secret {namespace}/{vcluster_secret_name}: {e.status} {e.reason}",
            delay=60,
        )

    try:
        secret_body = build_argocd_secret(
            vcluster_name,
            namespace,
            vc_secret,
            labels=wanted_labels(annotations, f"{namespace}/{vcluster_name}"),
            project=annotations.get(PROJECT_ANNOTATION, "").strip() or None,
        )
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
        elif _in_sync(existing, secret_body := _keeping_foreign_metadata(existing, secret_body)):
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
    # Keep kopf's progress/diffbase on annotations: the workload's controller owns .status and drops
    # kopf's patches there, so retries would not survive an operator restart.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage()
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage()
    settings.watching.connect_timeout = 1 * 60
    settings.watching.server_timeout = 10 * 60


@kopf.on.create("statefulsets.apps", labels=VCLUSTER_LABELS)
@kopf.on.create("deployments.apps", labels=VCLUSTER_LABELS)
def vcluster_created(name, namespace, meta, spec, resource, **kwargs):
    """Handle vcluster workload creation."""
    logger.info(f"Detected new vcluster {WORKLOAD_KINDS[resource.plural]}: {namespace}/{name}")
    # No return value: kopf would persist it into the workload's status, which its controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.resume("statefulsets.apps", labels=VCLUSTER_LABELS)
@kopf.on.resume("deployments.apps", labels=VCLUSTER_LABELS)
def vcluster_resumed(name, namespace, meta, spec, resource, **kwargs):
    """Handle existing vcluster workloads when operator starts."""
    logger.info(f"Detected existing vcluster {WORKLOAD_KINDS[resource.plural]}: {namespace}/{name}")
    # No return value: kopf would persist it into the workload's status, which its controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.update("statefulsets.apps", labels=VCLUSTER_LABELS)
@kopf.on.update("deployments.apps", labels=VCLUSTER_LABELS)
def vcluster_updated(name, namespace, meta, spec, resource, **kwargs):
    """Handle vcluster workload updates."""
    logger.info(f"Detected vcluster {WORKLOAD_KINDS[resource.plural]} update: {namespace}/{name}")
    # No return value: kopf would persist it into the workload's status, which its controller drops.
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.timer("statefulsets.apps", labels=VCLUSTER_LABELS, interval=RESYNC_INTERVAL, initial_delay=RESYNC_INTERVAL)
@kopf.timer("deployments.apps", labels=VCLUSTER_LABELS, interval=RESYNC_INTERVAL, initial_delay=RESYNC_INTERVAL)
def vcluster_resync(name, namespace, **kwargs):
    """Re-sync periodically, picking up rotated credentials and restoring a changed or removed ArgoCD secret."""
    handle_vcluster_enrollment(name, namespace, **kwargs)


@kopf.on.delete("statefulsets.apps", labels=VCLUSTER_LABELS, retries=DELETE_RETRIES)
@kopf.on.delete("deployments.apps", labels=VCLUSTER_LABELS, retries=DELETE_RETRIES)
def vcluster_deleted(name, namespace, resource, **kwargs):
    """Handle vcluster workload deletion.

    Returns nothing: kopf would persist a return value into the workload's status, which is being dropped.
    """
    core_v1_api, apps_v1_api = _ensure_k8s()

    kind = WORKLOAD_KINDS[resource.plural]
    logger.info(f"Detected vcluster {kind} deletion: {namespace}/{name}")

    vcluster_name = vc_name(name)

    try:
        if _lives_on(apps_v1_api, resource.plural, name, namespace):
            logger.info(f"Keeping the enrollment of {namespace}/{vcluster_name}: only its {kind} is replaced")
            return
        _remove_enrollment(core_v1_api, namespace, vcluster_name)
    except ApiException as e:
        logger.error(f"Failed to delete ArgoCD cluster secret for {namespace}/{vcluster_name}: {e.status} {e.reason}")
        # Never a PermanentError here. kopf retries DELETE_RETRIES times, then releases the finalizer anyway.
        raise kopf.TemporaryError(f"Failed to delete ArgoCD cluster secret: {e.status} {e.reason}", delay=30)
