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
    external_key: str | None = typer.Option(None, "--external-key", help="External key for session lookup"),
) -> None:
    """Start a new session for a symbiote."""
    kernel = _make_kernel()
    try:
        session = kernel.start_session(
            symbiote_id=symbiote_id, goal=goal, external_key=external_key
        )
        console.print(f"Started session: [cyan]{session.id}[/cyan]")
        if session.external_key:
            console.print(f"External key: [dim]{session.external_key}[/dim]")
    except SymbioteError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    finally:
        kernel.shutdown()


@session_app.command("find")
def session_find(
    external_key: str = typer.Argument(help="External key to search"),
) -> None:
    """Find a session by external key."""
    kernel = _make_kernel()
    try:
        session = kernel._sessions.find_by_external_key(external_key)
        if session is None:
            console.print("No session found for that key.")
            return
        console.print(f"Session: [cyan]{session.id}[/cyan]")
        console.print(f"Symbiote: {session.symbiote_id}")
        console.print(f"Status: {session.status}")
        console.print(f"External key: [dim]{session.external_key}[/dim]")
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


# ── interactive chat (B-2) ────────────────────────────────────────────────


@app.command("interactive")
def interactive_chat(
    symbiote_id: str = typer.Argument(help="Symbiote ID (or name)"),
    goal: str | None = typer.Option(None, "--goal", help="Session goal"),
) -> None:
    """Interactive chat mode — continuous input/output loop with a symbiote."""
    kernel = _make_kernel(with_llm=True)
    try:
        # Resolve by name if not a UUID
        sym = kernel.get_symbiote(symbiote_id) or kernel.find_symbiote_by_name(symbiote_id)
        if sym is None:
            err_console.print(f"[red]Error:[/red] Symbiote '{symbiote_id}' not found")
            raise typer.Exit(code=1)

        session = kernel.start_session(symbiote_id=sym.id, goal=goal)
        console.print(Panel(
            f"Symbiote: [cyan]{sym.name}[/cyan] ({sym.role})\n"
            f"Session: [dim]{session.id[:8]}[/dim]\n"
            f"Type [bold]/quit[/bold] to exit, [bold]/reflect[/bold] to reflect.",
            title="Interactive Chat",
            border_style="blue",
        ))

        while True:
            try:
                user_input = console.input("[bold green]> [/bold green]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Closing session...[/dim]")
                break

            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped in ("/quit", "/exit", "/q"):
                break
            if stripped == "/reflect":
                result = kernel.capabilities.reflect(sym.id, session.id)
                console.print(Panel(
                    f"Messages: {result.get('message_count', 0)}\n"
                    f"Summary: {(result.get('summary') or 'No summary')[:200]}",
                    title="Reflection",
                ))
                continue

            response = kernel.message(session_id=session.id, content=stripped)
            if isinstance(response, dict):
                text = response.get("text", "")
                if text:
                    console.print(Markdown(text))
                for tr in response.get("tool_results", []):
                    status = "[green]OK[/green]" if tr.get("success") else "[red]FAIL[/red]"
                    console.print(f"  Tool {tr['tool_id']}: {status}")
            else:
                console.print(Markdown(str(response)))

        kernel.close_session(session.id)
        console.print("[dim]Session closed.[/dim]")
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


# ── init / discover ────────────────────────────────────────────────────────

_SYMBIOTE_CONFIG_DIR = ".symbiote"
_SYMBIOTE_CONFIG_FILE = ".symbiote/config"


def _read_symbiote_config(path: Path = Path(".")) -> dict:
    """Read .symbiote/config from the project root."""
    cfg_file = path / _SYMBIOTE_CONFIG_FILE
    if not cfg_file.exists():
        err_console.print("[red]No .symbiote/config found.[/] Run [bold]symbiote init[/] first.")
        raise typer.Exit(1)
    import configparser

    cp = configparser.ConfigParser()
    cp.read(str(cfg_file))
    return dict(cp["symbiote"]) if "symbiote" in cp else {}


def _write_symbiote_config(cfg: dict, path: Path = Path(".")) -> None:
    """Write .symbiote/config to the project root."""
    import configparser

    cfg_dir = path / _SYMBIOTE_CONFIG_DIR
    cfg_dir.mkdir(exist_ok=True)
    cp = configparser.ConfigParser()
    cp["symbiote"] = cfg
    with open(path / _SYMBIOTE_CONFIG_FILE, "w") as f:
        cp.write(f)


@app.command()
def init(
    server: str = typer.Option(
        None, "--server", "-s", help="Symbiote server URL (default: http://localhost:8000)"
    ),
    api_key: str = typer.Option(None, "--api-key", "-k", help="API key (sk-symbiote_...)"),
    name: str = typer.Option(None, "--name", "-n", help="Symbiote name"),
    role: str = typer.Option("assistant", "--role", "-r", help="Symbiote role"),
    symbiote_id: str = typer.Option(None, "--id", help="Existing Symbiote ID to link (skip creation)"),
) -> None:
    """Initialize a Symbiote project — links a local repo to a Symbiote on a server."""
    console.print(Panel("[bold cyan]symbiote init[/]", expand=False))

    # Prompt for missing values
    if server is None:
        server = typer.prompt("Server URL", default="http://localhost:8000")
    if api_key is None:
        api_key = typer.prompt("API key", hide_input=True)
    if name is None and symbiote_id is None:
        name = typer.prompt("Symbiote name")

    import urllib.error
    import urllib.request

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if symbiote_id:
        # Verify existing symbiote
        req = urllib.request.Request(
            f"{server.rstrip('/')}/symbiotes/{symbiote_id}",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                import json as _json
                data = _json.loads(resp.read())
                name = data.get("name", symbiote_id)
        except urllib.error.HTTPError as exc:
            err_console.print(f"[red]Failed to fetch symbiote:[/] HTTP {exc.code}")
            raise typer.Exit(1) from exc
        except Exception as exc:
            err_console.print(f"[red]Connection error:[/] {exc}")
            raise typer.Exit(1) from exc
    else:
        # Create new symbiote
        import json as _json

        payload = _json.dumps({"name": name, "role": role}).encode()
        req = urllib.request.Request(
            f"{server.rstrip('/')}/symbiotes",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
                symbiote_id = data["id"]
        except urllib.error.HTTPError as exc:
            err_console.print(f"[red]Failed to create symbiote:[/] HTTP {exc.code}: {exc.read().decode()}")
            raise typer.Exit(1) from exc
        except Exception as exc:
            err_console.print(f"[red]Connection error:[/] {exc}")
            raise typer.Exit(1) from exc

    _write_symbiote_config({
        "server": server.rstrip("/"),
        "api_key": api_key,
        "id": symbiote_id,
        "name": name or symbiote_id,
    })

    console.print(f"[green]✓[/] Connected to {server}")
    console.print(f"[green]✓[/] Symbiote: [bold]{name}[/] ({symbiote_id[:8]}...)")
    console.print(f"[green]✓[/] Config saved to [dim]{_SYMBIOTE_CONFIG_FILE}[/]")
    console.print("\nNext: [bold]symbiote discover .[/]")


@app.command()
def discover(
    source_path: str = typer.Argument(".", help="Repository path to scan"),
    url: str | None = typer.Option(
        None, "--url", "-u",
        help="Live server URL to fetch /openapi.json from (e.g. http://localhost:8000). "
             "Uses operationId as tool_id and captures full parameter schemas.",
    ),
) -> None:
    """Scan a repository for APIs and register discovered tools.

    Use --url to fetch the live OpenAPI spec from a running server — this
    produces semantic tool IDs (from operationId) and complete parameter schemas.
    """
    cfg = _read_symbiote_config()

    server = cfg.get("server", "http://localhost:8000")
    api_key = cfg.get("api_key", "")
    symbiote_id = cfg.get("id", "")
    symbiote_name = cfg.get("name", symbiote_id)

    if not symbiote_id:
        err_console.print("[red]No symbiote ID in config.[/] Run [bold]symbiote init[/] first.")
        raise typer.Exit(1)

    import json as _json
    import urllib.error
    import urllib.request

    abs_path = str(Path(source_path).resolve())
    body: dict = {"source_path": abs_path}
    if url:
        body["url"] = url.rstrip("/")
    payload = _json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    req = urllib.request.Request(
        f"{server}/symbiotes/{symbiote_id}/discover",
        data=payload,
        headers=headers,
        method="POST",
    )

    if url:
        console.print(f"Scanning [dim]{abs_path}[/] + live OpenAPI from [dim]{url}[/] for [bold]{symbiote_name}[/]...")
    else:
        console.print(f"Scanning [dim]{abs_path}[/] for [bold]{symbiote_name}[/]...")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_console.print(f"[red]Discovery failed:[/] HTTP {exc.code}: {exc.read().decode()}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        err_console.print(f"[red]Connection error:[/] {exc}")
        raise typer.Exit(1) from exc

    count = data.get("discovered", 0)
    errors = data.get("errors", [])

    console.print(f"[green]✓[/] {count} tool(s) discovered")

    if count > 0:
        table = Table(title="Discovered Tools")
        table.add_column("Tool ID", style="cyan")
        table.add_column("Method", style="dim")
        table.add_column("Endpoint")
        table.add_column("Source", style="dim", max_width=40)
        for t in data.get("tools", []):
            table.add_row(
                t["tool_id"],
                t.get("method") or t.get("handler_type", ""),
                t.get("url_template") or "",
                (t.get("source_path") or "")[-40:],
            )
        console.print(table)

    if errors:
        console.print(f"[yellow]Warnings:[/] {len(errors)} scan error(s)")
        for e in errors[:3]:
            console.print(f"  [dim]{e}[/]")

    console.print(f"\nReview and approve at: [bold]{server}[/]")


@app.command()
def classify(
    approve: str = typer.Option(None, "--approve", "-a", help="Tags to approve (comma-separated)"),
    disable_rest: bool = typer.Option(False, "--disable-rest", help="Disable non-matching pending tools"),
    summary: bool = typer.Option(False, "--summary", "-s", help="Show tag summary, no changes"),
    reset: bool = typer.Option(False, "--reset", help="Reset all disabled tools back to pending"),
) -> None:
    """Auto-approve/disable discovered tools by OpenAPI tags."""
    cfg = _read_symbiote_config()
    server = cfg.get("server", "http://localhost:8000")
    api_key = cfg.get("api_key", "")
    symbiote_id = cfg.get("id", "")
    symbiote_name = cfg.get("name", symbiote_id)

    if not symbiote_id:
        err_console.print("[red]No symbiote ID in config.[/] Run [bold]symbiote init[/] first.")
        raise typer.Exit(1)

    import json as _json
    import urllib.error
    import urllib.request
    from collections import Counter

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if reset:
        req = urllib.request.Request(
            f"{server}/symbiotes/{symbiote_id}/discovered-tools/reset",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            err_console.print(f"[red]Reset failed:[/] HTTP {exc.code}: {exc.read().decode()}")
            raise typer.Exit(1) from exc
        except Exception as exc:
            err_console.print(f"[red]Connection error:[/] {exc}")
            raise typer.Exit(1) from exc
        console.print(f"[green]✓[/] Reset {data['reset']} tool(s) back to pending")
        return

    if summary or not approve:
        # Fetch all discovered tools and show tag summary
        req = urllib.request.Request(
            f"{server}/symbiotes/{symbiote_id}/discovered-tools",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                tools = _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            err_console.print(f"[red]Failed:[/] HTTP {exc.code}: {exc.read().decode()}")
            raise typer.Exit(1) from exc
        except Exception as exc:
            err_console.print(f"[red]Connection error:[/] {exc}")
            raise typer.Exit(1) from exc

        # Group by tag
        tag_status: dict[str, Counter] = {}
        no_tag_status: Counter = Counter()
        for t in tools:
            tags = t.get("tags", [])
            status = t.get("status", "pending")
            if tags:
                for tag in tags:
                    if tag not in tag_status:
                        tag_status[tag] = Counter()
                    tag_status[tag][status] += 1
            else:
                no_tag_status[status] += 1

        table = Table(title=f"Tag Summary — {symbiote_name}")
        table.add_column("Tag", style="cyan")
        table.add_column("Pending", justify="right")
        table.add_column("Approved", justify="right", style="green")
        table.add_column("Disabled", justify="right", style="red")
        table.add_column("Total", justify="right", style="bold")

        for tag in sorted(tag_status):
            c = tag_status[tag]
            total = sum(c.values())
            table.add_row(tag, str(c["pending"]), str(c["approved"]), str(c["disabled"]), str(total))

        if no_tag_status:
            c = no_tag_status
            total = sum(c.values())
            table.add_row("[dim](no tag)[/]", str(c["pending"]), str(c["approved"]), str(c["disabled"]), str(total))

        console.print(table)
        console.print(f"\nTotal: {len(tools)} tool(s)")

        if not approve:
            return

    # POST classify
    payload = _json.dumps({
        "approve_tags": [t.strip() for t in approve.split(",")],
        "disable_rest": disable_rest,
    }).encode()
    req = urllib.request.Request(
        f"{server}/symbiotes/{symbiote_id}/discovered-tools/classify",
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_console.print(f"[red]Classify failed:[/] HTTP {exc.code}: {exc.read().decode()}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        err_console.print(f"[red]Connection error:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]✓[/] Approved: {data['approved']}, Disabled: {data['disabled']}, Unchanged: {data['unchanged']}")


if __name__ == "__main__":
    app()
