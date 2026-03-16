"""ProcessEngine — orchestrates process definitions and instances."""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import ProcessInstance, _utcnow
from symbiote.core.ports import StoragePort

# ── Models ───────────────────────────────────────────────────────────────────


class ProcessStep(BaseModel):
    name: str
    description: str = ""
    handler: str = ""  # reference name, resolved at runtime


class ProcessDefinition(BaseModel):
    name: str
    description: str = ""
    steps: list[ProcessStep] = Field(default_factory=list)
    entry_criteria: str = ""
    reflection_policy: str = "on_completion"  # on_completion | never


# ── Default definitions ──────────────────────────────────────────────────────

DEFAULT_DEFINITIONS: list[ProcessDefinition] = [
    ProcessDefinition(
        name="chat_session",
        description="General conversational session",
        steps=[
            ProcessStep(name="greet", description="Greet the user"),
            ProcessStep(name="converse", description="Handle conversation"),
            ProcessStep(name="summarize", description="Summarize session"),
        ],
    ),
    ProcessDefinition(
        name="research_task",
        description="Research and gather information",
        steps=[
            ProcessStep(name="define_scope", description="Define research scope"),
            ProcessStep(name="gather", description="Gather information"),
            ProcessStep(name="analyze", description="Analyze findings"),
            ProcessStep(name="report", description="Produce report"),
        ],
    ),
    ProcessDefinition(
        name="artifact_generation",
        description="Generate an artifact (code, doc, etc.)",
        steps=[
            ProcessStep(name="plan", description="Plan the artifact"),
            ProcessStep(name="generate", description="Generate content"),
            ProcessStep(name="review", description="Review output"),
        ],
    ),
    ProcessDefinition(
        name="review_task",
        description="Review an existing artifact or process",
        steps=[
            ProcessStep(name="load", description="Load artifact for review"),
            ProcessStep(name="evaluate", description="Evaluate quality"),
            ProcessStep(name="feedback", description="Produce feedback"),
        ],
    ),
    ProcessDefinition(
        name="workspace_task",
        description="Perform workspace-level operations",
        steps=[
            ProcessStep(name="scan", description="Scan workspace"),
            ProcessStep(name="execute", description="Execute task"),
            ProcessStep(name="verify", description="Verify results"),
        ],
    ),
]


# ── Engine ───────────────────────────────────────────────────────────────────


class ProcessEngine:
    """Manages process definitions and orchestrates process instances."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage
        self._definitions: dict[str, ProcessDefinition] = {}
        # In-memory cache of active instances for fast advance/get
        self._instances: dict[str, ProcessInstance] = {}
        # Map instance_id -> definition name for step tracking
        self._instance_step_index: dict[str, int] = {}

        # Register default definitions
        for defn in DEFAULT_DEFINITIONS:
            self.register_process(defn)

    # ── Public API ───────────────────────────────────────────────────────

    def register_process(self, definition: ProcessDefinition) -> None:
        """Register a process definition."""
        self._definitions[definition.name] = definition

    def select(self, intent: str) -> ProcessDefinition | None:
        """Return the first process whose name matches intent (exact match)."""
        return self._definitions.get(intent)

    def list_definitions(self) -> list[str]:
        """Return registered process names."""
        return list(self._definitions.keys())

    def start(self, session_id: str, process_name: str) -> ProcessInstance:
        """Create and persist a process instance with state='running'."""
        defn = self._definitions.get(process_name)
        if defn is None:
            raise EntityNotFoundError("ProcessDefinition", process_name)
        first_step = defn.steps[0].name if defn.steps else None

        instance = ProcessInstance(
            session_id=session_id,
            process_name=process_name,
            state="running",
            current_step=first_step,
        )

        self._instances[instance.id] = instance
        self._instance_step_index[instance.id] = 0

        self._persist_instance(instance)
        return instance

    def advance(self, instance_id: str) -> ProcessInstance:
        """Move to next step; complete if no more steps. Raises EntityNotFoundError."""
        instance = self._instances.get(instance_id)
        if instance is None:
            raise EntityNotFoundError("ProcessInstance", instance_id)

        if instance.state != "running":
            raise ValidationError(
                f"Cannot advance process {instance_id!r}: state is '{instance.state}', expected 'running'"
            )

        defn = self._definitions.get(instance.process_name)
        step_index = self._instance_step_index.get(instance_id, 0)

        # Log the completed step
        completed_step = instance.current_step
        instance.logs.append(
            {
                "step": completed_step,
                "status": "completed",
                "timestamp": _utcnow().isoformat(),
            }
        )

        next_index = step_index + 1

        if defn and next_index < len(defn.steps):
            # Move to next step
            instance.current_step = defn.steps[next_index].name
            self._instance_step_index[instance_id] = next_index
        else:
            # No more steps — mark completed
            instance.state = "completed"
            instance.current_step = None

        instance.updated_at = _utcnow()
        self._instances[instance_id] = instance
        self._update_instance(instance)
        return instance

    def get_instance(self, instance_id: str) -> ProcessInstance | None:
        """Fetch instance by ID (in-memory cache, falls back to storage)."""
        if instance_id in self._instances:
            return self._instances[instance_id]

        row = self._storage.fetch_one(
            "SELECT * FROM process_instances WHERE id = ?",
            (instance_id,),
        )
        if row is None:
            return None

        return self._row_to_instance(row)

    # ── Persistence helpers ──────────────────────────────────────────────

    def _persist_instance(self, inst: ProcessInstance) -> None:
        self._storage.execute(
            "INSERT INTO process_instances "
            "(id, session_id, process_name, state, current_step, logs_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inst.id,
                inst.session_id,
                inst.process_name,
                inst.state,
                inst.current_step,
                json.dumps(inst.logs),
                inst.created_at.isoformat(),
                inst.updated_at.isoformat(),
            ),
        )

    def _update_instance(self, inst: ProcessInstance) -> None:
        self._storage.execute(
            "UPDATE process_instances "
            "SET state = ?, current_step = ?, logs_json = ?, updated_at = ? "
            "WHERE id = ?",
            (
                inst.state,
                inst.current_step,
                json.dumps(inst.logs),
                inst.updated_at.isoformat(),
                inst.id,
            ),
        )

    def _row_to_instance(self, row: dict) -> ProcessInstance:
        logs = json.loads(row.get("logs_json", "[]"))
        created = row.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        updated = row.get("updated_at")
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        return ProcessInstance(
            id=row["id"],
            session_id=row["session_id"],
            process_name=row["process_name"],
            state=row["state"],
            current_step=row.get("current_step"),
            logs=logs,
            created_at=created,
            updated_at=updated,
        )
