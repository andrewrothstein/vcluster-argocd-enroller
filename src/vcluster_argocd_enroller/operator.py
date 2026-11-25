import asyncio
import base64
import json
import logging
import os
import re
from collections.abc import Collection, Mapping
from fnmatch import fnmatchcase

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from . import metrics


def _env(name: str, default: str) -> str:
    """Read a setting from the environment. One that is set but empty counts as unset: the chart always sets these."""
    return os.getenv(name, "").strip() or default


def _env_list(name: str) -> tuple[str, ...]:
    """Read a comma-separated setting from the environment."""
    return tuple(filter(None, (entry.strip() for entry in os.getenv(name, "").split(","))))


ARGOCD_NAMESPACE = _env("ARGOCD_NAMESPACE", "argocd")
# Where ArgoCD reaches a vCluster. The certificate vcluster serves covers <name> and <name>.<namespace>,
# but not <name>.<namespace>.svc.cluster.local, unless that is added to its controlPlane.proxy.extraSANs.
SERVER_URL = _env("SERVER_URL", "https://{name}.{namespace}")
# What enrollments pointed at before SERVER_URL existed.
LEGACY_SERVER_URL = "https://{name}.{namespace}.svc.cluster.local"
# Seconds between re-syncs of each vCluster: vcluster rotates vc-<name> without touching its workload.
RESYNC_INTERVAL = float(_env("RESYNC_INTERVAL", "300"))
# The label keys and the projects a vCluster's workload may ask for, as patterns in which `*` matches anything.
# Empty allows them all. Whoever can edit a workload picks its annotations: these keep that to what is harmless.
ALLOWED_LABELS = _env_list("ALLOWED_LABELS")
ALLOWED_PROJECTS = _env_list("ALLOWED_PROJECTS")
# Whether enrollments whose vCluster is gone are removed. The delete handler does that as a vCluster goes, but
# not for one that went while the operator was down, lost its app=vcluster label, or outlasted DELETE_RETRIES.
SWEEP_ORPHANS = _env("SWEEP_ORPHANS", "true").lower() != "false"
# The namespaces whose vClusters this instance looks after, set by the CLI's --namespace. Empty for all of them.
WATCHED_NAMESPACES: tuple[str, ...] = ()
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

# ArgoCD secrets are written with server-side apply under this name. The API server keeps track of the fields
# applied, so one that is no longer applied (a label the workload dropped) goes, and nothing else is touched.
FIELD_MANAGER = "vcluster-argocd-enroller"
# The name under which the API server recorded the writes of 2.2 and earlier: the Python client's user agent.
LEGACY_FIELD_MANAGER = "OpenAPI-Generator"
# On ArgoCD secrets written by 2.2: the keys of the labels copied from the workload. Read once more, to hand
# those labels over to FIELD_MANAGER.
COPIED_LABELS_ANNOTATION = "vcluster-argocd-enroller/copied-labels"
APPLY_PATCH = "application/apply-patch+yaml"
MERGE_PATCH = "application/merge-patch+json"

# What Kubernetes accepts as a label key's prefix, and as its name or a label value.
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_LABEL_NAME = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")

logger = logging.getLogger(__name__)

_k8s_configured = False
_core_v1_api: client.CoreV1Api | None = None
_apps_v1_api: client.AppsV1Api | None = None
_sweep_task: asyncio.Task | None = None


class NotOwnedError(Exception):
    """An ArgoCD secret of the wanted name exists, and is not the enrollment of the vCluster asking for it."""


def load_k8s_config() -> None:
    """Configure the Kubernetes client from the pod's service account, or from the kubeconfig outside a cluster."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _ensure_k8s() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Lazy-init Kubernetes clients on first use."""
    global _k8s_configured, _core_v1_api, _apps_v1_api
    if not _k8s_configured:
        try:
            load_k8s_config()
        except config.ConfigException as e:
            logger.error(f"Failed to load kubernetes config: {e}")
            # Retried, never a PermanentError: that would end the re-sync timer, and has no place in a delete handler.
            raise kopf.TemporaryError("Could not configure kubernetes client", delay=60)
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


def allowed(value: str, patterns: Collection[str]) -> bool:
    """Tell whether a label key or project is one a workload may ask for: any is, unless patterns are given."""
    return not patterns or any(fnmatchcase(value, pattern) for pattern in patterns)


def wanted_labels(annotations: Mapping[str, str], vcluster: str) -> dict[str, str]:
    """Return the labels a vCluster's workload asks for on its ArgoCD secret.

    An entry that cannot be a label, or whose key ALLOWED_LABELS does not allow, is left out and reported:
    the vCluster is still enrolled without it.
    """
    labels = {}
    for entry in filter(str.strip, annotations.get(LABELS_ANNOTATION, "").split(",")):
        try:
            key, value = parse_label(entry)
            if not allowed(key, ALLOWED_LABELS):
                raise ValueError(f"{key!r} is not a label key workloads are allowed to set")
        except ValueError as e:
            logger.warning(f"Ignoring a label asked for by {vcluster} in {LABELS_ANNOTATION}: {e}")
        else:
            labels[key] = value
    return labels


def wanted_project(annotations: Mapping[str, str], vcluster: str) -> str | None:
    """Return the ArgoCD project a vCluster's workload asks to be scoped to.

    One that ALLOWED_PROJECTS does not allow is reported: the vCluster is still enrolled, without a project.
    """
    project = annotations.get(PROJECT_ANNOTATION, "").strip()
    if project and not allowed(project, ALLOWED_PROJECTS):
        logger.warning(
            f"Ignoring the project asked for by {vcluster} in {PROJECT_ANNOTATION}: {project!r} is not allowed"
        )
        return None
    return project or None


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


def read_argocd_secret(core_v1_api: client.CoreV1Api, name: str) -> client.V1Secret | None:
    """Read a secret from the ArgoCD namespace, or None when there is none."""
    try:
        return core_v1_api.read_namespaced_secret(name=name, namespace=ARGOCD_NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _version(secret: client.V1Secret) -> str | None:
    """Return the resource version of a secret read from the API."""
    return secret.metadata.resource_version if secret.metadata else None


def _union(fields: dict, more: Mapping) -> dict:
    """Merge one managed-fields set into another."""
    for key, value in more.items():
        fields[key] = _union(fields.get(key, {}), value) if isinstance(value, Mapping) else value
    return fields


def _adopted_managed_fields(secret: client.V1Secret) -> list[dict] | None:
    """Return the managed fields of an ArgoCD secret written by 2.2 or earlier, handed over to FIELD_MANAGER.

    None when there is nothing to hand over. Without this the first apply would share those fields with
    LEGACY_FIELD_MANAGER, and a label or project dropped later would stay: something else still "owned" it.
    Only what the operator wrote is taken: the secret's data, its own labels, and the labels and annotation
    it recorded as copied. Whatever else another user of the Python client set under the same name is left as is.
    """
    meta = secret.metadata
    entries = (meta.managed_fields if meta else None) or []
    legacy = [e for e in entries if e.manager == LEGACY_FIELD_MANAGER and e.operation == "Update" and not e.subresource]
    if not legacy:
        return None
    ours = [e for e in entries if e.manager == FIELD_MANAGER and e.operation == "Apply" and not e.subresource]

    written: dict = {}
    for entry in legacy:
        _union(written, entry.fields_v1 or {})
    copied = (((meta.annotations if meta else None) or {}).get(COPIED_LABELS_ANNOTATION, "")).split(",")
    labels = {f"f:{key}" for key in (*RESERVED_LABELS, *filter(None, copied))}
    written_meta = written.get("f:metadata", {})
    fields = {
        "f:data": written.get("f:data", {}),
        "f:metadata": {
            "f:labels": {k: v for k, v in written_meta.get("f:labels", {}).items() if k in labels},
            "f:annotations": {
                k: v for k, v in written_meta.get("f:annotations", {}).items() if k == f"f:{COPIED_LABELS_ANNOTATION}"
            },
        },
    }
    # Plus what is applied already, on a secret that an older version wrote to again after a downgrade
    for entry in ours:
        _union(fields, entry.fields_v1 or {})

    adopted = [
        {
            "manager": entry.manager,
            "operation": entry.operation,
            "apiVersion": entry.api_version,
            "fieldsType": entry.fields_type,
            "fieldsV1": entry.fields_v1,
            **({"subresource": entry.subresource} if entry.subresource else {}),
            **({"time": entry.time.strftime("%Y-%m-%dT%H:%M:%SZ")} if entry.time else {}),
        }
        for entry in entries
        if entry not in legacy and entry not in ours
    ]
    adopted.append(
        {
            "manager": FIELD_MANAGER,
            "operation": "Apply",
            "apiVersion": "v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": fields,
        }
    )
    return adopted


def _delete_if_owned(core_v1_api: client.CoreV1Api, name: str, namespace: str, vcluster_name: str) -> bool:
    """Delete an ArgoCD secret unless it is missing or belongs to something else."""
    secret = read_argocd_secret(core_v1_api, name)
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


def sync_enrollment(core_v1_api: client.CoreV1Api, namespace: str, vcluster_name: str, desired: dict) -> str | None:
    """Apply a vCluster's ArgoCD secret, and drop the one 1.x wrote for it.

    Returns what that did to the secret, "Created" or "Updated", or None when it was up to date already.
    Raises NotOwnedError rather than touch a secret that is not this vCluster's enrollment.

    The apply sets the secret's data and labels and nothing else: labels, annotations and finalizers that
    something else put there stay, and what this operator applied earlier but no longer does is removed.
    """
    name = desired["metadata"]["name"]
    existing = read_argocd_secret(core_v1_api, name)
    if existing is not None:
        if not owns(existing, namespace, vcluster_name):
            raise NotOwnedError(f"ArgoCD secret {name} exists but is not {namespace}/{vcluster_name}'s")
        if (managed_fields := _adopted_managed_fields(existing)) is not None:
            existing = core_v1_api.patch_namespaced_secret(
                name,
                ARGOCD_NAMESPACE,
                {"metadata": {"resourceVersion": _version(existing), "managedFields": managed_fields}},
                _content_type=MERGE_PATCH,
            )
            logger.info(f"Took over the fields of ArgoCD cluster secret {name} written before server-side apply")
        # Applying on top of the version just read: a secret changed, or replaced, in between is a 409 to retry.
        version = _version(existing)
        desired = {**desired, "metadata": {**desired["metadata"], "resourceVersion": version}}

    applied = core_v1_api.patch_namespaced_secret(
        name, ARGOCD_NAMESPACE, desired, field_manager=FIELD_MANAGER, force=True, _content_type=APPLY_PATCH
    )
    if existing is None:
        done: str | None = "Created"
    else:
        # An apply that changes nothing leaves the resource version alone
        done = "Updated" if _version(applied) != version else None
    if done:
        logger.info(f"{done} ArgoCD cluster secret {name} for vcluster {namespace}/{vcluster_name}")
    else:
        logger.debug(f"ArgoCD cluster secret {name} is up to date")

    # Enrollments made before the namespace was part of the name move over to the new one.
    legacy_secret_name = legacy_ar_secret_name(vcluster_name)
    if _delete_if_owned(core_v1_api, legacy_secret_name, namespace, vcluster_name):
        logger.info(f"Removed legacy ArgoCD cluster secret {legacy_secret_name}, replaced by {name}")
    return done


def remove_enrollment(core_v1_api: client.CoreV1Api, namespace: str, vcluster_name: str) -> list[str]:
    """Delete the ArgoCD secrets this operator wrote for a vCluster, under the current name and the 1.x one.

    Returns the names of the secrets deleted. One that belongs to something else is left alone.
    """
    deleted = []
    for secret_name in (ar_secret_name(namespace, vcluster_name), legacy_ar_secret_name(vcluster_name)):
        if _delete_if_owned(core_v1_api, secret_name, namespace, vcluster_name):
            logger.info(f"Deleted ArgoCD cluster secret {secret_name} for {namespace}/{vcluster_name}")
            deleted.append(secret_name)
    return deleted


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


def _has_workload(apps_v1_api: client.AppsV1Api, namespace: str, name: str) -> bool:
    """Tell whether a vCluster has a workload the operator watches: a StatefulSet or Deployment labelled as one.

    One that is being deleted counts: its delete handler sees to the enrollment.
    """
    for read in (apps_v1_api.read_namespaced_stateful_set, apps_v1_api.read_namespaced_deployment):
        try:
            workload = read(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                continue
            raise
        if VCLUSTER_LABELS.items() <= ((workload.metadata.labels if workload.metadata else None) or {}).items():
            return True
    return False


def sweep_orphans(
    core_v1_api: client.CoreV1Api,
    apps_v1_api: client.AppsV1Api,
    namespaces: Collection[str] = (),
    suspects: Collection[str] = (),
) -> set[str]:
    """Remove the enrollments of vClusters that are gone, or no longer labelled as vClusters.

    An enrollment is removed the second time in a row it is found without its vCluster: `suspects` are the
    UIDs of the secrets found that way last time, and the return value those to pass in next time. That
    keeps the enrollment of a vCluster whose workload is away for a moment, being replaced or restored after
    its secret. With `namespaces`, only the enrollments of vClusters in those are looked at. A vCluster that
    cannot be looked up keeps its enrollment.
    """
    secrets = core_v1_api.list_namespaced_secret(namespace=ARGOCD_NAMESPACE, label_selector=f"{OPERATOR_LABEL}=true")
    orphans = set()
    enrollments = 0
    for secret in secrets.items:
        meta = secret.metadata
        labels = (meta.labels if meta else None) or {}
        namespace, vcluster_name = labels.get(NAMESPACE_LABEL), labels.get(NAME_LABEL)
        # A secret 1.x wrote, without these labels, goes when its vCluster is synced or deleted
        if not meta or not meta.uid or not namespace or not vcluster_name:
            continue
        if (namespaces and namespace not in namespaces) or meta.name != ar_secret_name(namespace, vcluster_name):
            continue
        vcluster = f"{namespace}/{vcluster_name}"
        enrollments += 1
        try:
            if _has_workload(apps_v1_api, namespace, vcluster_name):
                continue
            if meta.uid not in suspects:
                logger.debug(f"ArgoCD cluster secret {meta.name} has no vCluster {vcluster}")
                orphans.add(meta.uid)
                continue
            logger.info(f"Removing the enrollment of {vcluster}: there is no such vCluster any more")
            removed = remove_enrollment(core_v1_api, namespace, vcluster_name)
            metrics.REMOVED.labels("orphaned").inc(len(removed))
            enrollments -= 1
        except ApiException as e:
            logger.warning(f"Keeping the enrollment of {vcluster}: {e.status} {e.reason} checking on it")
    metrics.ENROLLMENTS.set(enrollments)
    metrics.ORPHAN_SUSPECTS.set(len(orphans))
    metrics.LAST_SWEEP.set_to_current_time()
    return orphans


async def _sweep_periodically() -> None:
    """Run sweep_orphans() at startup and every RESYNC_INTERVAL from then on."""
    suspects: set[str] = set()
    while True:
        try:
            core_v1_api, apps_v1_api = _ensure_k8s()
            suspects = await asyncio.to_thread(sweep_orphans, core_v1_api, apps_v1_api, WATCHED_NAMESPACES, suspects)
        except ApiException as e:
            logger.error(f"API error looking for enrollments without a vCluster: {e.status} {e.reason}")
        except kopf.TemporaryError as e:
            logger.error(f"Not looking for enrollments without a vCluster: {e}")
        await asyncio.sleep(RESYNC_INTERVAL)


def handle_vcluster_enrollment(
    workload_name: str, namespace: str, annotations: Mapping[str, str] | None = None, **kwargs
) -> None:
    """Create or update the ArgoCD cluster secret for a vCluster, or remove it when the vCluster opts out.

    `annotations` are those of the vCluster's workload, which say how it wants to be enrolled.
    """
    try:
        outcome = _enroll(workload_name, namespace, annotations or {})
    except kopf.TemporaryError:
        metrics.SYNCS.labels("failed").inc()
        raise
    metrics.SYNCS.labels(outcome).inc()


def _enroll(workload_name: str, namespace: str, annotations: Mapping[str, str]) -> str:
    """Do what handle_vcluster_enrollment() says, and return what that was, as the `outcome` of the syncs metric."""
    core_v1_api, _ = _ensure_k8s()

    vcluster_name = vc_name(workload_name)

    if opted_out(annotations):
        logger.debug(f"Not enrolling {namespace}/{vcluster_name}: its workload is annotated {ENROLL_ANNOTATION}=false")
        try:
            removed = remove_enrollment(core_v1_api, namespace, vcluster_name)
        except ApiException as e:
            logger.error(f"API error removing the enrollment of {namespace}/{vcluster_name}: {e.status} {e.reason}")
            raise kopf.TemporaryError(
                f"Failed to remove the enrollment of {namespace}/{vcluster_name}: {e.status} {e.reason}", delay=60
            )
        metrics.REMOVED.labels("opted_out").inc(len(removed))
        return "opted_out"
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
            project=wanted_project(annotations, f"{namespace}/{vcluster_name}"),
        )
    except KeyError as e:
        # Retried rather than given up on: the secret can be completed (or fixed) later.
        logger.warning(f"vcluster secret {namespace}/{vcluster_secret_name} has no {e} entry")
        raise kopf.TemporaryError(
            f"Failed to parse vcluster secret {namespace}/{vcluster_secret_name}: no {e} entry", delay=60
        )

    argocd_secret_name = ar_secret_name(namespace, vcluster_name)

    try:
        done = sync_enrollment(core_v1_api, namespace, vcluster_name, secret_body)
    except NotOwnedError as e:
        logger.error(str(e))
        raise kopf.TemporaryError(
            f"ArgoCD secret {argocd_secret_name} belongs to something else; not overwriting it", delay=60
        )
    except ApiException as e:
        logger.error(f"API error syncing ArgoCD secret {argocd_secret_name}: {e.status} {e.reason}")
        raise kopf.TemporaryError(f"Failed to sync ArgoCD secret {argocd_secret_name}: {e.status} {e.reason}", delay=60)
    return done.lower() if done else "unchanged"


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


@kopf.on.startup()
async def start_sweep(**_):
    """Start looking for enrollments whose vCluster is gone."""
    global _sweep_task
    if SWEEP_ORPHANS:
        _sweep_task = asyncio.create_task(_sweep_periodically())


@kopf.on.cleanup()
async def stop_sweep(**_):
    """Stop the sweep as the operator exits."""
    if _sweep_task is not None:
        _sweep_task.cancel()


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
        removed = remove_enrollment(core_v1_api, namespace, vcluster_name)
    except ApiException as e:
        logger.error(f"Failed to delete ArgoCD cluster secret for {namespace}/{vcluster_name}: {e.status} {e.reason}")
        # Never a PermanentError here. kopf retries DELETE_RETRIES times, then releases the finalizer anyway.
        raise kopf.TemporaryError(f"Failed to delete ArgoCD cluster secret: {e.status} {e.reason}", delay=30)
    metrics.REMOVED.labels("deleted").inc(len(removed))
