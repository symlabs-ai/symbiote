"""A fictitious personal API for the assistant: tasks, calendar, contacts,
notes, expenses and a simulated mailbox.

It is a normal FastAPI app mounted at ``/pa``. The kernel never imports it:
the demo discovers its tools from ``/pa/openapi.json`` (``operation_id`` is the
tool id, ``tags`` drive tool visibility, ``x-risk-level`` marks what needs a
human approval) and calls it over HTTP like any other service.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, due TEXT,
  priority TEXT, done INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, start TEXT,
  end_time TEXT, location TEXT);
CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, name TEXT, email TEXT,
  phone TEXT, relation TEXT, birthday TEXT);
CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT, body TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS expenses (id TEXT PRIMARY KEY, description TEXT, amount REAL,
  category TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, recipient TEXT, subject TEXT,
  body TEXT, sent_at TEXT);
"""


def _id() -> str:
    return uuid.uuid4().hex[:8]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        if not self.all("SELECT id FROM tasks LIMIT 1"):
            self.seed()

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def run(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def reset(self) -> None:
        for table in ("tasks", "events", "contacts", "notes", "expenses", "outbox"):
            self.run(f"DELETE FROM {table}")
        self.seed()

    def seed(self) -> None:
        today = date.today()

        def d(days: int) -> str:
            return (today + timedelta(days=days)).isoformat()

        now = datetime.now().isoformat(timespec="seconds")
        tasks = [
            ("Renovar o seguro do carro", d(3), "alta"),
            ("Marcar dentista para a Lia", d(7), "media"),
            ("Pagar a conta de luz", d(1), "alta"),
            ("Comprar presente da Marina", d(10), "media"),
            ("Organizar fotos da viagem", "", "baixa"),
        ]
        for title, due, prio in tasks:
            self.run("INSERT INTO tasks VALUES (?,?,?,?,0,?)", (_id(), title, due, prio, now))
        events = [
            ("Reunião de planejamento", f"{d(1)}T10:00", f"{d(1)}T11:00", "Escritório"),
            ("Aula de natação da Lia", f"{d(2)}T18:30", f"{d(2)}T19:30", "Clube Atlântico"),
            ("Jantar com Marina e Paulo", f"{d(4)}T20:00", f"{d(4)}T22:30", "Restaurante Oliva"),
            ("Consulta médica", f"{d(9)}T09:00", f"{d(9)}T09:45", "Clínica Vida"),
        ]
        for title, start, end, loc in events:
            self.run("INSERT INTO events VALUES (?,?,?,?,?)", (_id(), title, start, end, loc))
        contacts = [
            ("Marina Costa", "marina@example.com", "+55 11 99876-1234", "irmã", "1990-" + d(10)[5:]),
            ("Paulo Mendes", "paulo@example.com", "+55 11 98765-4321", "cunhado", "1987-03-14"),
            ("Dra. Helena Rocha", "helena.rocha@clinicavida.example", "+55 11 3333-2020", "médica", ""),
            ("Ana Souza", "ana.souza@example.com", "+55 21 99111-2233", "colega de trabalho", "1993-08-02"),
        ]
        for name, email, phone, rel, bday in contacts:
            self.run("INSERT INTO contacts VALUES (?,?,?,?,?,?)", (_id(), name, email, phone, rel, bday))
        notes = [
            ("Ideias de presente para a Marina", "Livro de fotografia, aula de cerâmica, fone novo."),
            ("Senha do Wi-Fi da casa de praia", "Rede: Praia-5G (a senha fica no cofre de senhas, não aqui)."),
            ("Receita do pão de fermentação natural", "500g farinha, 350g água, 100g levain, 10g sal. 24h de fermentação."),
        ]
        for title, body in notes:
            self.run("INSERT INTO notes VALUES (?,?,?,?)", (_id(), title, body, now))
        month = today.strftime("%Y-%m")
        expenses = [
            ("Supermercado", 612.40, "mercado", f"{month}-02"),
            ("Gasolina", 280.00, "transporte", f"{month}-03"),
            ("Streaming", 55.90, "lazer", f"{month}-05"),
            ("Farmácia", 89.30, "saude", f"{month}-06"),
            ("Restaurante", 214.00, "lazer", f"{month}-08"),
            ("Supermercado", 398.75, "mercado", f"{month}-12"),
        ]
        for desc, amount, cat, day in expenses:
            self.run("INSERT INTO expenses VALUES (?,?,?,?,?)", (_id(), desc, amount, cat, day))


class TaskIn(BaseModel):
    title: str = Field(description="O que precisa ser feito")
    due: str | None = Field(default=None, description="Prazo AAAA-MM-DD (opcional)")
    priority: str | None = Field(default="media", description="baixa, media ou alta")


class EventIn(BaseModel):
    title: str
    start: str = Field(description="Início, AAAA-MM-DDTHH:MM")
    end: str | None = Field(default=None, description="Fim, AAAA-MM-DDTHH:MM (opcional)")
    location: str | None = None


class NoteIn(BaseModel):
    title: str
    body: str


class ExpenseIn(BaseModel):
    description: str
    amount: float = Field(description="Valor em reais")
    category: str = Field(description="mercado, transporte, lazer, saude, casa ou outros")
    day: str | None = Field(default=None, description="Data AAAA-MM-DD (padrão: hoje)")


class EmailIn(BaseModel):
    to: str = Field(description="Endereço de e-mail do destinatário")
    subject: str
    body: str


def create_personal_api(db_path: Path) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(
        title="Sol — API pessoal (fictícia)",
        description="Dados de exemplo do demo do Symbiote. Nada sai da máquina.",
        version="1.0",
    )
    app.state.store = store
    high = {"x-risk-level": "high"}
    low = {"x-risk-level": "low"}

    # ── tarefas ────────────────────────────────────────────────────────
    @app.get("/tasks", operation_id="list_tasks", tags=["tarefas"],
             summary="Listar tarefas", description="Lista as tarefas. status: abertas, feitas ou todas.")
    def list_tasks(status: str = Query("abertas", description="abertas, feitas ou todas")) -> list[dict]:
        where = {"abertas": "WHERE done = 0", "feitas": "WHERE done = 1"}.get(status, "")
        return store.all(f"SELECT id, title, due, priority, done FROM tasks {where} ORDER BY due = '', due")

    @app.post("/tasks", operation_id="create_task", tags=["tarefas"], summary="Criar tarefa")
    def create_task(task: TaskIn) -> dict:
        tid = _id()
        store.run("INSERT INTO tasks VALUES (?,?,?,?,0,?)",
                  (tid, task.title, task.due or "", task.priority or "media",
                   datetime.now().isoformat(timespec="seconds")))
        return {"id": tid, "title": task.title, "due": task.due, "priority": task.priority}

    @app.post("/tasks/{task_id}/complete", operation_id="complete_task", tags=["tarefas"],
              summary="Concluir tarefa", openapi_extra=low)
    def complete_task(task_id: str) -> dict:
        if not store.run("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,)):
            raise HTTPException(404, "tarefa não encontrada")
        return {"id": task_id, "done": True}

    @app.delete("/tasks/{task_id}", operation_id="delete_task", tags=["tarefas"],
                summary="Apagar tarefa (irreversível)", openapi_extra=high)
    def delete_task(task_id: str) -> dict:
        if not store.run("DELETE FROM tasks WHERE id = ?", (task_id,)):
            raise HTTPException(404, "tarefa não encontrada")
        return {"id": task_id, "deleted": True}

    # ── agenda ─────────────────────────────────────────────────────────
    @app.get("/events", operation_id="list_events", tags=["agenda"], summary="Ver agenda",
             description="Compromissos entre duas datas (AAAA-MM-DD). Sem datas: próximos 14 dias.")
    def list_events(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        start = date_from or date.today().isoformat()
        end = date_to or (date.today() + timedelta(days=14)).isoformat()
        return store.all(
            "SELECT id, title, start, end_time AS end, location FROM events "
            "WHERE substr(start, 1, 10) BETWEEN ? AND ? ORDER BY start", (start, end))

    @app.post("/events", operation_id="create_event", tags=["agenda"], summary="Marcar compromisso")
    def create_event(event: EventIn) -> dict:
        eid = _id()
        store.run("INSERT INTO events VALUES (?,?,?,?,?)",
                  (eid, event.title, event.start, event.end or "", event.location or ""))
        return {"id": eid, **event.model_dump()}

    # ── contatos ───────────────────────────────────────────────────────
    @app.get("/contacts", operation_id="search_contacts", tags=["contatos"],
             summary="Buscar contatos", description="Busca por nome ou relação (ex.: irmã). Vazio lista todos.")
    def search_contacts(q: str | None = None) -> list[dict]:
        like = f"%{q or ''}%"
        return store.all("SELECT * FROM contacts WHERE name LIKE ? OR relation LIKE ? ORDER BY name",
                         (like, like))

    # ── notas ──────────────────────────────────────────────────────────
    @app.get("/notes", operation_id="search_notes", tags=["notas"], summary="Buscar notas")
    def search_notes(q: str | None = None) -> list[dict]:
        like = f"%{q or ''}%"
        return store.all("SELECT * FROM notes WHERE title LIKE ? OR body LIKE ? ORDER BY created_at DESC",
                         (like, like))

    @app.post("/notes", operation_id="create_note", tags=["notas"], summary="Criar nota")
    def create_note(note: NoteIn) -> dict:
        nid = _id()
        store.run("INSERT INTO notes VALUES (?,?,?,?)",
                  (nid, note.title, note.body, datetime.now().isoformat(timespec="seconds")))
        return {"id": nid, **note.model_dump()}

    # ── finanças ───────────────────────────────────────────────────────
    @app.get("/expenses", operation_id="list_expenses", tags=["financas"], summary="Listar despesas",
             description="Despesas de um mês (AAAA-MM). Padrão: mês atual.")
    def list_expenses(month: str | None = None) -> list[dict]:
        m = month or date.today().strftime("%Y-%m")
        return store.all("SELECT * FROM expenses WHERE substr(day, 1, 7) = ? ORDER BY day", (m,))

    @app.get("/expenses/summary", operation_id="expense_summary", tags=["financas"],
             summary="Resumo de gastos por categoria")
    def expense_summary(month: str | None = None) -> dict:
        m = month or date.today().strftime("%Y-%m")
        rows = store.all("SELECT category, ROUND(SUM(amount), 2) AS total FROM expenses "
                         "WHERE substr(day, 1, 7) = ? GROUP BY category ORDER BY total DESC", (m,))
        return {"month": m, "by_category": rows, "total": round(sum(r["total"] for r in rows), 2)}

    @app.post("/expenses", operation_id="add_expense", tags=["financas"], summary="Registrar despesa")
    def add_expense(expense: ExpenseIn) -> dict:
        xid = _id()
        day = expense.day or date.today().isoformat()
        store.run("INSERT INTO expenses VALUES (?,?,?,?,?)",
                  (xid, expense.description, expense.amount, expense.category, day))
        return {"id": xid, **expense.model_dump(), "day": day}

    # ── e-mail (simulado) ──────────────────────────────────────────────
    @app.post("/email/send", operation_id="send_email", tags=["email"],
              summary="Enviar e-mail (simulado — vai para a caixa de saída)", openapi_extra=high)
    def send_email(email: EmailIn) -> dict:
        mid = _id()
        store.run("INSERT INTO outbox VALUES (?,?,?,?,?)",
                  (mid, email.to, email.subject, email.body, datetime.now().isoformat(timespec="seconds")))
        return {"id": mid, "status": "enviado (simulado)", "to": email.to}

    # Not part of the tool surface: the page reads it to show the outbox.
    @app.get("/_outbox", include_in_schema=False)
    def outbox() -> list[dict]:
        return store.all("SELECT * FROM outbox ORDER BY sent_at DESC")

    @app.get("/_data", include_in_schema=False)
    def data() -> dict:
        return {
            "tasks": store.all("SELECT * FROM tasks ORDER BY done, due = '', due"),
            "events": store.all("SELECT * FROM events ORDER BY start"),
            "expenses": store.all("SELECT * FROM expenses ORDER BY day DESC"),
            "outbox": store.all("SELECT * FROM outbox ORDER BY sent_at DESC"),
        }

    return app
