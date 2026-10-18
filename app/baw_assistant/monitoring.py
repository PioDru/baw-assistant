"""Wykrywanie alertów dla Agenta Monitora: logi, kolejki, jakość danych, SLA.

Reguły i progi są w kodzie (progi edytowalne w ustawieniach), a diagnoza korzysta z instrukcji
w bazie wiedzy (Qdrant). Alerty są deduplikowane po „odcisku” (kategoria + komponent + okno czasu),
więc kolejne skany harmonogramu nie tworzą duplikatów.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import kb, tools
from .db import execute, get_setting, query, query_one, now_iso

CHANNELS_BY_PRIORITY = {"Krytyczny": ["Zadanie BAW", "E-mail", "Teams"], "Ostrzeżenie": ["E-mail"],
                        "Informacja": []}
PRIORITY_ORDER = {"Krytyczny": 0, "Ostrzeżenie": 1, "Informacja": 2}

COMPONENT_LABELS = {"DocumentService": "Brak połączenia z repozytorium dokumentów"}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _logs_since(hours: int, level: str | None = None) -> list[dict]:
    since = (tools.now() - timedelta(hours=hours)).isoformat(timespec="minutes")
    until = tools.now().isoformat(timespec="minutes")
    sql = "SELECT * FROM logs WHERE ts >= ? AND ts <= ?"
    params: list = [since, until]
    if level:
        sql += " AND level = ?"
        params.append(level)
    return query(sql + " ORDER BY ts, id", tuple(params))


def _diagnose(text: str) -> tuple[str | None, str | None, str | None]:
    """(diagnoza, sugerowana akcja, tytuł instrukcji) z bazy wiedzy."""
    hits = kb.search(text, kind="runbook", role="admin", limit=1)
    if not hits:
        return None, None, None
    hit = hits[0]
    causes = kb.doc_section(hit["doc"], "Najczęstsze przyczyny")
    actions = kb.doc_section(hit["doc"], "Działania")
    history = kb.doc_section(hit["doc"], "Historia")
    diagnosis = kb.short(causes, 200) if causes else None
    if history:
        diagnosis = f"{diagnosis or ''} {kb.short(history, 120)}".strip()
    return diagnosis, kb.short(actions or "", 260) or None, hit["title"]


def _alert(fingerprint: str, event_ts: str, priority: str, category: str, component: str | None,
           description: str, evidence: str = "", diagnose_text: str | None = None,
           channels: list[str] | None = None, audience: str = "admin") -> dict:
    diagnosis = action = source = None
    if diagnose_text:
        diagnosis, action, source = _diagnose(diagnose_text)
    return {"fingerprint": fingerprint, "event_ts": event_ts, "priority": priority, "category": category,
            "component": component, "description": description, "evidence": evidence,
            "diagnosis": diagnosis, "suggested_action": action, "kb_source": source,
            "channels": channels if channels is not None else CHANNELS_BY_PRIORITY[priority],
            "audience": audience}


def detect_alerts(hours: int = 24) -> list[dict]:
    alerts: list[dict] = []
    threshold = int(get_setting("integration_errors_15min"))

    # 1) Awaria integracji: seria błędów tego samego typu w oknie 15 minut.
    groups: dict[tuple[str, str], list[dict]] = {}
    for lg in _logs_since(hours, "ERROR"):
        signature = re.sub(r"\d+", "#", lg["message"].split(":")[0])
        groups.setdefault((lg["component"], signature), []).append(lg)
    for (component, signature), rows in groups.items():
        best: list[dict] = []
        for i, first in enumerate(rows):
            window = [r for r in rows[i:] if _ts(r["ts"]) - _ts(first["ts"]) <= timedelta(minutes=15)]
            best = window if len(window) > len(best) else best
        if len(best) >= threshold:
            start, end = best[0]["ts"], best[-1]["ts"]
            suspended = query_one("SELECT COUNT(DISTINCT instance_id) AS n FROM events WHERE type = 'SUSPENDED'"
                                  " AND ts >= ? AND ts <= ?", (start, (_ts(end) + timedelta(minutes=30))
                                                                .isoformat(timespec="minutes")))["n"]
            label = next((v for k, v in COMPONENT_LABELS.items() if k in signature), "Seria błędów integracji")
            alerts.append(_alert(
                f"integracja|{component}|{signature}|{start}", start, "Krytyczny", "Awaria integracji", component,
                f"{label}: {len(best)} błędy w 15 min od {tools.fmt_datetime(start)} ({component}); "
                f"wstrzymane instancje: {suspended}.",
                evidence="\n".join(f"{r['ts']} {r['level']} [{r['component']}] {r['message']}" for r in best),
                diagnose_text=best[0]["message"]))

    warns = _logs_since(hours, "WARN")
    # 2) Opóźnienie kolejki zdarzeń.
    delay_limit = int(get_setting("queue_delay_s"))
    for lg in warns:
        m = re.search(r"Opóźnienie kolejki zdarzeń: (\d+) s", lg["message"])
        if m and int(m.group(1)) >= delay_limit:
            delay = int(m.group(1))
            alerts.append(_alert(
                f"kolejka|{lg['component']}|{lg['ts']}", lg["ts"],
                "Krytyczny" if delay >= 2 * delay_limit else "Ostrzeżenie", "Dostępność", lg["component"],
                f"Opóźnienie kolejki zdarzeń {delay} s (próg {delay_limit} s) o {tools.fmt_datetime(lg['ts'])}.",
                evidence=f"{lg['ts']} WARN [{lg['component']}] {lg['message']}", diagnose_text=lg["message"]))

    # 3) Błędy walidacji danych biznesowych.
    data_errors = [lg for lg in warns if lg["message"].startswith("Walidacja danych")]
    for lg in data_errors:
        alerts.append(_alert(
            f"dane|{lg['instance_id']}|{lg['message']}", lg["ts"], "Ostrzeżenie", "Błąd danych", lg["component"],
            f"{lg['message']} w sprawie {lg['instance_id']} ({tools.fmt_datetime(lg['ts'])}).",
            evidence=f"{lg['ts']} WARN [{lg['component']}] {lg['instance_id']} {lg['message']}",
            diagnose_text=lg["message"], channels=["Zadanie BAW"], audience="owner"))

    # 4) Wolne odpowiedzi usług (informacyjnie).
    slow_limit = int(get_setting("slow_response_warnings_24h"))
    slow: dict[str, int] = {}
    for lg in warns:
        if lg["message"].startswith("Czas odpowiedzi"):
            slow[lg["component"]] = slow.get(lg["component"], 0) + 1
    for component, count in slow.items():
        if count >= slow_limit:
            alerts.append(_alert(
                f"wydajnosc|{component}|{tools.now().date()}", tools.now().isoformat(timespec="minutes"),
                "Informacja", "Wydajność", component,
                f"Ostrzeżenia o czasie odpowiedzi powyżej 2 s w ciągu {hours} h: {count} ({component})."))

    # 5) Sprawy po terminie SLA (per zespół) – do kierowników.
    for row in query("SELECT t.name AS team, u.name AS manager, COUNT(*) AS n FROM instances i"
                     " JOIN teams t ON t.id = i.team_id LEFT JOIN users u ON u.id = t.manager_id"
                     " WHERE i.status != 'Zakończona' AND i.sla_due < ? GROUP BY t.name, u.name",
                     (tools.now().isoformat(timespec="minutes"),)):
        alerts.append(_alert(
            f"sla|{row['team']}|{tools.now().date()}|{row['n']}", tools.now().isoformat(timespec="minutes"),
            "Ostrzeżenie", "SLA", None, f"Sprawy po terminie SLA w zespole „{row['team']}”: {row['n']}.",
            audience=f"manager:{row['manager']}"))

    # 6) Wzrost liczby spraw wchodzących do kroku (tydzień do tygodnia).
    growth_limit = int(get_setting("step_growth_pct"))
    now = tools.now()
    week, prev = (now - timedelta(days=7)).isoformat(), (now - timedelta(days=14)).isoformat()
    for row in query("SELECT p.name AS process, e.step, SUM(CASE WHEN e.ts >= ? THEN 1 ELSE 0 END) AS recent,"
                     " SUM(CASE WHEN e.ts < ? AND e.ts >= ? THEN 1 ELSE 0 END) AS previous FROM events e"
                     " JOIN instances i ON i.id = e.instance_id JOIN processes p ON p.id = i.process_id"
                     " WHERE e.type = 'STEP_ENTERED' AND e.ts >= ? GROUP BY p.name, e.step",
                     (week, week, prev, prev)):
        if row["previous"] and row["recent"] >= 10:
            growth = round((row["recent"] - row["previous"]) / row["previous"] * 100)
            if growth >= growth_limit:
                alerts.append(_alert(
                    f"trend|{row['process']}|{row['step']}|{now.date()}", now.isoformat(timespec="minutes"),
                    "Informacja", "Trend", None,
                    f"Wzrost liczby spraw w kroku „{row['step']}” ({row['process']}) o {growth}% tydzień do "
                    f"tygodnia ({row['previous']} → {row['recent']})."))

    return sorted(alerts, key=lambda a: (PRIORITY_ORDER[a["priority"]], a["event_ts"]))


def persist_alerts(alerts: list[dict]) -> list[dict]:
    """Zapisuje nowe alerty; zwraca tylko te, których wcześniej nie było (do powiadomień)."""
    new = []
    for a in alerts:
        if query_one("SELECT id FROM alerts WHERE fingerprint = ?", (a["fingerprint"],)):
            continue
        a["id"] = execute(
            "INSERT INTO alerts (created_at, fingerprint, event_ts, priority, category, component, description,"
            " diagnosis, suggested_action, evidence, channels_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), a["fingerprint"], a["event_ts"], a["priority"], a["category"], a["component"],
             a["description"], a["diagnosis"], a["suggested_action"], a["evidence"],
             json.dumps(a["channels"], ensure_ascii=False)))
        new.append(a)
    return new


def instance_technical_check(instance_id: str, days: int = 7) -> dict:
    logs = tools.get_instance_logs(instance_id, days)
    errors = [lg for lg in logs if lg["level"] == "ERROR"]
    return {"logs": logs, "errors": errors, "last": (errors[-1] if errors else logs[-1]) if logs else None}
