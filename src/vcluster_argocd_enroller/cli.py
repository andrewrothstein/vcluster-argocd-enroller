"""CLI for vCluster ArgoCD Enroller operator."""

import asyncio
import logging
import sys
from typing import Annotated, Literal, Optional

import cyclopts
from rich.console import Console

from . import __version__

console = Console()
app = cyclopts.App(
    name="vcluster-argocd-enroller",
    version=__version__,
    help="Kubernetes operator that automatically enrolls vCluster instances in ArgoCD",
)


@app.default
def run(
    *,
    namespace: Optional[str] = None,
    dev: bool = False,
    verbose: bool = False,
    log_level: Annotated[
        Literal["DEBUG", "INFO", "WARNING", "ERROR"], cyclopts.Parameter(env_var="LOG_LEVEL")
    ] = "INFO",
    standalone: bool = False,
    priority: int = 0,
    peering_name: Optional[str] = None,
    clusterwide: bool = True,
) -> None:
    """Run the vCluster ArgoCD enrollment operator.

    Parameters
    ----------
    namespace : str, optional
        Namespace to watch for vClusters (default: all namespaces)
    dev : bool
        Run in development mode (kopf --dev: take priority over other running instances)
    verbose : bool
        Enable verbose output (sets log level to DEBUG)
    log_level : str
        Logging level (DEBUG, INFO, WARNING, ERROR); defaults to $LOG_LEVEL
    standalone : bool
        Run in standalone mode (no peering)
    priority : int
        Operator priority for peering
    peering_name : str, optional
        Custom peering name for multiple operators
    clusterwide : bool
        Watch cluster-wide (default: True)
    """
    import kopf
    import uvloop

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
    from . import operator  # noqa: F401

    console.print("[green]Starting operator...[/green]")
    # Same loop setup as `kopf run`: kopf only picks uvloop by itself when launched from its own CLI.
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        kopf.run(
            loop=runner.get_loop(),
            standalone=standalone or None,
            priority=666 if dev else priority or None,
            peering_name=peering_name,
            liveness_endpoint="http://0.0.0.0:8080/healthz",
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
    from kubernetes import client, config
    from rich.table import Table

    from .operator import ARGOCD_NAMESPACE, ar_secret_name, vc_name

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    # Find vCluster StatefulSets
    if namespace:
        statefulsets = apps_v1.list_namespaced_stateful_set(namespace=namespace, label_selector="app=vcluster")
    else:
        statefulsets = apps_v1.list_stateful_set_for_all_namespaces(label_selector="app=vcluster")

    if not statefulsets.items:
        console.print("[yellow]No vClusters found[/yellow]")
        return

    # Create table
    table = Table(title="vCluster Status")
    table.add_column("Namespace", style="cyan")
    table.add_column("vCluster", style="green")
    table.add_column("Ready", style="yellow")
    table.add_column("Secret", style="blue")
    table.add_column("ArgoCD Secret", style="magenta")

    for sts in statefulsets.items:
        if not sts.metadata or not sts.metadata.namespace or not sts.metadata.name:
            continue
        ns = sts.metadata.namespace
        name = sts.metadata.name

        vcluster_name = vc_name(name)

        # Check readiness
        ready_replicas = sts.status.ready_replicas if sts.status else None
        ready = f"{ready_replicas or 0}/{sts.spec.replicas}"

        # Check for vcluster secret
        vc_secret_name = f"vc-{vcluster_name}"
        try:
            v1.read_namespaced_secret(name=vc_secret_name, namespace=ns)
            vc_secret_status = "Y"
        except client.exceptions.ApiException:
            vc_secret_status = "N"

        # Check for ArgoCD secret
        argocd_secret_name = ar_secret_name(vcluster_name)
        try:
            v1.read_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE)
            argocd_secret_status = "Y"
        except client.exceptions.ApiException:
            argocd_secret_status = "N"

        table.add_row(ns, vcluster_name, ready, vc_secret_status, argocd_secret_status)

    console.print(table)

    if show_secrets:
        console.print("\n[bold]Secret Details:[/bold]")
        for sts in statefulsets.items:
            if not sts.metadata or not sts.metadata.namespace or not sts.metadata.name:
                continue
            ns = sts.metadata.namespace
            name = sts.metadata.name
            vcluster_name = vc_name(name)

            console.print(f"\n[cyan]{ns}/{vcluster_name}:[/cyan]")
            console.print(f"  vCluster Secret: vc-{vcluster_name}")
            console.print(f"  ArgoCD Secret: {ar_secret_name(vcluster_name)}")


@app.command
def enroll(
    vcluster_name: str,
    namespace: str = "default",
    *,
    force: bool = False,
) -> None:
    """Manually enroll a vCluster in ArgoCD.

    Parameters
    ----------
    vcluster_name : str
        Name of the vCluster to enroll
    namespace : str
        Namespace where vCluster is deployed
    force : bool
        Force re-enrollment even if already exists
    """
    from typing import cast

    from kubernetes import client, config

    from .operator import ARGOCD_NAMESPACE, ar_secret_name, build_argocd_secret

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()

    # Check for existing ArgoCD secret
    argocd_secret_name = ar_secret_name(vcluster_name)

    if not force:
        try:
            v1.read_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE)
            console.print(f"[yellow]vCluster {vcluster_name} already enrolled in ArgoCD[/yellow]")
            console.print("[dim]Use --force to re-enroll[/dim]")
            return
        except client.exceptions.ApiException as e:
            if e.status != 404:
                console.print(f"[red]Failed to check ArgoCD secret {argocd_secret_name}: {e}[/red]")
                sys.exit(1)

    # Read vCluster secret
    vc_secret_name = f"vc-{vcluster_name}"
    try:
        vc_secret = v1.read_namespaced_secret(name=vc_secret_name, namespace=namespace)
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to read vCluster secret {vc_secret_name}: {e}[/red]")
        sys.exit(1)

    try:
        argocd_secret = cast(client.V1Secret, build_argocd_secret(vcluster_name, namespace, vc_secret))
    except KeyError as e:
        console.print(f"[red]vCluster secret {vc_secret_name} has no {e} entry[/red]")
        sys.exit(1)

    # Create the secret, replacing an existing one in place when forced
    try:
        try:
            v1.create_namespaced_secret(namespace=ARGOCD_NAMESPACE, body=argocd_secret)
        except client.exceptions.ApiException as e:
            if not (force and e.status == 409):
                raise
            v1.replace_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE, body=argocd_secret)
            console.print("[yellow]Replaced existing ArgoCD secret[/yellow]")
        console.print(f"[green]Successfully enrolled vCluster {vcluster_name} in ArgoCD[/green]")
    except client.exceptions.ApiException as e:
        console.print(f"[red]Failed to create ArgoCD secret: {e}[/red]")
        sys.exit(1)


@app.command
def unenroll(
    vcluster_name: str,
    *,
    confirm: bool = False,
) -> None:
    """Remove a vCluster from ArgoCD.

    Parameters
    ----------
    vcluster_name : str
        Name of the vCluster to unenroll
    confirm : bool
        Skip confirmation prompt
    """
    from kubernetes import client, config

    from .operator import ARGOCD_NAMESPACE, ar_secret_name

    if not confirm:
        response = console.input(f"[yellow]Remove {vcluster_name} from ArgoCD? [y/N]:[/yellow] ")
        if response.lower() != "y":
            console.print("[dim]Cancelled[/dim]")
            return

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    argocd_secret_name = ar_secret_name(vcluster_name)

    try:
        v1.delete_namespaced_secret(name=argocd_secret_name, namespace=ARGOCD_NAMESPACE)
        console.print(f"[green]Successfully removed vCluster {vcluster_name} from ArgoCD[/green]")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            console.print(f"[yellow]vCluster {vcluster_name} not found in ArgoCD[/yellow]")
        else:
            console.print(f"[red]Failed to remove ArgoCD secret: {e}[/red]")
            sys.exit(1)


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
