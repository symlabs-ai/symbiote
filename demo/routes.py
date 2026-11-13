"""Routes behind the tabs: context, memory, learning, tools, execution,
skills, harness, web and system. Each one is a thin call into the kernel's
public API, so the code doubles as a reference for hosts.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from demo.server import get_kernel, get_runtime, symbiote_id
from demo.tools_custom import CUSTOM_IDS, HTTP_EXAMPLES
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS
from symbiote.memory.recall import rank_entries, terms

router = APIRouter(prefix="/api")

BUILTINS = {"fs_read", "fs_write", "fs_list", "bash", "search_memories", "search_knowledge",
            "spawn", "skill_view", "skill_manage", "get_tool_schema"}


def _sid(request: Request, which: str) -> str:
    return symbiote_id(get_runtime(request), which)


# ── context & model calls ──────────────────────────────────────────────


@router.get("/context")
def context(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, symbiote)
    session_id = rt.current_session(sid)
    last = rt.hook.last_context.get(session_id)
    calls = [c for c in rt.llm.find() if c["session_id"] == session_id and c["role"] == "main"]
    last_call = calls[-1] if calls else None
    return {"session_id": session_id, "context": last, "last_call": last_call}


@router.get("/llm/calls")
def llm_calls(request: Request, limit: int = 40, _k: Any = Depends(get_kernel)) -> list[dict]:
    rt = get_runtime(request)
    calls = sorted([*rt.llm.find(), *rt.aux_llm.find()], key=lambda c: c["started"])[-limit:]
    return [{k: v for k, v in c.items() if k != "messages"} for c in calls]


@router.get("/llm/calls/{call_id}")
def llm_call(call_id: str, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    found = rt.llm.find(call_id=call_id) or rt.aux_llm.find(call_id=call_id)
    if not found:
        raise HTTPException(404, "chamada não encontrada (o demo guarda as últimas 60)")
    return found[0]


# ── memory ─────────────────────────────────────────────────────────────


@router.get("/memory")
def memory(request: Request, symbiote: str = "sol", inactive: bool = False,
           kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, symbiote)
    items = kernel.inspector.list_memories(sid, include_inactive=inactive, limit=500)
    return {
        "items": items,
        "by_category": Counter(m["category"] for m in items if m["is_active"]),
        "by_type": Counter(m["type"] for m in items if m["is_active"]),
    }


class LearnBody(BaseModel):
    symbiote: str = "sol"
    content: str
    type: str = "preference"
    importance: float = 0.7


@router.post("/memory/learn")
def memory_learn(body: LearnBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    entry = kernel.capabilities.learn(sid, rt.current_session(sid), body.content, body.type, body.importance)
    rt.bus.publish("memory", action="learned", memory_id=entry.id, symbiote_id=sid)
    return entry.model_dump(mode="json")


class WhichBody(BaseModel):
    symbiote: str = "sol"


@router.post("/memory/{memory_id}/deactivate")
def memory_deactivate(memory_id: str, body: WhichBody, request: Request,
                      kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    kernel.memory.deactivate(memory_id, symbiote_id=sid)
    get_runtime(request).bus.publish("memory", action="deactivated", memory_id=memory_id)
    return {"ok": True}


class RecallBody(BaseModel):
    symbiote: str = "sol"
    query: str


@router.post("/memory/recall")
def memory_recall(body: RecallBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    """The same ranking ``get_relevant`` uses, without touching last_used_at."""
    sid = _sid(request, body.symbiote)
    entries = kernel.memory.search("", symbiote_id=sid, limit=500)
    ranked = rank_entries(entries, body.query, None, limit=8)
    query_terms = terms(body.query)
    return {
        "terms": sorted(query_terms),
        "results": [
            {"id": e.id, "content": e.content, "type": e.type, "importance": e.importance,
             "shared_terms": sorted(terms(e.content) & query_terms)}
            for e in ranked
        ],
        "other_symbiote": [
            e.content for e in rank_entries(
                kernel.memory.search("", symbiote_id=_sid(request, "researcher" if body.symbiote == "sol" else "sol"),
                                     limit=500), body.query, None, limit=3)
        ],
    }


# ── learning ───────────────────────────────────────────────────────────


@router.get("/learning")
def learning(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, symbiote)
    cfg = kernel.environment.get_config(sid)
    reports = kernel.inspector.list_dream_reports(sid, limit=10)
    last_real = next((r for r in reports if not r["dry_run"]), None)
    closed = kernel.inspector.list_sessions(sid, status="closed", limit=500)
    since_dream = [s for s in closed if not last_real or (s["ended_at"] or "") > (last_real["completed_at"] or "")]
    return {
        "last_close": rt.last_close if rt.last_close and rt.last_close.get("closed_session") else None,
        "scores": kernel.inspector.list_scores(sid, limit=30),
        "reflections": kernel.inspector.list_reflections(sid, limit=10),
        "dreams": reports,
        "config": {
            "reflection_mode": cfg.reflection_mode,
            "dream_mode": cfg.dream_mode,
            "dream_min_sessions": cfg.dream_min_sessions,
            "dream_max_llm_calls": cfg.dream_max_llm_calls,
        },
        "sessions_since_dream": len(since_dream),
    }


class LearningConfigBody(BaseModel):
    symbiote: str = "sol"
    reflection_mode: str | None = None
    dream_mode: str | None = None
    dream_min_sessions: int | None = None


@router.post("/learning/config")
def learning_config(body: LearningConfigBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    fields = body.model_dump(exclude={"symbiote"}, exclude_none=True)
    cfg = kernel.environment.configure(symbiote_id=_sid(request, body.symbiote), **fields)
    return {"reflection_mode": cfg.reflection_mode, "dream_mode": cfg.dream_mode,
            "dream_min_sessions": cfg.dream_min_sessions}


class DreamBody(BaseModel):
    symbiote: str = "sol"
    dry_run: bool = False


@router.post("/dream")
async def dream(body: DreamBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    rt = get_runtime(request)
    rt.bus.publish("dream", state="start", dry_run=body.dry_run, symbiote_id=sid)
    report = await asyncio.to_thread(kernel.dream, sid, dry_run=body.dry_run)
    data = report.model_dump(mode="json")
    rt.bus.publish("dream", state="end", dry_run=body.dry_run, symbiote_id=sid,
                   phases=[p["phase"] for p in data["phases"]])
    return data


# ── tools ──────────────────────────────────────────────────────────────


def _source(tool_id: str, descriptor: Any, rt: Any, discovered: set[str]) -> str:
    if tool_id in BUILTINS:
        return "builtin"
    if tool_id in rt.mcp_tools:
        return "mcp"
    if tool_id in rt.web_tools:
        return "web"
    if tool_id in discovered:
        return "descoberta"
    if tool_id in CUSTOM_IDS:
        return "python"
    if descriptor is not None and descriptor.handler_type == "http":
        return "http"
    return descriptor.handler_type if descriptor else "?"


@router.get("/tools")
def tools(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, symbiote)
    gateway = kernel.tool_gateway
    cfg = kernel.environment.get_config(sid)
    authorized = set(cfg.tools)
    visible = {d.tool_id for d in gateway.get_descriptors(cfg.tool_tags or None)}
    discovered = kernel.discovered_tools.list(sid)
    discovered_ids = {t.tool_id for t in discovered}
    rows = []
    for tool_id in sorted(gateway.list_tools()):
        d = gateway.get_descriptor(tool_id)
        rows.append({
            "tool_id": tool_id,
            "name": d.name if d else tool_id,
            "description": d.description if d else "",
            "risk": gateway.get_risk_level(tool_id),
            "tags": d.tags if d else [],
            "source": _source(tool_id, d, rt, discovered_ids),
            "visible": tool_id in visible,
            "authorized": tool_id in authorized,
        })
    return {
        "symbiote_id": sid,
        "tools": rows,
        "tool_tags": cfg.tool_tags,
        "available_tags": gateway.get_available_tags(),
        "tool_loading": cfg.tool_loading,
        "context_mode": cfg.context_mode,
        "discovered": [t.model_dump(mode="json") for t in discovered],
        "http_examples": [{k: e[k] for k in ("tool_id", "name", "description", "method", "url_template")}
                          for e in HTTP_EXAMPLES],
        "audit": kernel.inspector.audit_log(sid, limit=60),
        "approvals_enabled": rt.approvals_enabled,
        "pending_approvals": rt.broker.pending(),
        "mcp_error": rt.mcp_error,
        "web_error": rt.web_error,
        "pa_base": f"{rt.base_url}/pa",
    }


class AuthorizeBody(BaseModel):
    symbiote: str = "sol"
    tool_id: str
    authorized: bool


@router.post("/tools/authorize")
def tools_authorize(body: AuthorizeBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    if body.authorized:
        tools_now = kernel.environment.add_tools(sid, [body.tool_id])
    else:
        tools_now = kernel.environment.remove_tools(sid, [body.tool_id])
    get_runtime(request).bus.publish("tools", action="authorize", tool_id=body.tool_id,
                                     authorized=body.authorized, symbiote_id=sid)
    return {"tools": tools_now}


class TagsBody(BaseModel):
    symbiote: str = "sol"
    tags: list[str]


@router.post("/tools/tags")
def tools_tags(body: TagsBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    cfg = kernel.environment.configure(symbiote_id=_sid(request, body.symbiote), tool_tags=body.tags)
    return {"tool_tags": cfg.tool_tags}


class TryBody(BaseModel):
    symbiote: str = "sol"
    tool_id: str
    params: dict = {}


@router.post("/tools/try")
async def tools_try(body: TryBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    """Run a tool directly through the gateway: policy, hooks and audit apply."""
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    result = await kernel.tool_gateway.execute_async(sid, rt.current_session(sid), body.tool_id, body.params)
    output = result.output
    if isinstance(output, dict) and "png_base64" in output:
        output = {**output, "png_base64": f"<{len(output['png_base64'])} chars>"}
    return {"success": result.success, "output": output, "error": result.error}


class DiscoverBody(BaseModel):
    symbiote: str = "sol"


@router.post("/discovery/run")
def discovery_run(body: DiscoverBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    # Sync route (runs in the threadpool): discovery fetches this same
    # server's /pa/openapi.json with a blocking urlopen.
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    result = kernel.discover_tools(sid, str(rt.data_dir), url=f"{rt.base_url}/pa")
    found = [t for t in result.discovered if t.handler_type == "http"]
    rt.bus.publish("discovery", found=len(found), errors=result.errors, symbiote_id=sid)
    return {"found": [t.tool_id for t in found], "errors": result.errors}


class DiscoveryStatusBody(BaseModel):
    symbiote: str = "sol"
    tool_id: str | None = None
    tag: str | None = None
    status: str


@router.post("/discovery/status")
def discovery_status(body: DiscoveryStatusBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    repo = kernel.discovered_tools
    if body.tag:
        changed = [t.tool_id for t in repo.list(sid) if body.tag in t.tags]
        for tool_id in changed:
            repo.set_status(sid, tool_id, body.status)
    elif body.tool_id:
        if not repo.set_status(sid, body.tool_id, body.status):
            raise HTTPException(404, "tool descoberta não encontrada")
        changed = [body.tool_id]
    else:
        raise HTTPException(422, "informe tool_id ou tag")
    return {"changed": changed}


@router.post("/discovery/load")
def discovery_load(body: DiscoverBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    loaded = kernel.load_discovered_tools(sid, base_url=rt.base_url)
    rt.bus.publish("tools", action="loaded", tools=loaded, symbiote_id=sid)
    return {"loaded": loaded}


class HttpToolBody(BaseModel):
    symbiote: str = "sol"
    example: str | None = None
    tool_id: str | None = None
    name: str | None = None
    description: str | None = None
    method: str = "GET"
    url_template: str | None = None
    parameters: dict | None = None
    risk: str = "low"


@router.post("/http-tools")
def http_tool_add(body: HttpToolBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    if body.example:
        spec = next((e for e in HTTP_EXAMPLES if e["tool_id"] == body.example), None)
        if spec is None:
            raise HTTPException(404, "exemplo desconhecido")
    else:
        if not (body.tool_id and body.url_template):
            raise HTTPException(422, "tool_id e url_template são obrigatórios")
        spec = body.model_dump()
        spec["tags"] = ["utilidades"]
    descriptor = ToolDescriptor(
        tool_id=spec["tool_id"], name=spec.get("name") or spec["tool_id"],
        description=spec.get("description") or "", parameters=spec.get("parameters") or {},
        tags=spec.get("tags", []), handler_type="http", risk_level=spec.get("risk", "low"),
    )
    existing = kernel.tool_gateway.get_descriptor(descriptor.tool_id)
    if existing is not None and existing.handler_type != "http":
        raise HTTPException(409, f"{descriptor.tool_id} já é uma tool {existing.handler_type}")
    if existing is not None:
        kernel.remove_http_tool(sid, descriptor.tool_id)
    kernel.register_http_tool(sid, descriptor, HttpToolConfig(method=spec["method"], url_template=spec["url_template"]))
    get_runtime(request).bus.publish("tools", action="http_registered", tool_id=descriptor.tool_id, symbiote_id=sid)
    return {"tool_id": descriptor.tool_id}


@router.delete("/http-tools/{tool_id}")
def http_tool_remove(tool_id: str, request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    return {"removed": kernel.remove_http_tool(_sid(request, symbiote), tool_id)}


@router.get("/pa")
def personal_data(request: Request) -> dict:
    store = get_runtime(request).pa_store
    return {
        "tasks": store.all("SELECT * FROM tasks ORDER BY done, due = '', due"),
        "events": store.all("SELECT * FROM events ORDER BY start"),
        "expenses": store.all("SELECT * FROM expenses ORDER BY day DESC"),
        "outbox": store.all("SELECT * FROM outbox ORDER BY sent_at DESC"),
    }


# ── execution ──────────────────────────────────────────────────────────

CONFIG_FIELDS = (
    "tool_mode", "tool_loading", "context_mode", "max_tool_iterations", "tool_call_timeout",
    "loop_timeout", "memory_share", "knowledge_share", "prompt_caching", "planner_prompt",
    "evaluator_prompt", "context_strategy", "max_blocks",
)


@router.get("/config")
def config(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    cfg = kernel.environment.get_config(_sid(request, symbiote))
    return {
        "config": {f: getattr(cfg, f) for f in CONFIG_FIELDS},
        "native_tools": rt.native_tools,
        "semantic_llm": rt.settings.aux_model,
    }


class ConfigBody(BaseModel):
    symbiote: str = "sol"
    fields: dict[str, Any]


@router.post("/config")
def config_set(body: ConfigBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    fields = {k: v for k, v in body.fields.items() if k in CONFIG_FIELDS or k == "native_tools"}
    if "native_tools" in fields:
        rt.set_native_tools(bool(fields.pop("native_tools")))
    for key in ("planner_prompt", "evaluator_prompt"):
        if key in fields and fields[key] is None:
            fields[key] = ""  # "" clears the prompt; None would mean "unchanged"
    sid = _sid(request, body.symbiote)
    cfg = kernel.environment.configure(symbiote_id=sid, **fields)
    rt.bus.publish("config", symbiote_id=sid, fields=list(body.fields))
    return {"config": {f: getattr(cfg, f) for f in CONFIG_FIELDS}, "native_tools": rt.native_tools}


@router.get("/traces")
def traces(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> list[dict]:
    return kernel.inspector.list_traces(symbiote_id=_sid(request, symbiote), limit=30)


class WorkBody(BaseModel):
    symbiote: str = "sol"
    task: str


@router.post("/process")
async def process(body: WorkBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    return await asyncio.to_thread(kernel.capabilities.work, sid, rt.current_session(sid), body.task)


# ── skills ─────────────────────────────────────────────────────────────


@router.get("/skills")
def skills(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    from symbiote.skills import usage

    sid = _sid(request, symbiote)
    loader = kernel.skills_loader_for(sid)
    items = []
    if loader is not None:
        loader.refresh()
        for skill in loader.list_skills():
            meta = usage.read_meta(skill.path.parent) or {}
            items.append({
                "name": skill.name, "description": skill.description, "status": skill.status,
                "agent_created": skill.agent_created, "use_count": meta.get("use_count", 0),
                "patch_count": meta.get("patch_count", 0), "last_used_at": meta.get("last_used_at"),
            })
    cfg = kernel.environment.get_config(sid)
    return {
        "skills": items,
        "reviews": kernel.inspector.list_skill_reviews(sid, limit=20),
        "config": {k: getattr(cfg, k) for k in (
            "skill_review_enabled", "skill_injection_enabled", "skill_injection_mode",
            "skill_nudge_interval", "skill_auto_promote_threshold", "max_active_skills")},
        "root": str(get_runtime(request).data_dir / "skills"),
    }


@router.get("/skills/{name}")
def skill_detail(name: str, request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    loader = kernel.skills_loader_for(_sid(request, symbiote))
    skill = loader.peek(name) if loader else None
    if skill is None:
        raise HTTPException(404, "skill não encontrada")
    return {"name": skill.name, "status": skill.status, "content": skill.path.read_text(encoding="utf-8")}


class SkillStatusBody(BaseModel):
    symbiote: str = "sol"
    status: str


@router.post("/skills/{name}/status")
def skill_status(name: str, body: SkillStatusBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    from symbiote.skills import usage

    loader = kernel.skills_loader_for(_sid(request, body.symbiote))
    skill = loader.peek(name) if loader else None
    if skill is None:
        raise HTTPException(404, "skill não encontrada")
    usage.set_status(skill.path.parent, body.status)
    loader.refresh()
    return {"name": name, "status": body.status}


class SkillConfigBody(BaseModel):
    symbiote: str = "sol"
    fields: dict[str, Any]


@router.post("/skills/config")
def skills_config(body: SkillConfigBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    allowed = {"skill_review_enabled", "skill_injection_enabled", "skill_injection_mode", "skill_nudge_interval"}
    fields = {k: v for k, v in body.fields.items() if k in allowed}
    kernel.environment.configure(symbiote_id=_sid(request, body.symbiote), **fields)
    return {"ok": True, **fields}


# ── harness ────────────────────────────────────────────────────────────


@router.get("/harness")
def harness(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, symbiote)
    scores = kernel.inspector.list_scores(sid, limit=200)
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    recent = [s for s in scores if (s["computed_at"] or "") >= week_ago]
    final = [s["final_score"] for s in recent if s["final_score"] is not None]
    tuning = kernel.tune_parameters(sid, days=7)
    versions = kernel.harness_versions
    components = []
    for component in EVOLVABLE_COMPONENTS:
        components.append({
            "component": component,
            "default_text": kernel.harness_default_text(component),
            "active": versions.get_active_version(sid, component),
            "baseline": versions.get_baseline(sid, component),
            "versions": versions.list_versions(sid, component, limit=10),
        })
    return {
        "scores": scores[:60],
        "evolution_readiness": {
            "failed": sum(1 for v in final if v < 0.5), "failed_needed": 5,
            "succeeded": sum(1 for v in final if v >= 0.8), "succeeded_needed": 3,
        },
        "tuning": {"tier": tuning.tier, "session_count": tuning.session_count,
                   "adjustments": tuning.adjustments, "reasons": tuning.reasons,
                   "tiers": [{"tier": 1, "min": 5}, {"tier": 2, "min": 20}, {"tier": 3, "min": 50}]},
        "components": components,
        "cross_learning": [c.__dict__ for c in kernel.cross_learning_candidates(sid, min_overlap=0.3)],
    }


class HarnessBody(BaseModel):
    symbiote: str = "sol"
    component: str | None = None


@router.post("/harness/evolve")
async def harness_evolve(body: HarnessBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    result = await asyncio.to_thread(kernel.evolve_harness, sid, body.component or "tool_instructions")
    return {"success": result.success, "new_version": result.new_version, "reason": result.reason,
            "guard_rail_failed": result.guard_rail_failed}


@router.post("/harness/rollback")
def harness_rollback(body: HarnessBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sid = _sid(request, body.symbiote)
    return {"rolled_back": kernel.harness_versions.rollback(sid, body.component or "tool_instructions")}


@router.post("/harness/tune")
def harness_tune(body: HarnessBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    result = kernel.tune_parameters(_sid(request, body.symbiote), days=7, apply=True)
    return {"applied": result.applied, "adjustments": result.adjustments, "reasons": result.reasons}


# ── web ────────────────────────────────────────────────────────────────


@router.get("/web")
def web(request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    shots = sorted((rt.shots_dir.glob("*.png")), reverse=True)[:12]
    return {
        "tools": rt.web_tools,
        "error": rt.web_error,
        "web_search": "web_search" in rt.web_tools,
        "screenshots": [f"/shots/{p.name}" for p in shots],
    }


# ── system ─────────────────────────────────────────────────────────────


@router.get("/sessions")
def sessions(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> list[dict]:
    return kernel.inspector.list_sessions(_sid(request, symbiote), limit=50)


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, kernel: Any = Depends(get_kernel)) -> list[dict]:
    return kernel.inspector.list_messages(session_id)


@router.get("/knowledge")
def knowledge(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> list[dict]:
    return [e.model_dump(mode="json") for e in kernel.knowledge.list_by_symbiote(_sid(request, symbiote))]


class KnowledgeBody(BaseModel):
    symbiote: str = "sol"
    name: str
    content: str
    type: str = "note"


@router.post("/knowledge")
def knowledge_add(body: KnowledgeBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    entry = kernel.knowledge.register_source(_sid(request, body.symbiote), body.name,
                                             content=body.content, entry_type=body.type)
    return entry.model_dump(mode="json")


@router.delete("/knowledge/{entry_id}")
def knowledge_remove(entry_id: str, kernel: Any = Depends(get_kernel)) -> dict:
    kernel.knowledge.remove(entry_id)
    return {"ok": True}


class KnowledgeQueryBody(BaseModel):
    symbiote: str = "sol"
    query: str


@router.post("/knowledge/query")
def knowledge_query(body: KnowledgeQueryBody, request: Request, kernel: Any = Depends(get_kernel)) -> list[dict]:
    return [e.model_dump(mode="json") for e in kernel.knowledge.query(_sid(request, body.symbiote), body.query, limit=5)]


@router.get("/persona")
def persona(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    sym = kernel.get_symbiote(_sid(request, symbiote))
    return {"id": sym.id, "name": sym.name, "role": sym.role, "persona": sym.persona_json}


class PersonaBody(BaseModel):
    symbiote: str = "sol"
    persona: dict


@router.post("/persona")
def persona_set(body: PersonaBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    sym = kernel.update_symbiote(_sid(request, body.symbiote), persona=body.persona)
    return {"persona": sym.persona_json}


class CapabilityBody(BaseModel):
    symbiote: str = "sol"
    name: str
    query: str = ""


@router.post("/capability")
async def capability(body: CapabilityBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    sid = _sid(request, body.symbiote)
    session_id = rt.current_session(sid)
    caps = kernel.capabilities
    if body.name == "teach":
        return {"result": caps.teach(sid, session_id, body.query)}
    if body.name == "show":
        return {"result": caps.show(sid, session_id, body.query)}
    if body.name == "reflect":
        return {"result": caps.reflect(sid, session_id)}
    if body.name == "work":
        return {"result": await asyncio.to_thread(caps.work, sid, session_id, body.query)}
    raise HTTPException(422, "use teach, show, reflect ou work")


class SecurityBody(BaseModel):
    test: str


@router.post("/security")
async def security(body: SecurityBody, request: Request, kernel: Any = Depends(get_kernel)) -> dict:
    """Show that the guards hold: SSRF to the cloud metadata IP and a file
    read outside the sandbox both fail inside the kernel."""
    rt = get_runtime(request)
    sid = rt.sol_id
    gateway = kernel.tool_gateway
    if body.test == "ssrf":
        tool_id = "teste_ssrf_metadados"
        if not gateway.has_tool(tool_id):
            gateway.register_http_tool(
                ToolDescriptor(tool_id=tool_id, name="Teste SSRF", description="só para o teste",
                               tags=["teste"]),
                HttpToolConfig(url_template="http://169.254.169.254/latest/meta-data/"),
            )
        kernel.environment.add_tools(sid, [tool_id])
        try:
            result = await gateway.execute_async(sid, None, tool_id, {})
        finally:
            kernel.environment.remove_tools(sid, [tool_id])
        return {"blocked": not result.success, "error": result.error}
    if body.test == "fs":
        result = await gateway.execute_async(sid, None, "fs_read", {"path": "../../../../etc/hostname"})
        return {"blocked": not result.success, "error": result.error, "output": result.output}
    if body.test == "policy":
        result = await gateway.execute_async(sid, None, "browser_navigate", {"url": "https://example.com"})
        return {"blocked": not result.success, "error": result.error}
    raise HTTPException(422, "testes: ssrf, fs, policy")


@router.post("/prune")
def prune(kernel: Any = Depends(get_kernel)) -> dict:
    return kernel.prune(dry_run=True, messages_days=0, traces_days=0)


@router.get("/system")
def system(request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    db = rt.data_dir / "demo.db"
    return {
        "db_path": str(db),
        "sandbox": str(rt.sandbox),
        "commands": [
            f".venv/bin/symbiote --db-path {db} list",
            f".venv/bin/symbiote --db-path {db} memory search \"\" --symbiote {rt.sol_id}",
            f"SYMBIOTE_DB_PATH={db} SYMBIOTE_LOCAL_ADMIN=1 .venv/bin/uvicorn symbiote.api.http:app --port 8008",
        ],
    }
