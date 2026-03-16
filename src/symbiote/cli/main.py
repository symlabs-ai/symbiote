"""Symbiote CLI — Typer app with Rich output.

All 6 value tracks (Learn, Teach, Chat, Work, Show, Reflect) are
accessible as commands, routed through SymbioteKernel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from symbiote.adapters.export.markdown import ExportService
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import EntityNotFoundError, SymbioteError
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.ports import LLMPort

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(name="symbiote", help="Symbiote — Kernel for persistent cognitive entities")

session_app = typer.Typer(help="Session management commands")
memory_app = typer.Typer(help="Memory management commands")
export_app = typer.Typer(help="Export commands")
tools_app = typer.Typer(help="Tool management commands")

app.add_typer(session_app, name="session")
app.add_typer(memory_app, name="memory")
app.add_typer(export_app, name="export")
app.add_typer(tools_app, name="tools")

# ── shared state ───────────────────────────────────────────────────────────

_db_path_option: Path | None = None
_llm_provider_option: str | None = None


def _make_kernel(with_llm: bool = False) -> SymbioteKernel:
    """Build a SymbioteKernel from config + optional LLM."""
    db_path = _db_path_option or KernelConfig().db_path
    provider = _llm_provider_option or os.environ.get("SYMBIOTE_LLM_PROVIDER", "mock")
    config = KernelConfig(db_path=db_path, llm_provider=provider)

    llm: LLMPort | None = None
    if with_llm:
        llm = _resolve_llm(provider)

    return SymbioteKernel(config=config, llm=llm)


def _resolve_llm(provider: str) -> LLMPort:
    """Resolve LLM adapter by provider name."""
    if provider == "mock":
        from symbiote.adapters.llm.base import MockLLMAdapter
        return MockLLMAdapter(default_response="I'm a mock symbiote. Configure a real LLM provider to get real responses.")

    # Default: try ForgeLLM
    from symbiote.adapters.llm.forge import ForgeLLMAdapter
    return ForgeLLMAdapter(provider=provider)


def _get_symbiote_id(kernel: SymbioteKernel, session_id: str) -> str:
    """Look up symbiote_id from session."""
    row = kernel._storage.fetch_one(
        "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
    )
    if row is None:
        raise EntityNotFoundError("Session", session_id)
    return row["symbiote_id"]


# ── callback (global options) ────────────────────────────────────────────


@app.callback()
def main(
    db_path: str | None = typer.Option(None, "--db-path", help="Path to SQLite database"),
    llm: str | None = typer.Option(None, "--llm", help="LLM provider (mock, anthropic, openai, openrouter)"),
) -> None:
    """Symbiote CLI."""
    global _db_path_option, _llm_provider_option
    _db_path_option = Path(db_path) if db_path is not None else None
    _llm_provider_option = llm


# ── create ─────────────────────────────────────────────────────────────────


@app.command()
def create(
    name: str = typer.Option(..., "--name", help="Symbiote name"),
    role: str = typer.Option(..., "--role", help="Symbiote role"),
    persona_json: str | None = typer.Option(None, "--persona-json", help="Persona as JSON string"),
) -> None:
    """Create a new symbiote."""
    kernel = _make_kernel()
    try:
        persona = json.loads(persona_json) if persona_json else None
        sym = kernel.create_symbiote(name=name, role=role, persona=persona)
        console.print(f"Created symbiote: [cyan]{sym.id}[/cyan]")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── list ───────────────────────────────────────────────────────────────────


@app.command("list")
def list_symbiotes() -> None:
    """List all symbiotes."""
    kernel = _make_kernel()
    try:
        symbiotes = kernel._identity.list_all()
        table = Table(title="Symbiotes")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Role")
        table.add_column("Status")
        for sym in symbiotes:
            table.add_row(sym.id, sym.name, sym.role, sym.status)
        console.print(table)
    finally:
        kernel.shutdown()


# ── session start ──────────────────────────────────────────────────────────


@session_app.command("start")
def session_start(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    goal: str | None = typer.Option(None, "--goal", help="Session goal"),
) -> None:
    """Start a new session for a symbiote."""
    kernel = _make_kernel()
    try:
        session = kernel.start_session(symbiote_id=symbiote_id, goal=goal)
        console.print(f"Started session: [cyan]{session.id}[/cyan]")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── session close ──────────────────────────────────────────────────────────


@session_app.command("close")
def session_close(
    session_id: str = typer.Argument(help="Session ID"),
) -> None:
    """Close a session (runs reflection, generates summary)."""
    kernel = _make_kernel()
    try:
        session = kernel.close_session(session_id=session_id)
        console.print(Panel(
            f"Session [cyan]{session.id}[/cyan] closed.\n"
            f"Summary: {session.summary or 'No messages'}",
            title="Session Closed",
        ))
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════════
# VALUE TRACKS — the 6 capabilities as CLI commands
# ══════════════════════════════════════════════════════════════════════════


# ── chat (Value Track: chat) ──────────────────────────────────────────────


@app.command()
def chat(
    session_id: str = typer.Argument(help="Session ID"),
    message: str = typer.Argument(help="Message to send"),
) -> None:
    """Chat with a symbiote — sends message, gets LLM response."""
    kernel = _make_kernel(with_llm=True)
    try:
        response = kernel.message(session_id=session_id, content=message)
        if isinstance(response, dict):
            console.print(Panel(response.get("text", ""), title="Assistant", border_style="green"))
            for tr in response.get("tool_results", []):
                status = "[green]OK[/green]" if tr.get("success") else "[red]FAIL[/red]"
                console.print(f"  Tool {tr['tool_id']}: {status} → {tr.get('output') or tr.get('error')}")
        else:
            console.print(Panel(response, title="Assistant", border_style="green"))
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── learn (Value Track: learn) ────────────────────────────────────────────


@app.command()
def learn(
    session_id: str = typer.Argument(help="Session ID"),
    content: str = typer.Argument(help="Fact or knowledge to remember"),
    fact_type: str = typer.Option("factual", "--type", help="Memory type (factual, preference, procedural, constraint)"),
    importance: float = typer.Option(0.7, "--importance", help="Importance 0.0-1.0"),
) -> None:
    """Teach the symbiote a durable fact (persists as long-term memory)."""
    kernel = _make_kernel()
    try:
        symbiote_id = _get_symbiote_id(kernel, session_id)
        entry = kernel.capabilities.learn(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content=content,
            fact_type=fact_type,
            importance=importance,
        )
        console.print(f"Learned: [cyan]{entry.id}[/cyan] [{entry.type}, importance={entry.importance}]")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── teach (Value Track: teach) ────────────────────────────────────────────


@app.command()
def teach(
    session_id: str = typer.Argument(help="Session ID"),
    query: str = typer.Argument(help="What to explain"),
) -> None:
    """Ask the symbiote to explain something (uses knowledge + memories)."""
    kernel = _make_kernel()
    try:
        symbiote_id = _get_symbiote_id(kernel, session_id)
        explanation = kernel.capabilities.teach(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query=query,
        )
        console.print(Markdown(explanation))
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── work (Value Track: work) ──────────────────────────────────────────────


@app.command()
def work(
    session_id: str = typer.Argument(help="Session ID"),
    task: str = typer.Argument(help="Task description (format: 'intent: description')"),
    intent: str | None = typer.Option(None, "--intent", help="Explicit intent for runner selection"),
) -> None:
    """Execute a task via the appropriate runner."""
    kernel = _make_kernel(with_llm=True)
    try:
        symbiote_id = _get_symbiote_id(kernel, session_id)
        result = kernel.capabilities.work(
            symbiote_id=symbiote_id,
            session_id=session_id,
            task=task,
            intent=intent,
        )
        if result["success"]:
            console.print(Panel(str(result["output"]), title=f"Work Result ({result['runner_type']})", border_style="green"))
        else:
            err_console.print(f"[red]Work failed:[/red] {result['error']}")
            raise typer.Exit(code=1) from None
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── show (Value Track: show) ──────────────────────────────────────────────


@app.command()
def show(
    session_id: str = typer.Argument(help="Session ID"),
    query: str = typer.Argument(help="What to show (memories, knowledge, session data)"),
) -> None:
    """Show relevant data as formatted Markdown."""
    kernel = _make_kernel()
    try:
        symbiote_id = _get_symbiote_id(kernel, session_id)
        output = kernel.capabilities.show(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query=query,
        )
        console.print(Markdown(output))
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── reflect (Value Track: reflect) ────────────────────────────────────────


@app.command()
def reflect(
    session_id: str = typer.Argument(help="Session ID"),
) -> None:
    """Reflect on the session — extract durable facts, generate summary."""
    kernel = _make_kernel()
    try:
        symbiote_id = _get_symbiote_id(kernel, session_id)
        result = kernel.capabilities.reflect(
            symbiote_id=symbiote_id,
            session_id=session_id,
        )
        table = Table(title="Reflection Result")
        table.add_column("Key", style="cyan")
        table.add_column("Value")
        table.add_row("Messages", str(result.get("message_count", 0)))
        table.add_row("Roles", str(result.get("role_counts", {})))
        table.add_row("Summary", (result.get("summary") or "No summary")[:200])
        console.print(table)
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ── memory search ──────────────────────────────────────────────────────────


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(help="Search query"),
    scope: str | None = typer.Option(None, "--scope", help="Memory scope filter"),
    limit: int = typer.Option(10, "--limit", help="Max results"),
) -> None:
    """Search memory entries."""
    kernel = _make_kernel()
    try:
        results = kernel._memory.search(query=query, scope=scope, limit=limit)
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
        kernel.shutdown()


# ── export session ─────────────────────────────────────────────────────────


@export_app.command("session")
def export_session(
    session_id: str = typer.Argument(help="Session ID"),
) -> None:
    """Export a session as Markdown to stdout."""
    kernel = _make_kernel()
    try:
        export_svc = ExportService(storage=kernel._storage)
        output = export_svc.export_session(session_id)
        console.print(Panel(output, title="Session Export (Markdown)"))
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════════
# TOOLS — tool management commands
# ══════════════════════════════════════════════════════════════════════════


@tools_app.command("add")
def tools_add(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    tool_id: str = typer.Option(..., "--id", help="Tool identifier (e.g. yn_publish)"),
    name: str = typer.Option(..., "--name", help="Human-readable tool name"),
    description: str = typer.Option(..., "--desc", help="What the tool does"),
    method: str = typer.Option("GET", "--method", help="HTTP method"),
    url: str = typer.Option(..., "--url", help="URL template (e.g. http://host/api/{id})"),
    params_json: str | None = typer.Option(None, "--params-json", help="JSON Schema for parameters"),
) -> None:
    """Register an HTTP tool for a symbiote."""
    from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

    kernel = _make_kernel()
    try:
        params = json.loads(params_json) if params_json else {}
        descriptor = ToolDescriptor(
            tool_id=tool_id,
            name=name,
            description=description,
            parameters=params,
            handler_type="http",
        )
        http_config = HttpToolConfig(method=method, url_template=url)
        kernel.tool_gateway.register_http_tool(descriptor, http_config)

        # Also add to environment config so PolicyGate authorizes it
        kernel.environment.configure(
            symbiote_id=symbiote_id,
            tools=kernel.environment.list_tools(symbiote_id) + [tool_id],
        )
        console.print(f"Registered tool [cyan]{tool_id}[/cyan] for symbiote {symbiote_id[:8]}")
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


@tools_app.command("list")
def tools_list(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
) -> None:
    """List tools available to a symbiote."""
    kernel = _make_kernel()
    try:
        authorized = kernel.environment.list_tools(symbiote_id)
        all_descriptors = kernel.tool_gateway.get_descriptors()

        table = Table(title=f"Tools for {symbiote_id[:8]}")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Authorized", justify="center")
        table.add_column("Description", max_width=50)

        for d in all_descriptors:
            auth = "[green]yes[/green]" if d.tool_id in authorized else "[red]no[/red]"
            table.add_row(d.tool_id, d.name, d.handler_type, auth, d.description[:50])

        console.print(table)
    finally:
        kernel.shutdown()


@tools_app.command("remove")
def tools_remove(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    tool_id: str = typer.Argument(help="Tool ID to remove"),
) -> None:
    """Remove a tool from a symbiote's authorized list."""
    kernel = _make_kernel()
    try:
        current = kernel.environment.list_tools(symbiote_id)
        updated = [t for t in current if t != tool_id]
        kernel.environment.configure(symbiote_id=symbiote_id, tools=updated)
        kernel.tool_gateway.unregister_tool(tool_id)
        console.print(f"Removed tool [cyan]{tool_id}[/cyan]")
    finally:
        kernel.shutdown()


@tools_app.command("exec")
def tools_exec(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    tool_id: str = typer.Argument(help="Tool ID to execute"),
    params_json: str = typer.Option("{}", "--params", help="Tool params as JSON"),
) -> None:
    """Manually execute a tool (for testing)."""
    kernel = _make_kernel()
    try:
        params = json.loads(params_json)
        result = kernel.tool_gateway.execute(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id=tool_id,
            params=params,
        )
        if result.success:
            console.print(Panel(str(result.output), title=f"Tool: {tool_id}", border_style="green"))
        else:
            err_console.print(f"[red]Failed:[/red] {result.error}")
            raise typer.Exit(code=1) from None
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]Invalid JSON:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


@tools_app.command("audit")
def tools_audit(
    symbiote_id: str = typer.Argument(help="Symbiote ID"),
    limit: int = typer.Option(20, "--limit", help="Max entries to show"),
) -> None:
    """Show tool execution audit log."""
    kernel = _make_kernel()
    try:
        log = kernel._policy_gate.get_audit_log(symbiote_id, limit=limit)
        if not log:
            console.print("No audit entries found.")
            return
        table = Table(title=f"Audit Log — {symbiote_id[:8]}")
        table.add_column("Time", style="dim")
        table.add_column("Tool", style="cyan")
        table.add_column("Action")
        table.add_column("Result")
        table.add_column("Session", max_width=12)
        for entry in log:
            table.add_row(
                entry.get("created_at", "")[:19],
                entry["tool_id"],
                entry["action"],
                entry["result"],
                (entry.get("session_id") or "")[:12],
            )
        console.print(table)
    finally:
        kernel.shutdown()


if __name__ == "__main__":
    app()
