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
    "team_overview": {"kierownik", "admin"},          # Agent Analityk – PoC v2
    "process_analytics": {"wlasciciel", "kierownik", "admin"},  # Agent Analityk – PoC v2
    "platform_status": {"admin"},                     # Agent Monitor – PoC v2
}

# Intencje obsługiwane w PoC v1 przez Agenta Instancji.
V1_INTENTS = {"instance_diagnosis", "instance_details", "my_work"}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    team_id: str | None

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


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
    if user.role == "admin":
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
