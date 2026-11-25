"""CLI for vCluster ArgoCD Enroller operator."""

import asyncio
import logging
import random
import sys
from typing import Annotated, Literal, Optional

import cyclopts
from rich.console import Console

from . import __version__

console = Console()

# Replicas sharing a peering draw a random priority, so exactly one outranks the rest; --dev outranks them all.
MAX_PRIORITY = 2**31 - 1
app = cyclopts.App(
    name="vcluster-argocd-enroller",
    version=__version__,
    help="Kubernetes operator that automatically enrolls vCluster instances in ArgoCD",
)


def _configure_k8s() -> None:
    """Point the Kubernetes client at the cluster, or exit when there is no configuration to do it with."""
    from kubernetes import config

    from .operator import load_k8s_config

    try:
        load_k8s_config()
    except config.ConfigException as e:
        console.print(f"[red]Could not configure the Kubernetes client: {e}[/red]")
        sys.exit(1)


@app.default
def run(
    *,
    namespace: Optional[str] = None,
    dev: bool = False,
    verbose: bool = False,
    log_level: Annotated[
        Literal["DEBUG", "INFO", "WARNING", "ERROR"], cyclopts.Parameter(env_var="LOG_LEVEL")
    ] = "INFO",
    priority: Optional[int] = None,
    peering_name: Annotated[Optional[str], cyclopts.Parameter(env_var="PEERING_NAME")] = None,
    clusterwide: bool = True,
    health_port: Annotated[int, cyclopts.Parameter(env_var="HEALTH_PORT")] = 8080,
    metrics_port: Annotated[Optional[int], cyclopts.Parameter(env_var="METRICS_PORT")] = None,
) -> None:
    """Run the vCluster ArgoCD enrollment operator.

    Parameters
    ----------
    namespace : str, optional
        Namespace to watch for vClusters (default: all namespaces); not with --peering-name
    dev : bool
        Run in development mode (with --peering-name: outrank every other instance, so they pause)
    verbose : bool
        Enable verbose output (sets log level to DEBUG)
    log_level : str
        Logging level (DEBUG, INFO, WARNING, ERROR); defaults to $LOG_LEVEL
    priority : int, optional
        Priority among the instances sharing --peering-name (default: random, so replicas never tie)
    peering_name : str, optional
        Name of the kopf peering object used for leader election: only the instance with the highest
        priority works, the rest stand by. Defaults to $PEERING_NAME; without it there is no peering.
    clusterwide : bool
        Watch cluster-wide (default: True); --no-clusterwide needs --namespace
    health_port : int
        Port serving /healthz for liveness and readiness probes; defaults to $HEALTH_PORT
    metrics_port : int, optional
        Port serving Prometheus metrics on /metrics; defaults to $METRICS_PORT, and without it there are none
    """
    import kopf
    import uvloop

    # kopf would not say no to either: it idles for want of a namespaced peering object, or watches everything.
    if namespace and peering_name:
        console.print("[red]--namespace cannot be combined with --peering-name: leader election is cluster-wide[/red]")
        sys.exit(1)
    if not clusterwide and not namespace:
        console.print("[red]--no-clusterwide needs a --namespace to watch[/red]")
        sys.exit(1)

    if verbose:
        log_level = "DEBUG"

    # kopf owns the root handler, so every record is emitted once and in one format.
    kopf.configure(verbose=log_level == "DEBUG", quiet=log_level in ("WARNING", "ERROR"))
    logging.getLogger().setLevel(log_level)

    console.print(f"[bold green]vCluster ArgoCD Enroller v{__version__}[/bold green]")
    console.print(f"[dim]Running in {'development' if dev else 'production'} mode[/dim]")

    if namespace:
        console.print(f"[yellow]Watching namespace:[/yellow] {namespace}")
    else:
        console.print("[yellow]Watching:[/yellow] all namespaces")

    # Import operator module to register handlers
    from . import operator

    # The sweep for enrollments without a vCluster keeps to the namespace watched
    operator.WATCHED_NAMESPACES = (namespace,) if namespace else ()

    if dev:
        priority = MAX_PRIORITY
    elif priority is None:
        priority = random.randrange(MAX_PRIORITY)

    if metrics_port:
        from prometheus_client import start_http_server

        start_http_server(metrics_port)
        console.print(f"[yellow]Metrics:[/yellow] http://0.0.0.0:{metrics_port}/metrics")

    console.print("[green]Starting operator...[/green]")
    # Same loop setup as `kopf run`: kopf only picks uvloop by itself when launched from its own CLI.
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        kopf.run(
            loop=runner.get_loop(),
            # Peering is opt-in: kopf would otherwise join any peering object named "default" it finds.
            standalone=not peering_name,
            priority=priority,
            peering_name=peering_name,
            liveness_endpoint=f"http://0.0.0.0:{health_port}/healthz",
            clusterwide=clusterwide and not namespace,
            namespaces=[namespace] if namespace else [],
        )


@app.command
def check(
    *,
    namespace: Optional[str] = None,
    show_secrets: bool = False,
) -> None:
    """Check for existing vClusters and their ArgoCD enrollment status.

    Parameters
    ----------
    namespace : str, optional
        Namespace to check (default: all namespaces)
    show_secrets : bool
        Show secret details (default: False)
    """
    from kubernetes import client
    from rich.table import Table

    from .operator import ar_secret_name, opted_out, owns, read_argocd_secret, vc_name

    _configure_k8s()

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    # Find vCluster control planes: StatefulSets, or Deployments for vClusters that keep no state of their own
    workloads: list[client.V1StatefulSet | client.V1Deployment]
    if namespace:
        workloads = [
            *apps_v1.list_namespaced_stateful_set(namespace=namespace, label_selector="app=vcluster").items,
            *apps_v1.list_namespaced_deployment(namespace=namespace, label_selector="app=vcluster").items,
        ]
    else:
        workloads = [
            *apps_v1.list_stateful_set_for_all_namespaces(label_selector="app=vcluster").items,
            *apps_v1.list_deployment_for_all_namespaces(label_selector="app=vcluster").items,
        ]

    if not workloads:
        console.print("[yellow]No vClusters found[/yellow]")
        return

    # Create table
    table = Table(title="vCluster Status")
    table.add_column("Namespace", style="cyan")
    table.add_column("vCluster", style="green")
    table.add_column("Ready", style="yellow")
    table.add_column("Secret", style="blue")
    table.add_column("ArgoCD Secret", style="magenta")

    for workload in workloads:
        if not workload.metadata or not workload.metadata.namespace or not workload.metadata.name:
            continue
        ns = workload.metadata.namespace
        name = workload.metadata.name

        vcluster_name = vc_name(name)

        # Check readiness
        ready_replicas = workload.status.ready_replicas if workload.status else None
        ready = f"{ready_replicas or 0}/{workload.spec.replicas}"

        # Check for vcluster secret. Only a 404 says there is none: any other failure is shown as what it is.
        vc_secret_name = f"vc-{vcluster_name}"
        try:
            v1.read_namespaced_secret(name=vc_secret_name, namespace=ns)
            vc_secret_status = "Y"
        except client.exceptions.ApiException as e:
            vc_secret_status = "N" if e.status == 404 else f"? ({e.status} {e.reason})"

        # Check for ArgoCD secret: one of that name that belongs to something else is no enrollment
        try:
            argocd_secret = read_argocd_secret(v1, ar_secret_name(ns, vcluster_name))
        except client.exceptions.ApiException as e:
            argocd_secret_status = f"? ({e.status} {e.reason})"
        else:
            if argocd_secret is None:
                argocd_secret_status = "N"
            elif owns(argocd_secret, ns, vcluster_name):
                argocd_secret_status = "Y"
            else:
                argocd_secret_status = "N (name taken)"
        # The operator keeps a vCluster annotated this way out of ArgoCD
        if opted_out(workload.metadata.annotations or {}):
            argocd_secret_status += " (opted out)"

        table.add_row(ns, vcluster_name, ready, vc_secret_status, argocd_secret_status)

    console.print(table)

    if show_secrets:
        console.print("\n[bold]Secret Details:[/bold]")
        for workload in workloads:
            if not workload.metadata or not workload.metadata.namespace or not workload.metadata.name:
                continue
            ns = workload.metadata.namespace
            name = workload.metadata.name
            vcluster_name = vc_name(name)

            console.print(f"\n[cyan]{ns}/{vcluster_name}:[/cyan]")
            console.print(f"  vCluster Secret: vc-{vcluster_name}")
            console.print(f"  ArgoCD Secret: {ar_secret_name(ns, vcluster_name)}")


@app.command
def enroll(
    vcluster_name: str,
    namespace: str = "default",
    *,
    force: bool = False,
    label: Annotated[Optional[list[str]], cyclopts.Parameter(negative="")] = None,
    project: Optional[str] = None,
) -> None:
    """Manually enroll a vCluster in ArgoCD.

    A running operator re-syncs the enrollment from the annotations of the vCluster's workload, so set
    labels and the project there when the operator is in charge. An ArgoCD secret of the same name that
    is not this vCluster's enrollment is never overwritten, --force or not.

    Parameters
    ----------
    vcluster_name : str
        Name of the vCluster to enroll
    namespace : str
        Namespace where vCluster is deployed
    force : bool
        Force re-enrollment even if already exists
    label : list of str, optional
        Label for the ArgoCD cluster secret, as key=value; repeat for more than one
    project : str, optional
        ArgoCD project the cluster is scoped to
    """
    from kubernetes import client

    from .operator import (
        NotOwnedError,
        ar_secret_name,
        build_argocd_secret,
        owns,
        parse_label,
        read_argocd_secret,
        sync_enrollment,
    )

    try:
        labels = dict(parse_label(entry) for entry in label or [])
    except ValueError as e:
        console.print(f"[red]Invalid --label: {e}[/red]")
        sys.exit(1)

    _configure_k8s()

    v1 = client.CoreV1Api()

    # Check for existing ArgoCD secret
    argocd_secret_name = ar_secret_name(namespace, vcluster_name)

    if not force:
        try:
            existing = read_argocd_secret(v1, argocd_secret_name)
        except client.exceptions.ApiException as e:
            console.print(f"[red]Failed to check ArgoCD secret {argocd_secret_name}: {e}[/red]")
            sys.exit(1)
        if existing is not None and owns(existing, namespace, vcluster_name):
            console.print(f"[yellow]vCluster {namespace}/{vcluster_name} already enrolled in ArgoCD[/yellow]")
            console.print("[dim]Use --force to re-enroll[/dim]")
            return

    # Read vCluster secret
    vc_secret_name = f"vc-{vcluster_name}"
    try:
        vc_secret = v1.read_namespaced_secret(name=vc_secret_name, namespace=namespace)
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to read vCluster secret {vc_secret_name}: {e}[/red]")
        sys.exit(1)

    try:
        argocd_secret = build_argocd_secret(vcluster_name, namespace, vc_secret, labels=labels, project=project)
    except KeyError as e:
        console.print(f"[red]vCluster secret {vc_secret_name} has no {e} entry[/red]")
        sys.exit(1)

    # The operator's own sync: creates the secret, or replaces this vCluster's existing one in place
    try:
        done = sync_enrollment(v1, namespace, vcluster_name, argocd_secret)
    except NotOwnedError as e:
        console.print(f"[red]{e}; not overwriting it[/red]")
        sys.exit(1)
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to write ArgoCD secret {argocd_secret_name}: {e}[/red]")
        sys.exit(1)
    if done == "Updated":
        console.print("[yellow]Replaced existing ArgoCD secret[/yellow]")
    console.print(f"[green]Successfully enrolled vCluster {namespace}/{vcluster_name} in ArgoCD[/green]")


@app.command
def unenroll(
    vcluster_name: str,
    namespace: str = "default",
    *,
    confirm: bool = False,
) -> None:
    """Remove a vCluster from ArgoCD.

    A running operator enrolls the vCluster again on its next re-sync for as long as the vCluster exists.
    An ArgoCD secret of the same name that is not this vCluster's enrollment is left alone.

    Parameters
    ----------
    vcluster_name : str
        Name of the vCluster to unenroll
    namespace : str
        Namespace where vCluster is deployed
    confirm : bool
        Skip confirmation prompt
    """
    from kubernetes import client

    from .operator import remove_enrollment

    if not confirm:
        response = console.input(f"[yellow]Remove {namespace}/{vcluster_name} from ArgoCD? [y/N]:[/yellow] ")
        if response.lower() != "y":
            console.print("[dim]Cancelled[/dim]")
            return

    _configure_k8s()

    v1 = client.CoreV1Api()

    # The operator's own removal: also takes the secret 1.x wrote, and warns about one that is something else's
    try:
        deleted = remove_enrollment(v1, namespace, vcluster_name)
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to remove ArgoCD secret: {e}[/red]")
        sys.exit(1)
    if deleted:
        console.print(f"[green]Successfully removed vCluster {namespace}/{vcluster_name} from ArgoCD[/green]")
    else:
        console.print(f"[yellow]vCluster {namespace}/{vcluster_name} not found in ArgoCD[/yellow]")


@app.command
def verify(
    vcluster_name: str,
    namespace: str = "default",
) -> None:
    """Check that the credentials enrolled in ArgoCD reach the vCluster's API server.

    Connects the way ArgoCD does, so it has to run where the vCluster's Service resolves (inside the cluster).

    Parameters
    ----------
    vcluster_name : str
        Name of the vCluster to verify
    namespace : str
        Namespace where vCluster is deployed
    """
    import json
    import tempfile
    from pathlib import Path

    from kubernetes import client

    from .operator import ARGOCD_NAMESPACE, ar_secret_name, decode

    _configure_k8s()

    v1 = client.CoreV1Api()
    argocd_secret_name = ar_secret_name(namespace, vcluster_name)

    try:
        data = v1.read_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE).data or {}
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to read ArgoCD secret {argocd_secret_name}: {e.status} {e.reason}[/red]")
        sys.exit(1)

    try:
        server = decode(data["server"])
        tls = json.loads(decode(data["config"]))["tlsClientConfig"]
        credentials = {key: decode(tls[key]) for key in ("caData", "certData", "keyData")}
    except (KeyError, ValueError) as e:
        console.print(f"[red]ArgoCD secret {argocd_secret_name} is malformed: {e}[/red]")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        for key, pem in credentials.items():
            Path(tmp, key).write_text(pem)

        configuration = client.Configuration()
        configuration.host = server
        configuration.ssl_ca_cert = str(Path(tmp, "caData"))
        configuration.cert_file = str(Path(tmp, "certData"))
        configuration.key_file = str(Path(tmp, "keyData"))

        try:
            version = client.VersionApi(client.ApiClient(configuration)).get_code()
        except Exception as e:
            console.print(f"[red]Failed to reach {server} with the enrolled credentials: {e}[/red]")
            sys.exit(1)

    console.print(f"[green]{server} answers with the enrolled credentials (Kubernetes {version.git_version})[/green]")


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
