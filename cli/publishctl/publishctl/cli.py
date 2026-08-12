"""PublishHub developer CLI — main click group and commands.

A thin, honest wrapper: it shells out to make, kubectl, and helm and surfaces
their exit codes and stderr rather than swallowing them.
"""

from __future__ import annotations

import shutil
import sys

import click
from rich.console import Console
from rich.table import Table

from publishctl.helpers import kubectl, make, run

console = Console()
err_console = Console(stderr=True)

# ─── Tool registry for `doctor` ────────────────────────────────────────────────

REQUIRED_TOOLS: list[tuple[str, str]] = [
    ("docker", "brew install --cask docker (or https://docs.docker.com/get-docker/)"),
    ("kind", "brew install kind (or https://kind.sigs.k8s.io/docs/user/quick-start/)"),
    ("kubectl", "brew install kubectl (or https://kubernetes.io/docs/tasks/tools/)"),
    ("helm", "brew install helm (or https://helm.sh/docs/intro/install/)"),
    ("node", "brew install node@20 (or https://nodejs.org/en/download)"),
    ("python3", "brew install python@3.11 (or https://www.python.org/downloads/)"),
]

# ─── Stages for `env start` ────────────────────────────────────────────────────

ENV_START_STAGES: list[tuple[str, list[str]]] = [
    ("Creating kind cluster and local registry", ["make", "cluster-up"]),
    ("Installing platform (ArgoCD, KEDA, Argo Rollouts)", ["make", "platform-install"]),
    ("Building and pushing container images", ["make", "apps-build"]),
    ("Syncing ArgoCD applications", ["make", "argocd-sync"]),
]

# ─── Root group ─────────────────────────────────────────────────────────────────


@click.group()
@click.version_option(package_name="publishctl")
def cli() -> None:
    """PublishHub developer CLI — wraps common platform operations."""


# ─── doctor ─────────────────────────────────────────────────────────────────────


@cli.command()
def doctor() -> None:
    """Verify that all prerequisite tools are installed."""
    missing: list[tuple[str, str]] = []
    table = Table(title="Prerequisite Check")
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Install hint")

    for tool, hint in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            table.add_row(tool, "[green]found[/green]", path)
        else:
            table.add_row(tool, "[red]missing[/red]", hint)
            missing.append((tool, hint))

    console.print(table)

    if missing:
        err_console.print(
            f"\n[red]ERROR:[/red] {len(missing)} tool(s) missing. "
            "Install them and re-run publishctl doctor."
        )
        sys.exit(1)
    else:
        console.print("\n[green]All prerequisites satisfied.[/green]")


# ─── env ────────────────────────────────────────────────────────────────────────


@cli.group()
def env() -> None:
    """Manage the local development environment."""


@env.command("start")
def env_start() -> None:
    """Create the cluster, install platform, build images, and deploy."""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        for description, cmd in ENV_START_STAGES:
            task = progress.add_task(description, total=None)
            run(cmd, stream=True)
            progress.update(task, completed=1, total=1)

    console.print("\n[green]Environment is up.[/green]")


@env.command("stop")
def env_stop() -> None:
    """Tear down the local environment (cluster + registry)."""
    console.print("Stopping the local environment...")
    make("clean")
    console.print("[green]Environment stopped.[/green]")


# ─── status ─────────────────────────────────────────────────────────────────────


@cli.command()
def status() -> None:
    """Display cluster, ArgoCD Application, pod, and ScaledObject state."""
    sections: list[tuple[str, list[str]]] = [
        ("Cluster info", ["kubectl", "cluster-info"]),
        (
            "ArgoCD Applications",
            ["kubectl", "get", "applications", "-n", "argocd"],
        ),
        ("Pods in publishhub namespace", ["kubectl", "get", "pods", "-n", "publishhub"]),
        (
            "ScaledObjects",
            ["kubectl", "get", "scaledobject", "-n", "publishhub"],
        ),
    ]

    for title, cmd in sections:
        console.print(f"\n[bold]{title}[/bold]")
        result = run(cmd, check=False, capture=True)
        if result.returncode == 0:
            if result.stdout:
                console.print(result.stdout.rstrip())
        else:
            err_console.print(f"  (not available: {result.stderr.strip() if result.stderr else 'command failed'})")


# ─── logs ───────────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--service",
    required=True,
    type=click.Choice(["api", "worker", "web"]),
    help="Service to stream logs from.",
)
@click.option("--tail", type=int, default=None, help="Number of recent lines to show.")
@click.option("--follow", is_flag=True, help="Follow log output.")
def logs(service: str, tail: int | None, follow: bool) -> None:
    """Stream logs for a service running in the cluster."""
    cmd: list[str] = [
        "kubectl",
        "logs",
        "-n",
        "publishhub",
        "-l",
        f"app.kubernetes.io/name=publishhub-{service}",
    ]
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    if follow:
        cmd.append("--follow")

    run(cmd, stream=True)


# ─── publish ────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--content", required=True, help="Post content text.")
@click.option(
    "--platforms",
    required=True,
    help="Comma-separated list of target platforms (e.g. twitter,linkedin).",
)
def publish(content: str, platforms: str) -> None:
    """Submit a post through the API and print the response."""
    import json

    platform_list = [p.strip() for p in platforms.split(",") if p.strip()]
    payload = json.dumps({"content": content, "platforms": platform_list})

    result = run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "http://localhost:8080/api/posts",
        ],
        capture=True,
    )

    # Pretty-print the JSON response.
    try:
        data = json.loads(result.stdout)
        console.print_json(json.dumps(data))
    except (json.JSONDecodeError, TypeError):
        console.print(result.stdout)


# ─── scale ──────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--replicas", required=True, type=int, help="Desired replica count.")
def scale(replicas: int) -> None:
    """Scale the worker deployment."""
    kubectl(
        "scale",
        "deployment/publishhub-worker",
        f"--replicas={replicas}",
        "-n",
        "publishhub",
    )
    console.print(f"[green]Worker scaled to {replicas} replica(s).[/green]")


# ─── rollout ────────────────────────────────────────────────────────────────────


@cli.group()
def rollout() -> None:
    """Manage Argo Rollouts canary deployments."""


@rollout.command("status")
def rollout_status() -> None:
    """Show the current rollout status."""
    kubectl(
        "get",
        "rollout",
        "publishhub-api",
        "-n",
        "publishhub",
        "-o",
        "wide",
    )


@rollout.command("promote")
def rollout_promote() -> None:
    """Promote the current canary to stable."""
    run(
        [
            "kubectl",
            "argo",
            "rollouts",
            "promote",
            "publishhub-api",
            "-n",
            "publishhub",
        ]
    )
    console.print("[green]Canary promoted.[/green]")


@rollout.command("abort")
def rollout_abort() -> None:
    """Abort the current canary rollout."""
    run(
        [
            "kubectl",
            "argo",
            "rollouts",
            "abort",
            "publishhub-api",
            "-n",
            "publishhub",
        ]
    )
    console.print("[yellow]Canary aborted.[/yellow]")


# ─── incident ──────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--pod", required=True, help="Name of the pod to analyze.")
@click.option("--namespace", default="publishhub", help="Kubernetes namespace.")
def incident(pod: str, namespace: str) -> None:
    """Invoke the AI incident analyzer for a pod."""
    run(
        [
            "python3",
            "scripts/ai-incident-analyzer.py",
            "--pod",
            pod,
            "--namespace",
            namespace,
        ]
    )
