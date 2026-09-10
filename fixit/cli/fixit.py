"""fixit CLI - thin Typer client for the agent server.

    fixit run "make the failing tests pass" --repo ./sample_repo
    fixit status <run_id>
    fixit history
    fixit health

Server URL from FIXIT_URL (default http://localhost:8000). Every request carries an
X-Request-ID header so a CLI invocation can be traced in Kibana.
"""
from __future__ import annotations

import os
import sys
from uuid import uuid4

import httpx
import typer

app = typer.Typer(help="fixit: an agentic coding tool with full observability", no_args_is_help=True)


def _url() -> str:
    return os.environ.get("FIXIT_URL", "http://localhost:8000").rstrip("/")


def _headers() -> dict:
    return {"X-Request-ID": uuid4().hex[:12]}


def _print_result(r: dict) -> None:
    typer.echo(f"run_id     : {r['run_id']}")
    typer.echo(f"outcome    : {r['outcome']}")
    typer.echo(f"iterations : {r['iterations']}")
    typer.echo(f"duration   : {r['duration_s']:.2f} s")
    typer.echo(f"tokens     : {r['tokens_in']} in / {r['tokens_out']} out")
    typer.echo(f"cost       : ${r['cost_usd']:.4f}")
    if r.get("changed_files"):
        typer.echo(f"changed    : {', '.join(r['changed_files'])}")
    if r.get("error"):
        typer.echo(f"error      : {r['error_type']}: {r['error']}")
    if r.get("summary"):
        typer.echo(f"summary    : {r['summary']}")


@app.command()
def run(
    task: str = typer.Argument(..., help="What the agent should do"),
    repo: str = typer.Option("/workspace", "--repo", help="Repo path (./sample_repo is mounted at /workspace in the agent container)"),
    timeout: float = typer.Option(900.0, help="Seconds to wait for the task"),
):
    """Send a task to the agent and print the result."""
    try:
        resp = httpx.post(f"{_url()}/tasks", json={"task": task, "repo": repo}, headers=_headers(), timeout=timeout)
    except httpx.HTTPError as e:
        typer.echo(f"error: cannot reach agent at {_url()}: {e}", err=True)
        raise typer.Exit(2)
    if resp.status_code == 400 or resp.status_code == 422:
        typer.echo(f"error {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    body = resp.json()
    _print_result(body)
    if resp.status_code >= 500:
        typer.echo(f"(server returned HTTP {resp.status_code})", err=True)
        raise typer.Exit(1)
    raise typer.Exit(0 if body["outcome"] == "success" else 1)


@app.command()
def status(run_id: str):
    """Fetch a previous task result by run_id."""
    resp = httpx.get(f"{_url()}/tasks/{run_id}", headers=_headers(), timeout=10)
    if resp.status_code == 404:
        typer.echo(f"unknown run_id {run_id}", err=True)
        raise typer.Exit(1)
    resp.raise_for_status()
    _print_result(resp.json())


@app.command()
def history(limit: int = typer.Option(20, help="Number of recent tasks")):
    """List recent tasks (newest first)."""
    resp = httpx.get(f"{_url()}/tasks", params={"limit": limit}, headers=_headers(), timeout=10)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        typer.echo("no tasks yet")
        return
    typer.echo(f"{'run_id':<14}{'outcome':<10}{'iter':>5}{'dur_s':>8}{'cost_usd':>10}")
    for r in rows:
        typer.echo(f"{r['run_id']:<14}{r['outcome']:<10}{r['iterations']:>5}{r['duration_s']:>8.2f}{r['cost_usd']:>10.4f}")


@app.command()
def health():
    """Check the agent server."""
    try:
        resp = httpx.get(f"{_url()}/health", headers=_headers(), timeout=5)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        typer.echo(f"agent at {_url()} is DOWN: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(resp.json())


if __name__ == "__main__":
    sys.exit(app())
