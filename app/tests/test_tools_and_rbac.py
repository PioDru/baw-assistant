"""Testy narzędzi odczytu i uprawnień – liczby muszą zgadzać się z danymi z generatora."""
from __future__ import annotations

from baw_assistant import tools
from baw_assistant.rbac import can_view_instance, get_user, visible_active_instances


def test_golden_instance_facts():
    inst = tools.get_instance("REK-2026-0412")
    assert inst["current_step"] == "Weryfikacja dokumentów"
    assert inst["missing_docs"] == ["Protokół szkody"]
    assert tools.days_between(inst["last_change_at"]) == 4
    assert tools.days_between(tools.now(), inst["sla_due"]) == 3
    assert tools.get_instance_logs("REK-2026-0412") == []


def test_outage_logs_for_suspended_instance():
    logs = tools.get_instance_logs("REK-2026-0419")
    assert {lg["level"] for lg in logs} == {"ERROR", "WARN"}


def test_similar_cases_are_aggregates_only():
    stats = tools.similar_cases_stats("REK", "Weryfikacja dokumentów", "Protokół szkody")
    assert stats["count"] > 10
    assert set(stats) == {"count", "with_reminder", "median_days_with_reminder", "median_days_without_reminder"}
    # przypomnienie telefoniczne skraca oczekiwanie (tak skonstruowano dane)
    assert stats["median_days_with_reminder"] < stats["median_days_without_reminder"]


def test_visibility_by_role():
    anna, marek, barbara, ewa, admin = (get_user(u) for u in ("u_anna", "u_marek", "u_barbara", "u_ewa",
                                                              "u_tomasz"))
    assert can_view_instance(anna, "REK-2026-0412")
    assert not can_view_instance(anna, "REK-2026-0377")          # sprawa Jana
    assert can_view_instance(marek, "REK-2026-0377")             # jego zespół
    assert not can_view_instance(marek, "FAK-2026-1162")         # inny zespół
    assert can_view_instance(barbara, "FAK-2026-1162")
    assert can_view_instance(ewa, "FAK-2026-1162")               # właściciel procesu
    assert can_view_instance(admin, "REK-2026-0419")
    assert all(i["assignee_id"] == "u_anna" for i in visible_active_instances(anna))
