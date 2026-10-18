"""Obliczenia KPI dla Agenta Analityka (pandas). Wszystkie liczby powstają tutaj, nie w LLM."""
from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd

from . import tools
from .db import query
from .rbac import User, managed_team


def _active_frame(team_id: str | None) -> pd.DataFrame:
    sql = ("SELECT i.id, i.process_id, i.current_step, i.sla_due, i.assignee_id, u.name AS assignee, i.team_id"
           " FROM instances i LEFT JOIN users u ON u.id = i.assignee_id WHERE i.status != 'Zakończona'")
    params: tuple = ()
    if team_id:
        sql += " AND i.team_id = ?"
        params = (team_id,)
    df = pd.DataFrame(query(sql, params))
    if df.empty:
        return df
    df["days_to_sla"] = df["sla_due"].map(lambda d: tools.days_between(tools.now(), d))
    df["bucket"] = pd.cut(df["days_to_sla"], bins=[-10_000, -1, 2, 10_000],
                          labels=["po terminie", "zagrożone", "w terminie"])
    return df


def team_overview(user: User) -> dict:
    """Stan aktywnych spraw zespołu kierownika (administrator: wszystkie zespoły)."""
    team = managed_team(user) if user.role == "kierownik" else None
    df = _active_frame(team["id"] if team else None)
    result: dict = {"team_name": team["name"] if team else "wszystkie zespoły", "active": len(df)}
    if df.empty:
        return {**result, "overdue": 0, "at_risk": 0, "per_assignee": [], "overdue_waiting": {}, "least_loaded": None}
    per = (df.pivot_table(index="assignee", columns="bucket", values="id", aggfunc="count", observed=False)
           .fillna(0).astype(int).reindex(columns=["w terminie", "zagrożone", "po terminie"], fill_value=0))
    per["razem"] = per.sum(axis=1)
    per = per.sort_values(["po terminie", "razem"], ascending=False)
    overdue_ids = df.loc[df["bucket"] == "po terminie", "id"].tolist()
    waiting: dict[str, int] = {}
    for iid in overdue_ids:
        open_tasks = [t for t in tools.get_tasks(iid) if t["status"] in ("Otwarte", "Wstrzymane")]
        external = [t for t in open_tasks if t["assignee_type"] != "user"]
        label = external[-1]["assignee_label"] if external else "pracownik prowadzący"
        waiting[label] = waiting.get(label, 0) + 1
    least = per.sort_values("razem").iloc[0]
    return {
        **result,
        "overdue": int((df["bucket"] == "po terminie").sum()),
        "at_risk": int((df["bucket"] == "zagrożone").sum()),
        "per_assignee": [{"name": name, "ok": int(r["w terminie"]), "risk": int(r["zagrożone"]),
                          "overdue": int(r["po terminie"]), "total": int(r["razem"])} for name, r in per.iterrows()],
        "overdue_waiting": dict(sorted(waiting.items(), key=lambda kv: -kv[1])),
        "least_loaded": {"name": least.name, "total": int(least["razem"])},
        "overdue_instances": overdue_ids,
    }


def _step_durations(process_id: str, instance_ids: list[str]) -> pd.DataFrame:
    if not instance_ids:
        return pd.DataFrame(columns=["step", "days"])
    marks = ",".join("?" for _ in instance_ids)
    ev = pd.DataFrame(query(f"SELECT instance_id, ts, type, step FROM events WHERE instance_id IN ({marks})"
                            " AND type IN ('STEP_ENTERED','COMPLETED') ORDER BY instance_id, ts, id",
                            tuple(instance_ids)))
    ev["ts"] = pd.to_datetime(ev["ts"])
    ev["next_ts"] = ev.groupby("instance_id")["ts"].shift(-1)
    steps = ev[ev["type"] == "STEP_ENTERED"].dropna(subset=["next_ts"])
    steps = steps.assign(days=(steps["next_ts"] - steps["ts"]).dt.total_seconds() / 86400)
    order = json.loads(query("SELECT steps_json FROM processes WHERE id = ?", (process_id,))[0]["steps_json"])
    durations = steps.groupby("step")["days"].mean().reindex(order).dropna().round(1)
    return pd.DataFrame({"step": durations.index, "days": durations.values})


def process_kpis(process_id: str, days: int = 84) -> dict:
    now = tools.now()
    start = now - timedelta(days=days)
    proc = query("SELECT id, name, sla_days FROM processes WHERE id = ?", (process_id,))[0]
    df = pd.DataFrame(query("SELECT id, channel, created_at, completed_at, sla_due FROM instances"
                            " WHERE process_id = ? AND status = 'Zakończona'", (process_id,)))
    for col in ("created_at", "completed_at", "sla_due"):
        df[col] = pd.to_datetime(df[col])
    df = df[df["completed_at"] >= start]
    df["cycle_days"] = (df["completed_at"] - df["created_at"]).dt.total_seconds() / 86400
    df["in_sla"] = df["completed_at"] <= df["sla_due"]

    recent = df[df["completed_at"] >= now - timedelta(days=28)]
    previous = df[(df["completed_at"] < now - timedelta(days=28)) & (df["completed_at"] >= now - timedelta(days=56))]
    weekly = (df.set_index("completed_at").resample("W-SUN").size().rename("completed").reset_index()
              .rename(columns={"completed_at": "week_end"}))
    steps = _step_durations(process_id, df["id"].tolist())
    bottleneck = steps.sort_values("days", ascending=False).iloc[0].to_dict() if not steps.empty else None

    channel_share = None
    if process_id == "REK" and not df.empty:
        requested = {r["instance_id"] for r in query(
            "SELECT DISTINCT instance_id FROM events WHERE type = 'DOCUMENT_REQUESTED'")}
        df["missing_doc"] = df["id"].isin(requested)
        channel_share = (df.groupby("channel")["missing_doc"].mean().mul(100).round(0).astype(int).to_dict())

    active = query("SELECT COUNT(*) AS n, SUM(CASE WHEN sla_due < ? THEN 1 ELSE 0 END) AS overdue FROM instances"
                   " WHERE process_id = ? AND status != 'Zakończona'", (now.isoformat(timespec="minutes"), process_id))[0]
    pct = lambda s: int(round(s.mean() * 100)) if len(s) else None  # noqa: E731
    return {
        "process_id": process_id, "process_name": proc["name"], "sla_days": proc["sla_days"], "days": days,
        "completed": len(df),
        "median_cycle_days": round(float(df["cycle_days"].median()), 1) if len(df) else None,
        "sla_pct": pct(df["in_sla"]), "sla_pct_recent": pct(recent["in_sla"]), "sla_pct_previous": pct(previous["in_sla"]),
        "weekly": weekly, "steps": steps, "bottleneck": bottleneck, "missing_doc_by_channel": channel_share,
        "active": int(active["n"] or 0), "active_overdue": int(active["overdue"] or 0),
    }
