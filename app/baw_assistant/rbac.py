"""Uprawnienia: kto widzi które instancje i jakie intencje są dostępne dla roli.

Kontrola dostępu jest zawsze wykonywana w kodzie, nigdy przez LLM.
"""
from __future__ import annotations

from dataclasses import dataclass

from .db import query, query_one

ROLE_LABELS = {
    "pracownik": "Pracownik",
    "kierownik": "Kierownik zespołu",
    "wlasciciel": "Właściciel procesu",
    "admin": "Administrator",
}

# Intencje i role, które mogą z nich korzystać.
INTENT_ROLES = {
    "instance_diagnosis": {"pracownik", "kierownik", "wlasciciel", "admin"},
    "instance_details": {"pracownik", "kierownik", "wlasciciel", "admin"},
    "my_work": {"pracownik", "kierownik", "wlasciciel", "admin"},
    "team_overview": {"kierownik", "admin"},                    # Agent Analityk
    "process_analytics": {"wlasciciel", "kierownik", "admin"},  # Agent Analityk
    "platform_status": {"admin"},                               # Agent Monitor
}

# Procesy w zakresie użytkownika (dla Agenta Analityka).
def visible_process_ids(user: "User") -> list[str]:
    if user.role in ("admin", "system"):
        rows = query("SELECT id FROM processes ORDER BY id")
    elif user.role == "wlasciciel":
        rows = query("SELECT id FROM processes WHERE owner_id = ? ORDER BY id", (user.id,))
    elif user.role == "kierownik":
        rows = query("SELECT id FROM processes WHERE team_id IN (SELECT id FROM teams WHERE manager_id = ?)"
                     " ORDER BY id", (user.id,))
    else:
        rows = []
    return [r["id"] for r in rows]


def managed_team(user: "User") -> dict | None:
    return query_one("SELECT id, name FROM teams WHERE manager_id = ?", (user.id,))


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    team_id: str | None

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, "System")


def get_user(user_id: str) -> User:
    row = query_one("SELECT id, name, role, team_id FROM users WHERE id = ?", (user_id,))
    if not row:
        raise KeyError(f"Nieznany użytkownik: {user_id}")
    return User(**row)


def users_with_role(role: str) -> list[User]:
    return [User(**r) for r in query("SELECT id, name, role, team_id FROM users WHERE role = ? ORDER BY name",
                                     (role,))]


def _visibility_clause(user: User) -> tuple[str, tuple]:
    if user.role == "pracownik":
        return "i.assignee_id = ?", (user.id,)
    if user.role == "kierownik":
        return "i.team_id IN (SELECT id FROM teams WHERE manager_id = ?)", (user.id,)
    if user.role == "wlasciciel":
        return "i.process_id IN (SELECT id FROM processes WHERE owner_id = ?)", (user.id,)
    if user.role in ("admin", "system"):
        return "1 = 1", ()
    return "1 = 0", ()


def can_view_instance(user: User, instance_id: str) -> bool:
    clause, params = _visibility_clause(user)
    row = query_one(f"SELECT 1 AS ok FROM instances i WHERE i.id = ? AND {clause}", (instance_id, *params))
    return row is not None


def instance_exists(instance_id: str) -> bool:
    return query_one("SELECT 1 AS ok FROM instances WHERE id = ?", (instance_id,)) is not None


def visible_active_instances(user: User) -> list[dict]:
    clause, params = _visibility_clause(user)
    return query(
        "SELECT i.id, p.name AS process, i.current_step, i.sla_due, i.assignee_id"
        " FROM instances i JOIN processes p ON p.id = i.process_id"
        f" WHERE i.status != 'Zakończona' AND {clause} ORDER BY i.sla_due",
        params,
    )


def intent_allowed(user: User, intent: str) -> bool:
    return user.role in INTENT_ROLES.get(intent, set())
