"""Symbiote CLI — Typer app with Rich output."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import EntityNotFoundError, SymbioteError
from symbiote.core.identity import IdentityManager
from symbiote.core.session import SessionManager
from symbiote.memory.store import MemoryStore

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(name="symbiote", help="Symbiote — Kernel for persistent cognitive entities")

session_app = typer.Typer(help="Session management commands")
memory_app = typer.Typer(help="Memory management commands")
export_app = typer.Typer(help="Export commands")

app.add_typer(session_app, name="session")
app.add_typer(memory_app, name="memory")
app.add_typer(export_app, name="export")

# ── shared state ───────────────────────────────────────────────────────────

_db_path_option: Path | None = None


def _get_db_path() -> Path:
    if _db_path_option is not None:
        return _db_path_option
    return KernelConfig().db_path


def _make_storage() -> SQLiteAdapter:
    adapter = SQLiteAdapter(db_path=_get_db_path())
    adapter.init_schema()
    return adapter


# ── callback (global option) ──────────────────────────────────────────────


@app.callback()
def main(
    db_path: str | None = typer.Option(None, "--db-path", help="Path to SQLite database"),
) -> None:
    """Symbiote CLI."""
    global _db_path_option
    _db_path_option = Path(db_path) if db_path is not None else None


# ── create ─────────────────────────────────────────────────────────────────


@app.command()
def create(
    name: str = typer.Option(..., "--name", help="Symbiote name"),
    role: str = typer.Option(..., "--role", help="Symbiote role"),
    persona_json: str | None = typer.Option(None, "--persona-json", help="Persona as JSON string"),
) -> None:
    """Create a new symbiote."""
    storage = _make_storage()
    try:
        persona = json.loads(persona_json) if persona_json else None
        mgr = IdentityManager(storage=storage)
        sym = mgr.create(name=name, role=role, persona=persona)
        console.print(f"Created symbiote: {sym.id}")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        storage.close()


# ── list ───────────────────────────────────────────────────────────────────


@app.command("list")
def list_symbiotes() -> None:
    """List all symbiotes."""
    storage = _make_storage()
    try:
        mgr = IdentityManager(storage=storage)
        symbiotes = mgr.list_all()

        table = Table(title="Symbiotes")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Role")
        table.add_column("Status")

        for sym in symbiotes:
            table.add_row(sym.id, sym.name, sym.role, sym.status)

        console.print(table)
    finally:
        storage.close()


# ── session start ──────────────────────────────────────────────────────────


@session_app.command("start")
def session_start(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    goal: str | None = typer.Option(None, "--goal", help="Session goal"),
) -> None:
    """Start a new session for a symbiote."""
    storage = _make_storage()
    try:
        # Verify symbiote exists
        id_mgr = IdentityManager(storage=storage)
        sym = id_mgr.get(symbiote_id)
        if sym is None:
            err_console.print(f"[red]Error:[/red] Symbiote {symbiote_id!r} not found")
            raise typer.Exit(code=1) from None

        sess_mgr = SessionManager(storage=storage)
        session = sess_mgr.start(symbiote_id=symbiote_id, goal=goal)
        console.print(f"Started session: {session.id}")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        storage.close()


# ── session close ──────────────────────────────────────────────────────────


@session_app.command("close")
def session_close(
    session_id: str = typer.Argument(help="Session ID"),
) -> None:
    """Close a session."""
    storage = _make_storage()
    try:
        sess_mgr = SessionManager(storage=storage)
        session = sess_mgr.close(session_id=session_id)
        console.print(
            Panel(
                f"Session [cyan]{session.id}[/cyan] closed.\n"
                f"Summary: {session.summary or 'No messages'}",
                title="Session Closed",
            )
        )
    except EntityNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        storage.close()


# ── message ────────────────────────────────────────────────────────────────


@app.command()
def message(
    session_id: str = typer.Argument(help="Session ID"),
    content: str = typer.Argument(help="Message content"),
) -> None:
    """Send a message to a session (stores and confirms)."""
    storage = _make_storage()
    try:
        sess_mgr = SessionManager(storage=storage)
        msg = sess_mgr.add_message(session_id=session_id, role="user", content=content)
        console.print(f"Message stored: {msg.id}")
    except EntityNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        storage.close()


# ── memory search ──────────────────────────────────────────────────────────


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(help="Search query"),
    scope: str | None = typer.Option(None, "--scope", help="Memory scope filter"),
    limit: int = typer.Option(10, "--limit", help="Max results"),
) -> None:
    """Search memory entries."""
    storage = _make_storage()
    try:
        mem = MemoryStore(storage=storage)
        results = mem.search(query=query, scope=scope, limit=limit)

        if not results:
            console.print("No memories found.")
            return

        table = Table(title="Memory Search Results")
        table.add_column("ID", style="cyan")
        table.add_column("Type")
        table.add_column("Scope")
        table.add_column("Content", max_width=60)

        for entry in results:
            table.add_row(entry.id, entry.type, entry.scope, entry.content[:60])

        console.print(table)
    finally:
        storage.close()


# ── export session ─────────────────────────────────────────────────────────


@export_app.command("session")
def export_session(
    session_id: str = typer.Argument(help="Session ID"),
) -> None:
    """Export a session as Markdown to stdout."""
    storage = _make_storage()
    try:
        export_svc = ExportService(storage=storage)
        output = export_svc.export_session(session_id)
        console.print(Panel(output, title="Session Export (Markdown)"))

    except EntityNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        storage.close()


if __name__ == "__main__":
    app()
