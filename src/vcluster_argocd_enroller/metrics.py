"""Prometheus metrics of the operator, served on /metrics when it runs with --metrics-port."""

from prometheus_client import Counter, Gauge, Info

from . import __version__

INFO = Info("vcluster_argocd_enroller", "The version of the operator")
INFO.info({"version": __version__})

# One per sync of a vCluster: on its creation and changes, and every RESYNC_INTERVAL.
SYNCS = Counter(
    "vcluster_argocd_enroller_syncs_total",
    "Syncs of a vCluster's enrollment, by what they did: created, updated or unchanged its ArgoCD secret, "
    "opted_out (the vCluster asks not to be enrolled), or failed (to be retried)",
    ["outcome"],
)
REMOVED = Counter(
    "vcluster_argocd_enroller_enrollments_removed_total",
    "ArgoCD secrets deleted, by reason: the vCluster was deleted, opted_out, or orphaned (found gone by the sweep)",
    ["reason"],
)
# The three below are set by the sweep for enrollments without a vCluster, so not when that is turned off.
ENROLLMENTS = Gauge(
    "vcluster_argocd_enroller_enrollments",
    "ArgoCD secrets the operator looks after, as of the last sweep",
)
ORPHAN_SUSPECTS = Gauge(
    "vcluster_argocd_enroller_orphan_suspects",
    "ArgoCD secrets whose vCluster the last sweep did not find, removed by the next one unless it is back",
)
LAST_SWEEP = Gauge(
    "vcluster_argocd_enroller_last_sweep_timestamp_seconds",
    "When the sweep for enrollments without a vCluster last completed, in seconds since the epoch",
)

# Every label value exists from the start, so that a rate over a counter is 0 rather than absent.
for outcome in ("created", "updated", "unchanged", "opted_out", "failed"):
    SYNCS.labels(outcome)
for reason in ("deleted", "opted_out", "orphaned"):
    REMOVED.labels(reason)
