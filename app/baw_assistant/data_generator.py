"""Generator danych syntetycznych dla PoC.

Tworzy organizację, 3 procesy, kilkaset instancji z historią, zadaniami i dokumentami
oraz logi systemowe ze wstrzykniętymi anomaliami. Dane są ułożone wokół SIM_NOW,
a scenariusze ze znaną odpowiedzią (sprawy REK-2026-0412, -0377, -0381, -0390, -0419)
są dodawane jawnie, żeby testy i demo dawały zawsze te same wyniki.

Uruchomienie:  python -m baw_assistant.data_generator [--force | --if-missing]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import datetime, timedelta

from faker import Faker

from .config import get_settings
from .db import connect, database_ready, init_schema

# --- Organizacja ---------------------------------------------------------------

TEAMS = [
    ("T_REK", "Zespół reklamacji", "u_marek"),
    ("T_BO", "Zespół back-office", "u_barbara"),
]

USERS = [
    # id, imię i nazwisko, rola, zespół
    ("u_anna", "Anna Kowalska", "pracownik", "T_REK"),
    ("u_jan", "Jan Mazur", "pracownik", "T_REK"),
    ("u_ola", "Ola Lewandowska", "pracownik", "T_REK"),
    ("u_piotr", "Piotr Wójcik", "pracownik", "T_REK"),
    ("u_kasia", "Kasia Dąbrowska", "pracownik", "T_REK"),
    ("u_zofia", "Zofia Krawczyk", "pracownik", "T_BO"),
    ("u_adam", "Adam Kaczmarek", "pracownik", "T_BO"),
    ("u_marek", "Marek Nowak", "kierownik", "T_REK"),
    ("u_barbara", "Barbara Zając", "kierownik", "T_BO"),
    ("u_ewa", "Ewa Wiśniewska", "wlasciciel", None),
    ("u_tomasz", "Tomasz Zieliński", "admin", None),
]

# Kroki procesów ze średnim czasem trwania (dni).
PROCESSES = {
    "REK": {
        "name": "Obsługa reklamacji", "sla_days": 7, "team": "T_REK", "owner": "u_ewa",
        "steps": [("Rejestracja", 0.2), ("Weryfikacja dokumentów", 1.4), ("Opinia techniczna", 0.8),
                  ("Decyzja", 0.4), ("Zamknięcie", 0.2)],
        "count": 300,
    },
    "FAK": {
        "name": "Zatwierdzanie faktur", "sla_days": 5, "team": "T_BO", "owner": "u_ewa",
        "steps": [("Rejestracja faktury", 0.1), ("Weryfikacja formalna", 0.7),
                  ("Akceptacja kierownika", 0.9), ("Księgowanie", 0.3)],
        "count": 220,
    },
    "ONB": {
        "name": "Onboarding pracownika", "sla_days": 10, "team": "T_BO", "owner": "u_ewa",
        "steps": [("Przygotowanie umowy", 1.5), ("Konta i dostępy", 1.2), ("Sprzęt", 2.0),
                  ("Szkolenie wstępne", 1.0)],
        "count": 60,
    },
}

DOC_REQUIRED = {"REK": ["Formularz reklamacji", "Zdjęcia uszkodzenia", "Protokół szkody"],
                "FAK": ["Faktura PDF", "Zamówienie"],
                "ONB": ["Umowa", "Ankieta osobowa"]}

GOLDEN_NUMBERS = {"REK": {412, 377, 381, 390, 419}, "FAK": {1162}, "ONB": set()}
START_NUMBER = {"REK": 101, "FAK": 900, "ONB": 40}

# Obciążenie: Jan Mazur ma najwięcej spraw, Kasia najmniej.
ASSIGNEE_WEIGHTS = {"u_anna": 5, "u_jan": 6, "u_ola": 4, "u_piotr": 3, "u_kasia": 1,
                    "u_zofia": 5, "u_adam": 4}


def _fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def _work_time(dt: datetime) -> datetime:
    """Przesuwa znacznik do godzin pracy (8–17), żeby historia wyglądała realistycznie."""
    if dt.hour < 8:
        dt = dt.replace(hour=8, minute=dt.minute)
    elif dt.hour >= 17:
        dt = (dt + timedelta(days=1)).replace(hour=8 + dt.hour % 3, minute=dt.minute)
    return dt


class Generator:
    def __init__(self, now: datetime, seed: int):
        self.now = now
        self.rng = random.Random(seed)
        self.fake = Faker("pl_PL")
        self.fake.seed_instance(seed)
        self.instances: list[tuple] = []
        self.tasks: list[tuple] = []
        self.events: list[tuple] = []
        self.documents: list[tuple] = []
        self.logs: list[tuple] = []

    # --- pomocnicze ---------------------------------------------------------
    def _team_members(self, team: str) -> list[str]:
        return [u[0] for u in USERS if u[3] == team and u[2] == "pracownik"]

    def _pick_assignee(self, team: str) -> str:
        members = self._team_members(team)
        return self.rng.choices(members, weights=[ASSIGNEE_WEIGHTS[m] for m in members])[0]

    def _name(self, user_id: str) -> str:
        return next(u[1] for u in USERS if u[0] == user_id)

    def _duration(self, mean_days: float) -> timedelta:
        return timedelta(days=max(0.05, self.rng.gammavariate(2.0, mean_days / 2.0)))

    def _ev(self, iid: str, ts: datetime, etype: str, step: str | None, actor: str, details: str = "") -> None:
        self.events.append((iid, _fmt(ts), etype, step, actor, details))

    # --- instancje tła --------------------------------------------------------
    def generate_background(self) -> None:
        for prefix, proc in PROCESSES.items():
            numbers = [n for n in range(START_NUMBER[prefix], START_NUMBER[prefix] + proc["count"] + 10)
                       if n not in GOLDEN_NUMBERS[prefix]][: proc["count"]]
            # Daty utworzenia rosną razem z numerem sprawy.
            # ~85% historii z ostatnich 4 miesięcy, ~15% świeżych spraw (aktywne instancje).
            offsets = sorted((self.rng.uniform(0.3, 12.0) if self.rng.random() < 0.15
                              else self.rng.uniform(12.0, 120.0) for _ in numbers), reverse=True)
            for number, days_ago in zip(numbers, offsets):
                created = _work_time(self.now - timedelta(days=days_ago))
                self._background_instance(prefix, proc, f"{prefix}-2026-{number:04d}", created)

    def _background_instance(self, prefix: str, proc: dict, iid: str, created: datetime) -> None:
        assignee = self._pick_assignee(proc["team"])
        actor = self._name(assignee)
        sla_due = created + timedelta(days=proc["sla_days"])
        customer = self.fake.company() if prefix == "FAK" else self.fake.name()
        channel = self.rng.choice(["online", "online", "telefon", "oddział"]) if prefix == "REK" else "system"
        self._ev(iid, created, "CREATED", proc["steps"][0][0], actor, f"Kanał: {channel}")
        self.documents.append((iid, DOC_REQUIRED[prefix][0], 1, _fmt(created)))

        ts = created
        status, current, completed_at, missing = "Zakończona", None, None, []
        for idx, (step, mean) in enumerate(proc["steps"]):
            self._ev(iid, ts, "STEP_ENTERED", step, actor)
            duration = self._duration(mean)
            # Kanał telefoniczny częściej kończy się brakami w dokumentach.
            doc_added = False
            needs_doc =(prefix == "REK" and step == "Weryfikacja dokumentów"
                         and self.rng.random() < (0.55 if channel == "telefon" else 0.3))
            if needs_doc:
                req_ts = ts + timedelta(hours=2)
                self._ev(iid, req_ts, "DOCUMENT_REQUESTED", step, actor, "Brak: Protokół szkody")
                reminder = self.rng.random() < 0.5
                wait = timedelta(days=self.rng.uniform(1.0, 2.5) if reminder else self.rng.uniform(2.5, 6.0))
                if reminder:
                    self._ev(iid, req_ts + timedelta(days=1), "COMMENT", step, actor,
                             "Przypomnienie telefoniczne do klienta")
                duration = max(duration, wait + timedelta(hours=3))
                doc_ts = req_ts + wait
                doc_added = doc_ts < self.now
                if doc_added:
                    self._ev(iid, doc_ts, "DOCUMENT_ADDED", step, "Klient", "Protokół szkody")
                    self.documents.append((iid, "Protokół szkody", 1, _fmt(doc_ts)))
            end = ts + duration
            if end >= self.now:
                status, current = "W toku", step
                self.tasks.append((iid, step, "user", assignee, "Otwarte", _fmt(ts), _fmt(sla_due), None))
                if needs_doc and not doc_added:
                    missing = ["Protokół szkody"]
                break
            self.tasks.append((iid, step, "user", assignee, "Zakończone", _fmt(ts), _fmt(sla_due), _fmt(end)))
            self._ev(iid, end, "TASK_COMPLETED", step, actor)
            ts = end
            if idx == len(proc["steps"]) - 1:
                completed_at = end
                self._ev(iid, end, "COMPLETED", step, actor)

        last_change = max(datetime.fromisoformat(e[1]) for e in self.events if e[0] == iid)
        self.instances.append((
            iid, prefix, status, current, assignee, proc["team"], customer, channel,
            _fmt(created), _fmt(sla_due), _fmt(last_change),
            _fmt(completed_at) if completed_at else None, json.dumps(missing, ensure_ascii=False),
        ))

    # --- scenariusze ze znaną odpowiedzią ------------------------------------
    def generate_golden(self) -> None:
        d = lambda s: datetime.fromisoformat(s)  # noqa: E731

        # 1) REK-2026-0412: czeka na protokół szkody od klienta od 14.10, brak błędów technicznych.
        iid = "REK-2026-0412"
        self.instances.append((iid, "REK", "W toku", "Weryfikacja dokumentów", "u_anna", "T_REK",
                               "Krzysztof Malinowski", "telefon", "2026-10-14T09:10", "2026-10-21T09:10",
                               "2026-10-14T11:30", None, json.dumps(["Protokół szkody"], ensure_ascii=False)))
        ev = [
            ("2026-10-14T09:10", "CREATED", "Rejestracja", "Anna Kowalska", "Kanał: telefon"),
            ("2026-10-14T09:10", "STEP_ENTERED", "Rejestracja", "Anna Kowalska", ""),
            ("2026-10-14T09:12", "DOCUMENT_ADDED", "Rejestracja", "Klient", "Formularz reklamacji"),
            ("2026-10-14T09:15", "DOCUMENT_ADDED", "Rejestracja", "Klient", "Zdjęcia uszkodzenia"),
            ("2026-10-14T09:40", "TASK_COMPLETED", "Rejestracja", "Anna Kowalska", ""),
            ("2026-10-14T09:40", "STEP_ENTERED", "Weryfikacja dokumentów", "Anna Kowalska", ""),
            ("2026-10-14T11:30", "DOCUMENT_REQUESTED", "Weryfikacja dokumentów", "Anna Kowalska",
             "Brak: Protokół szkody"),
            ("2026-10-14T11:30", "TASK_CREATED", "Weryfikacja dokumentów", "Anna Kowalska",
             "Uzupełnienie załączników → klient"),
        ]
        for ts, et, step, actor, det in ev:
            self._ev(iid, d(ts), et, step, actor, det)
        self.documents += [(iid, "Formularz reklamacji", 1, "2026-10-14T09:12"),
                           (iid, "Zdjęcia uszkodzenia", 1, "2026-10-14T09:15")]
        self.tasks += [
            (iid, "Rejestracja", "user", "u_anna", "Zakończone", "2026-10-14T09:10", "2026-10-21T09:10",
             "2026-10-14T09:40"),
            (iid, "Weryfikacja dokumentów", "user", "u_anna", "Otwarte", "2026-10-14T09:40", "2026-10-21T09:10",
             None),
            (iid, "Uzupełnienie załączników", "customer", "Klient", "Otwarte", "2026-10-14T11:30",
             "2026-10-17T11:30", None),
        ]

        # 2) Trzy przeterminowane sprawy Jana Mazura czekające na opinię działu technicznego.
        for iid, created, due in [("REK-2026-0377", "2026-10-06T10:00", "2026-10-13T10:00"),
                                  ("REK-2026-0381", "2026-10-07T13:20", "2026-10-14T13:20"),
                                  ("REK-2026-0390", "2026-10-09T08:45", "2026-10-16T08:45")]:
            entered = d(created) + timedelta(days=1, hours=2)
            self.instances.append((iid, "REK", "W toku", "Opinia techniczna", "u_jan", "T_REK",
                                   self.fake.name(), "online", created, due, _fmt(entered), None, "[]"))
            self._ev(iid, d(created), "CREATED", "Rejestracja", "Jan Mazur", "Kanał: online")
            self._ev(iid, d(created) + timedelta(hours=3), "STEP_ENTERED", "Weryfikacja dokumentów", "Jan Mazur")
            self._ev(iid, entered, "STEP_ENTERED", "Opinia techniczna", "Jan Mazur")
            self._ev(iid, entered, "TASK_CREATED", "Opinia techniczna", "Jan Mazur",
                     "Prośba o opinię → dział techniczny")
            self.documents += [(iid, "Formularz reklamacji", 1, created), (iid, "Protokół szkody", 1, created)]
            self.tasks.append((iid, "Opinia techniczna", "department", "Dział techniczny", "Otwarte",
                               _fmt(entered), due, None))

        # 3) REK-2026-0419: zatrzymana przez awarię repozytorium dokumentów (dziś 10:24).
        iid = "REK-2026-0419"
        self.instances.append((iid, "REK", "W toku", "Weryfikacja dokumentów", "u_piotr", "T_REK",
                               self.fake.name(), "online", "2026-10-17T14:05", "2026-10-24T14:05",
                               "2026-10-18T10:24", None, "[]"))
        self._ev(iid, d("2026-10-17T14:05"), "CREATED", "Rejestracja", "Piotr Wójcik", "Kanał: online")
        self._ev(iid, d("2026-10-18T09:50"), "STEP_ENTERED", "Weryfikacja dokumentów", "Piotr Wójcik")
        self._ev(iid, d("2026-10-18T10:24"), "SUSPENDED", "Weryfikacja dokumentów", "System",
                 "Błąd usługi dokumentów")
        self.documents.append((iid, "Formularz reklamacji", 1, "2026-10-17T14:05"))
        self.tasks.append((iid, "Weryfikacja dokumentów", "user", "u_piotr", "Wstrzymane",
                           "2026-10-18T09:50", "2026-10-24T14:05", None))

        # 4) FAK-2026-1162: przeterminowana akceptacja faktury.
        iid = "FAK-2026-1162"
        self.instances.append((iid, "FAK", "W toku", "Akceptacja kierownika", "u_zofia", "T_BO",
                               "Kowalczyk i Wspólnicy sp. k.", "system", "2026-10-12T09:00",
                               "2026-10-17T09:00", "2026-10-13T15:00", None, "[]"))
        self._ev(iid, d("2026-10-12T09:00"), "CREATED", "Rejestracja faktury", "Zofia Krawczyk")
        self._ev(iid, d("2026-10-13T15:00"), "STEP_ENTERED", "Akceptacja kierownika", "Zofia Krawczyk")
        self.documents += [(iid, "Faktura PDF", 1, "2026-10-12T09:00")]
        self.tasks.append((iid, "Akceptacja kierownika", "user", "u_barbara", "Otwarte",
                           "2026-10-13T15:00", "2026-10-17T09:00", None))

    # --- logi -----------------------------------------------------------------
    def generate_logs(self) -> None:
        components = ["baw-server", "event-emitter", "ecm-gateway", "dictionary-service"]
        ts = self.now - timedelta(days=3)
        while ts < self.now - timedelta(minutes=30):
            comp = self.rng.choice(components)
            if self.rng.random() < 0.08:
                self.logs.append((_fmt(ts), "WARN", comp, None, "Czas odpowiedzi usługi powyżej 2 s"))
            else:
                self.logs.append((_fmt(ts), "INFO", comp, None, "Heartbeat OK"))
            ts += timedelta(minutes=self.rng.randint(10, 30))

        # Anomalie dnia dzisiejszego.
        self.logs += [
            ("2026-10-18T09:12", "WARN", "baw-server", "FAK-2026-1162",
             "Walidacja danych: brak numeru NIP kontrahenta"),
            ("2026-10-18T09:58", "WARN", "event-emitter", None, "Opóźnienie kolejki zdarzeń: 312 s"),
            ("2026-10-18T10:24", "ERROR", "baw-server", "REK-2026-0419",
             "DocumentService call failed: Connection refused (ecm-gateway:9443), retry 5/5"),
            ("2026-10-18T10:24", "WARN", "baw-server", "REK-2026-0419",
             "Activity \"Weryfikacja dokumentów\" suspended"),
            ("2026-10-18T10:31", "ERROR", "baw-server", None,
             "DocumentService call failed: Connection refused (ecm-gateway:9443), retry 5/5"),
        ]

    # --- zapis ----------------------------------------------------------------
    def write(self, conn: sqlite3.Connection) -> None:
        init_schema(conn)
        for table in ("teams", "users", "processes", "instances", "tasks", "events", "documents", "logs"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany("INSERT INTO teams VALUES (?,?,?)", TEAMS)
        conn.executemany(
            "INSERT INTO users VALUES (?,?,?,?,?)",
            [(u[0], u[1], u[2], u[3], f"{u[0][2:]}@baw.local") for u in USERS],
        )
        conn.executemany(
            "INSERT INTO processes VALUES (?,?,?,?,?,?)",
            [(k, p["name"], p["sla_days"], p["owner"], p["team"],
              json.dumps([s for s, _ in p["steps"]], ensure_ascii=False)) for k, p in PROCESSES.items()],
        )
        conn.executemany("INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", self.instances)
        conn.executemany(
            "INSERT INTO tasks (instance_id, name, assignee_type, assignee, status, created_at, due_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?)", self.tasks)
        conn.executemany(
            "INSERT INTO events (instance_id, ts, type, step, actor, details) VALUES (?,?,?,?,?,?)", self.events)
        conn.executemany(
            "INSERT INTO documents (instance_id, name, version, uploaded_at) VALUES (?,?,?,?)", self.documents)
        conn.executemany(
            "INSERT INTO logs (ts, level, component, instance_id, message) VALUES (?,?,?,?,?)", self.logs)


def generate(force: bool = False) -> dict[str, int]:
    settings = get_settings()
    if not force and database_ready():
        return {}
    gen = Generator(settings.sim_now, settings.seed)
    gen.generate_background()
    gen.generate_golden()
    gen.generate_logs()
    with connect() as conn:
        gen.write(conn)
    return {"instances": len(gen.instances), "events": len(gen.events), "tasks": len(gen.tasks),
            "documents": len(gen.documents), "logs": len(gen.logs)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generuje dane syntetyczne BAW do SQLite.")
    parser.add_argument("--force", action="store_true", help="nadpisz istniejące dane")
    parser.add_argument("--if-missing", action="store_true", help="generuj tylko, gdy bazy brak (domyślnie)")
    args = parser.parse_args()
    stats = generate(force=args.force)
    if stats:
        print("Wygenerowano dane syntetyczne:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    else:
        print("Baza danych już istnieje – pomijam generowanie (użyj --force, aby nadpisać).")


if __name__ == "__main__":
    main()
