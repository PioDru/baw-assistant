"""Agent 3 – Agent Analityk: agregaty i trendy dla kierowników i właścicieli procesów.

KPI liczy kod (pandas, moduł analytics); agent zamienia je na fakty, wnioski i rekomendacje.
Rekomendacje są regułami na wynikach (bez nowych liczb), a dane do wykresów trafiają do UI.
"""
from __future__ import annotations

import time

from .. import analytics
from ..rbac import get_user
from ..state import AssistantState
from .common import ACTION_REBALANCE, AGENT_ANALYST, Findings, days_dec, elapsed_ms

RECOMMENDATIONS_BY_BOTTLENECK = {
    "Weryfikacja dokumentów": "Dodać walidację kompletności załączników już przy rejestracji sprawy.",
    "Akceptacja kierownika": "Wprowadzić przypomnienia dla kierowników o oczekujących akceptacjach.",
    "Sprzęt": "Zamawiać sprzęt już na etapie przygotowania umowy.",
    "Opinia techniczna": "Uzgodnić z działem technicznym termin odpowiedzi i monitorować jego dotrzymanie.",
}


def team(f: Findings, user_id: str) -> None:
    t = analytics.team_overview(get_user(user_id))
    f.fact(f"Aktywne sprawy ({t['team_name']}): {t['active']}; po terminie SLA: {t['overdue']}; "
           f"z terminem w ciągu 2 dni: {t['at_risk']}.", "instancje (agregat)")
    for p in t["per_assignee"][:6]:
        f.fact(f"{p['name']} – aktywne sprawy: {p['total']}, po terminie: {p['overdue']}, zagrożone: {p['risk']}.",
               "instancje (agregat)")
    if t["overdue_waiting"]:
        f.fact("Sprawy po terminie czekają na: " + ", ".join(f"{k} ({v})" for k, v in t["overdue_waiting"].items())
               + ".", "zadania")
    if t["least_loaded"]:
        f.fact(f"Najmniej aktywnych spraw: {t['least_loaded']['name']} ({t['least_loaded']['total']}).",
               "instancje (agregat)")
    f.data["team_breakdown"] = t["per_assignee"]
    top = t["per_assignee"][0] if t["per_assignee"] else None
    if top and top["overdue"]:
        f.lead(f"Najwięcej spraw po terminie ma {top['name']}.")
        if t["least_loaded"] and t["least_loaded"]["name"] != top["name"]:
            f.actions.append(ACTION_REBALANCE)
        if t["overdue_waiting"]:
            f.recommendations.append(f"Ustalić z jednostką „{next(iter(t['overdue_waiting']))}” termin obsługi "
                                     "zaległych zadań.")
    else:
        f.lead("Zespół nie ma spraw po terminie SLA.")


def process(f: Findings, process_id: str, days: int) -> None:
    k = analytics.process_kpis(process_id, days)
    f.fact(f"Proces „{k['process_name']}”, ostatnie {days} dni: zakończone instancje {k['completed']}, mediana czasu "
           f"realizacji {days_dec(k['median_cycle_days'])}, zrealizowane w SLA ({k['sla_days']} dni): {k['sla_pct']}%.",
           "historia instancji (agregat)")
    if k["sla_pct_recent"] is not None and k["sla_pct_previous"] is not None:
        f.fact(f"Odsetek spraw w SLA: ostatnie 4 tygodnie {k['sla_pct_recent']}%, poprzednie 4 tygodnie "
               f"{k['sla_pct_previous']}%.", "historia instancji (agregat)")
    f.fact(f"Aktywne instancje procesu: {k['active']}, w tym po terminie SLA: {k['active_overdue']}.", "instancje")
    if not k["steps"].empty:
        f.fact("Średni czas w krokach: " + "; ".join(f"{r.step} {days_dec(r.days)}" for r in k["steps"].itertuples())
               + ".", "historia zdarzeń (agregat)")
    if k["missing_doc_by_channel"]:
        f.fact("Odsetek spraw z brakującym dokumentem według kanału: "
               + ", ".join(f"{ch} {v}%" for ch, v in sorted(k["missing_doc_by_channel"].items(), key=lambda x: -x[1]))
               + ".", "historia zdarzeń (agregat)")
    f.data.update({"weekly": k["weekly"].assign(week_end=k["weekly"]["week_end"].dt.strftime("%Y-%m-%d"))
                   .to_dict("records"), "steps": k["steps"].to_dict("records"), "process_name": k["process_name"]})

    b = k["bottleneck"]
    if b:
        f.lead(f"Wąskim gardłem procesu „{k['process_name']}” jest krok „{b['step']}” (średnio {days_dec(b['days'])}).")
        if b["step"] in RECOMMENDATIONS_BY_BOTTLENECK:
            f.recommendations.append(RECOMMENDATIONS_BY_BOTTLENECK[b["step"]])
    if k["missing_doc_by_channel"]:
        worst = max(k["missing_doc_by_channel"], key=k["missing_doc_by_channel"].get)
        f.recommendations.append(f"Wzmocnić kontrolę kompletności dokumentów w kanale „{worst}”.")
        f.recommendations.append("Uruchomić automatyczne przypomnienie do klienta, gdy brakuje dokumentu.")
    if (k["sla_pct_recent"] or 0) < (k["sla_pct_previous"] or 0):
        f.recommendations.append("Przeanalizować przyczyny spadku odsetka spraw w SLA w ostatnich tygodniach.")


def analyst_agent_node(state: AssistantState) -> AssistantState:
    started = time.perf_counter()
    f = Findings(AGENT_ANALYST)
    if state["intent"] == "team_overview":
        team(f, state["user_id"])
        what = "agregaty zespołu"
    else:
        days = int((state.get("params") or {}).get("days", 84))
        process(f, state["process_id"], days)
        what = f"KPI procesu {state['process_id']} za {days} dni"
    return f.update(f"Policzył {what} w pandas (faktów: {len(f.facts)}, rekomendacji: {len(f.recommendations)}).",
                    elapsed_ms(started))
