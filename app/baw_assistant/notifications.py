"""Powiadomienia: zadanie w BAW, e-mail, Teams. W PoC wysyłka jest symulowana – wpis w tabeli notifications.

Docelowo: zadanie BAW przez REST API / UCA, e-mail przez SMTP, Teams przez webhook.
"""
from __future__ import annotations

from .db import execute, now_iso, query

CHANNELS = ("Zadanie BAW", "E-mail", "Teams")

ADMIN_RECIPIENTS = {"Zadanie BAW": "Zespół platformy", "E-mail": "Dyżurny administrator",
                    "Teams": "#baw-incydenty"}


def send(channel: str, recipient: str, subject: str, body: str, origin: str) -> int:
    return execute("INSERT INTO notifications (ts, channel, recipient, subject, body, origin) VALUES (?,?,?,?,?,?)",
                   (now_iso(), channel, recipient, subject, body, origin))


def recipient_for(audience: str, channel: str) -> str:
    if audience == "owner":
        owner = query("SELECT u.name FROM processes p JOIN users u ON u.id = p.owner_id LIMIT 1")
        return f"{owner[0]['name']} (właściciel procesu)" if owner else "Właściciel procesu"
    if audience.startswith("manager:"):
        return f"{audience.split(':', 1)[1]} (kierownik zespołu)"
    if audience.startswith("user:"):
        return audience.split(":", 1)[1]
    return ADMIN_RECIPIENTS.get(channel, "Administrator platformy")


def notify_alert(alert: dict) -> list[int]:
    body = alert["description"]
    if alert.get("diagnosis"):
        body += f"\nDiagnoza: {alert['diagnosis']}"
    if alert.get("suggested_action"):
        body += f"\nSugerowane działanie: {alert['suggested_action']}"
    ids = [send(ch, recipient_for(alert.get("audience", "admin"), ch), f"[{alert['priority']}] {alert['category']}",
                body, f"alert #{alert.get('id', '?')}") for ch in alert["channels"]]
    if ids and alert.get("id"):
        execute("UPDATE alerts SET status = 'Powiadomiono' WHERE id = ?", (alert["id"],))
    return ids


def recent(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
