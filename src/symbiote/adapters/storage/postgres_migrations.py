"""Versioned schema migrations of ``PostgresAdapter``.

Each migration is ``(version, name, sql script)``. Applied migrations are
recorded in ``schema_migrations``; a migration is never edited after it is
released: schema changes are appended as a new one (and mirrored in
``SQLiteAdapter._MIGRATIONS``; ``tests/unit/test_storage_schema_parity.py``
compares the two schemas).

Conventions that keep the kernel's portable SQL behaving as on SQLite:

* text columns are ``TEXT COLLATE "C"``: compared and ordered by code point,
  and ``LOWER`` changes ASCII letters only;
* ``REAL`` becomes ``DOUBLE PRECISION`` (SQLite's REAL is 8 bytes);
* booleans stay ``INTEGER`` 0/1 and timestamps stay ISO-8601 text;
* every table has ``rowid``, an identity column giving the insertion order
  (the adapter leaves it out of ``SELECT *`` results).
"""

from __future__ import annotations

_V1_BASELINE = """
CREATE TABLE symbiotes (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id           TEXT COLLATE "C" PRIMARY KEY,
    name         TEXT COLLATE "C" NOT NULL,
    role         TEXT COLLATE "C",
    owner_id     TEXT COLLATE "C",
    persona_json TEXT COLLATE "C",
    status       TEXT COLLATE "C" DEFAULT 'active',
    created_at   TEXT COLLATE "C",
    updated_at   TEXT COLLATE "C"
);

CREATE TABLE sessions (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id              TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id     TEXT COLLATE "C" REFERENCES symbiotes(id),
    goal            TEXT COLLATE "C",
    workspace_id    TEXT COLLATE "C",
    external_key    TEXT COLLATE "C",
    status          TEXT COLLATE "C" DEFAULT 'active',
    started_at      TEXT COLLATE "C",
    ended_at        TEXT COLLATE "C",
    summary         TEXT COLLATE "C",
    reflected_until TEXT COLLATE "C"
);

CREATE TABLE messages (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id         TEXT COLLATE "C" PRIMARY KEY,
    session_id TEXT COLLATE "C" REFERENCES sessions(id),
    role       TEXT COLLATE "C" NOT NULL,
    content    TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C"
);

CREATE TABLE memory_entries (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id           TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id  TEXT COLLATE "C" REFERENCES symbiotes(id),
    session_id   TEXT COLLATE "C",
    type         TEXT COLLATE "C" NOT NULL,
    scope        TEXT COLLATE "C" NOT NULL,
    content      TEXT COLLATE "C" NOT NULL,
    tags_json    TEXT COLLATE "C" DEFAULT '[]',
    importance   DOUBLE PRECISION DEFAULT 0.5,
    source       TEXT COLLATE "C",
    confidence   DOUBLE PRECISION DEFAULT 1.0,
    created_at   TEXT COLLATE "C",
    last_used_at TEXT COLLATE "C",
    is_active    INTEGER DEFAULT 1,
    category     TEXT COLLATE "C",
    updated_at   TEXT COLLATE "C"
);

CREATE TABLE knowledge_entries (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id TEXT COLLATE "C" REFERENCES symbiotes(id),
    name        TEXT COLLATE "C" NOT NULL,
    source_path TEXT COLLATE "C",
    content     TEXT COLLATE "C",
    type        TEXT COLLATE "C",
    tags_json   TEXT COLLATE "C" DEFAULT '[]',
    created_at  TEXT COLLATE "C"
);

CREATE TABLE workspaces (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id TEXT COLLATE "C" REFERENCES symbiotes(id),
    name        TEXT COLLATE "C" NOT NULL,
    root_path   TEXT COLLATE "C",
    type        TEXT COLLATE "C" DEFAULT 'general',
    created_at  TEXT COLLATE "C"
);

CREATE TABLE artifacts (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id           TEXT COLLATE "C" PRIMARY KEY,
    session_id   TEXT COLLATE "C" REFERENCES sessions(id),
    workspace_id TEXT COLLATE "C" REFERENCES workspaces(id),
    path         TEXT COLLATE "C",
    type         TEXT COLLATE "C",
    description  TEXT COLLATE "C",
    created_at   TEXT COLLATE "C"
);

CREATE TABLE environment_configs (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id                            TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id                   TEXT COLLATE "C" REFERENCES symbiotes(id),
    workspace_id                  TEXT COLLATE "C",
    tools_json                    TEXT COLLATE "C" DEFAULT '[]',
    services_json                 TEXT COLLATE "C" DEFAULT '[]',
    humans_json                   TEXT COLLATE "C" DEFAULT '[]',
    policies_json                 TEXT COLLATE "C" DEFAULT '{}',
    resources_json                TEXT COLLATE "C" DEFAULT '{}',
    tool_tags_json                TEXT COLLATE "C" DEFAULT '[]',
    tool_loading                  TEXT COLLATE "C" DEFAULT 'full',
    tool_mode                     TEXT COLLATE "C" DEFAULT 'brief',
    tool_loop                     INTEGER DEFAULT 1,
    prompt_caching                INTEGER DEFAULT 0,
    memory_share                  DOUBLE PRECISION DEFAULT 0.40,
    knowledge_share               DOUBLE PRECISION DEFAULT 0.25,
    max_tool_iterations           INTEGER DEFAULT 10,
    tool_call_timeout             DOUBLE PRECISION DEFAULT 30.0,
    loop_timeout                  DOUBLE PRECISION DEFAULT 300.0,
    context_mode                  TEXT COLLATE "C" DEFAULT 'packed',
    planner_prompt                TEXT COLLATE "C",
    evaluator_prompt              TEXT COLLATE "C",
    evaluator_criteria_json       TEXT COLLATE "C",
    context_strategy              TEXT COLLATE "C" DEFAULT 'hybrid',
    max_blocks                    INTEGER DEFAULT 20,
    dream_mode                    TEXT COLLATE "C" DEFAULT 'off',
    dream_max_llm_calls           INTEGER DEFAULT 10,
    dream_min_sessions            INTEGER DEFAULT 5,
    reflection_mode               TEXT COLLATE "C" DEFAULT 'keyword',
    reflection_max_tokens         INTEGER DEFAULT 4000,
    skill_review_enabled          INTEGER DEFAULT 0,
    skill_nudge_interval          INTEGER DEFAULT 10,
    max_active_skills             INTEGER DEFAULT 20,
    max_quarantine_skills         INTEGER DEFAULT 10,
    skill_auto_promote_threshold  INTEGER DEFAULT 3,
    skill_quarantine_timeout_days INTEGER DEFAULT 14,
    skill_injection_enabled       INTEGER DEFAULT 0,
    skill_injection_mode          TEXT COLLATE "C" DEFAULT 'full',
    skill_review_strict           INTEGER DEFAULT 0,
    skill_review_extra_criteria   TEXT COLLATE "C",
    memory_enabled                INTEGER DEFAULT 1
);

CREATE TABLE decisions (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT COLLATE "C" PRIMARY KEY,
    session_id  TEXT COLLATE "C" REFERENCES sessions(id),
    title       TEXT COLLATE "C" NOT NULL,
    description TEXT COLLATE "C",
    tags_json   TEXT COLLATE "C" DEFAULT '[]',
    created_at  TEXT COLLATE "C"
);

CREATE TABLE process_instances (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id           TEXT COLLATE "C" PRIMARY KEY,
    session_id   TEXT COLLATE "C" REFERENCES sessions(id),
    process_name TEXT COLLATE "C" NOT NULL,
    state        TEXT COLLATE "C" DEFAULT 'running',
    current_step TEXT COLLATE "C",
    logs_json    TEXT COLLATE "C" DEFAULT '[]',
    created_at   TEXT COLLATE "C",
    updated_at   TEXT COLLATE "C"
);

CREATE TABLE audit_log (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id TEXT COLLATE "C" REFERENCES symbiotes(id),
    session_id  TEXT COLLATE "C",
    tool_id     TEXT COLLATE "C" NOT NULL,
    action      TEXT COLLATE "C" NOT NULL,
    params_json TEXT COLLATE "C",
    result      TEXT COLLATE "C",
    created_at  TEXT COLLATE "C"
);

CREATE TABLE persona_audit (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id               TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id      TEXT COLLATE "C" NOT NULL,
    old_persona_json TEXT COLLATE "C" NOT NULL,
    new_persona_json TEXT COLLATE "C" NOT NULL,
    changed_at       TEXT COLLATE "C" NOT NULL
);

CREATE TABLE discovered_tools (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id              TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id     TEXT COLLATE "C" REFERENCES symbiotes(id),
    tool_id         TEXT COLLATE "C" NOT NULL,
    name            TEXT COLLATE "C" NOT NULL,
    description     TEXT COLLATE "C" NOT NULL DEFAULT '',
    handler_type    TEXT COLLATE "C" NOT NULL DEFAULT 'http',
    method          TEXT COLLATE "C",
    url_template    TEXT COLLATE "C",
    parameters_json TEXT COLLATE "C" DEFAULT '{}',
    status          TEXT COLLATE "C" NOT NULL DEFAULT 'pending',
    source_path     TEXT COLLATE "C",
    discovered_at   TEXT COLLATE "C" NOT NULL,
    approved_at     TEXT COLLATE "C",
    tags_json       TEXT COLLATE "C" DEFAULT '[]',
    risk_level      TEXT COLLATE "C" NOT NULL DEFAULT 'medium',
    UNIQUE (symbiote_id, tool_id)
);

CREATE TABLE execution_traces (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id               TEXT COLLATE "C" PRIMARY KEY,
    session_id       TEXT COLLATE "C" NOT NULL,
    symbiote_id      TEXT COLLATE "C" NOT NULL,
    total_iterations INTEGER,
    total_tool_calls INTEGER,
    total_elapsed_ms INTEGER,
    stop_reason      TEXT COLLATE "C",
    steps_json       TEXT COLLATE "C",
    tool_mode        TEXT COLLATE "C" DEFAULT 'brief',
    created_at       TEXT COLLATE "C" NOT NULL
);

CREATE TABLE session_scores (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id               TEXT COLLATE "C" PRIMARY KEY,
    session_id       TEXT COLLATE "C" NOT NULL,
    symbiote_id      TEXT COLLATE "C" NOT NULL,
    auto_score       DOUBLE PRECISION DEFAULT 0.0,
    user_score       DOUBLE PRECISION,
    final_score      DOUBLE PRECISION DEFAULT 0.0,
    stop_reason      TEXT COLLATE "C",
    total_iterations INTEGER DEFAULT 0,
    total_tool_calls INTEGER DEFAULT 0,
    computed_at      TEXT COLLATE "C" NOT NULL
);

CREATE TABLE harness_versions (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id             TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id    TEXT COLLATE "C" NOT NULL,
    component      TEXT COLLATE "C" NOT NULL,
    version        INTEGER NOT NULL,
    content        TEXT COLLATE "C" NOT NULL,
    avg_score      DOUBLE PRECISION DEFAULT 0.0,
    session_count  INTEGER DEFAULT 0,
    is_active      INTEGER DEFAULT 1,
    created_at     TEXT COLLATE "C" NOT NULL,
    parent_version INTEGER,
    UNIQUE (symbiote_id, component, version)
);

CREATE TABLE dream_reports (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id              TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id     TEXT COLLATE "C" NOT NULL REFERENCES symbiotes(id),
    started_at      TEXT COLLATE "C" NOT NULL,
    completed_at    TEXT COLLATE "C",
    dream_mode      TEXT COLLATE "C" NOT NULL,
    dry_run         INTEGER DEFAULT 0,
    total_llm_calls INTEGER DEFAULT 0,
    max_llm_calls   INTEGER DEFAULT 10,
    phases_json     TEXT COLLATE "C" DEFAULT '[]'
);

CREATE TABLE reflection_audit (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id                 TEXT COLLATE "C" PRIMARY KEY,
    session_id         TEXT COLLATE "C" NOT NULL,
    symbiote_id        TEXT COLLATE "C" NOT NULL,
    mode               TEXT COLLATE "C" NOT NULL,
    keyword_facts_json TEXT COLLATE "C" DEFAULT '[]',
    llm_facts_json     TEXT COLLATE "C" DEFAULT '[]',
    llm_error          TEXT COLLATE "C",
    created_at         TEXT COLLATE "C" NOT NULL
);

CREATE TABLE skill_review_audit (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id          TEXT COLLATE "C" PRIMARY KEY,
    session_id  TEXT COLLATE "C" NOT NULL,
    symbiote_id TEXT COLLATE "C" NOT NULL,
    trigger     TEXT COLLATE "C" NOT NULL,
    applied     INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 0,
    error       TEXT COLLATE "C",
    ops_json    TEXT COLLATE "C" DEFAULT '[]',
    created_at  TEXT COLLATE "C" NOT NULL
);

CREATE TABLE turn_contexts (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id              TEXT COLLATE "C" PRIMARY KEY,
    session_id      TEXT COLLATE "C" NOT NULL,
    symbiote_id     TEXT COLLATE "C" NOT NULL,
    memories_json   TEXT COLLATE "C" DEFAULT '[]',
    knowledge_json  TEXT COLLATE "C" DEFAULT '[]',
    skills_json     TEXT COLLATE "C" DEFAULT '[]',
    tokens_estimate INTEGER DEFAULT 0,
    created_at      TEXT COLLATE "C" NOT NULL
);

CREATE TABLE api_keys (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    id         TEXT COLLATE "C" PRIMARY KEY,
    tenant_id  TEXT COLLATE "C" NOT NULL,
    name       TEXT COLLATE "C" NOT NULL,
    key_hash   TEXT COLLATE "C" NOT NULL UNIQUE,
    key_prefix TEXT COLLATE "C" NOT NULL,
    role       TEXT COLLATE "C" NOT NULL DEFAULT 'user',
    is_active  INTEGER DEFAULT 1,
    created_at TEXT COLLATE "C" NOT NULL
);

CREATE TABLE http_tools (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    tool_id         TEXT COLLATE "C" PRIMARY KEY,
    symbiote_id     TEXT COLLATE "C" NOT NULL,
    descriptor_json TEXT COLLATE "C" NOT NULL,
    config_json     TEXT COLLATE "C" NOT NULL,
    created_at      TEXT COLLATE "C" NOT NULL
);

CREATE INDEX idx_discovered_tools_symbiote ON discovered_tools(symbiote_id, status);

CREATE INDEX idx_sessions_external_key ON sessions(external_key);

CREATE INDEX idx_traces_symbiote ON execution_traces(symbiote_id, created_at);

CREATE INDEX idx_scores_symbiote ON session_scores(symbiote_id, computed_at);

CREATE INDEX idx_scores_session ON session_scores(session_id);

CREATE INDEX idx_hv_active ON harness_versions(symbiote_id, component, is_active);

CREATE INDEX idx_dream_reports_symbiote ON dream_reports(symbiote_id, started_at);

CREATE INDEX idx_reflection_audit_symbiote ON reflection_audit(symbiote_id, created_at);

CREATE INDEX idx_reflection_audit_session ON reflection_audit(session_id);

CREATE INDEX idx_skill_review_audit_symbiote ON skill_review_audit(symbiote_id, created_at);

CREATE INDEX idx_skill_review_audit_session ON skill_review_audit(session_id);

CREATE UNIQUE INDEX idx_sessions_symbiote_external_key ON sessions(symbiote_id, external_key) WHERE external_key IS NOT NULL;

CREATE UNIQUE INDEX idx_environment_configs_scope ON environment_configs(symbiote_id, (COALESCE(workspace_id, '')));

CREATE INDEX idx_messages_session ON messages(session_id, created_at);

CREATE INDEX idx_memory_symbiote_active ON memory_entries(symbiote_id, is_active, type);

CREATE INDEX idx_sessions_symbiote_status ON sessions(symbiote_id, status, ended_at);

CREATE INDEX idx_traces_session ON execution_traces(session_id);

CREATE INDEX idx_turn_contexts_session ON turn_contexts(session_id, created_at);

CREATE INDEX idx_turn_contexts_symbiote ON turn_contexts(symbiote_id, created_at);

CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);

CREATE INDEX idx_api_keys_tenant ON api_keys(tenant_id);
"""

_V2_SESSION_LOCKS = """
CREATE TABLE session_locks (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    session_id TEXT COLLATE "C" PRIMARY KEY,
    owner      TEXT COLLATE "C" NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
"""

_V3_SKILL_LIBRARY = """
CREATE TABLE skill_files (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    scope      TEXT COLLATE "C" NOT NULL,
    path       TEXT COLLATE "C" NOT NULL,
    content    TEXT COLLATE "C" NOT NULL,
    digest     TEXT COLLATE "C" NOT NULL,
    updated_at TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (scope, path)
);

CREATE TABLE skill_scopes (
    rowid BIGINT GENERATED ALWAYS AS IDENTITY,
    scope      TEXT COLLATE "C" PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at TEXT COLLATE "C" NOT NULL
);
"""

_V4_CANCEL_RELAY = """
ALTER TABLE session_locks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
"""

_V5_TOOL_VISIBILITY = """
ALTER TABLE environment_configs ADD COLUMN tool_visibility TEXT COLLATE "C" DEFAULT 'tagged';
"""

_V6_SUBJECT_KEY = """
ALTER TABLE sessions ADD COLUMN subject_key TEXT COLLATE "C";

ALTER TABLE memory_entries ADD COLUMN subject_key TEXT COLLATE "C";

ALTER TABLE environment_configs ADD COLUMN learn_from_subject_sessions INTEGER DEFAULT 0;

CREATE INDEX idx_memory_symbiote_subject ON memory_entries(symbiote_id, subject_key, is_active);

CREATE INDEX idx_sessions_symbiote_subject ON sessions(symbiote_id, subject_key);
"""

_V7_HOST_OPTIONS = """
ALTER TABLE environment_configs ADD COLUMN skill_trial_listing INTEGER DEFAULT 0;

ALTER TABLE environment_configs ADD COLUMN dream_llm TEXT DEFAULT 'aux';

ALTER TABLE environment_configs ADD COLUMN session_summary_mode TEXT DEFAULT 'concat';

ALTER TABLE knowledge_entries ADD COLUMN auto_recall INTEGER DEFAULT 1;

ALTER TABLE session_scores ADD COLUMN scored_at_close INTEGER DEFAULT 1;
"""

MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "baseline", _V1_BASELINE),
    (2, "session_locks", _V2_SESSION_LOCKS),
    (3, "skill_library", _V3_SKILL_LIBRARY),
    (4, "cancel_relay", _V4_CANCEL_RELAY),
    (5, "tool_visibility", _V5_TOOL_VISIBILITY),
    (6, "subject_key", _V6_SUBJECT_KEY),
    (7, "host_options", _V7_HOST_OPTIONS),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def statements(script: str) -> list[str]:
    """Split a migration script into statements (none contains ``;``)."""
    return [part.strip() for part in script.split(";") if part.strip()]


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "statements"]
