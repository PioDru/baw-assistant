"""Worker trybu systemowego (osobny kontener): harmonogram skanów i przetwarzanie kolejki zdarzeń.

- co SCAN_INTERVAL_MIN minut (ustawienie „scan_interval_min”) dodaje do kolejki zdarzenie skanu,
- co QUEUE_POLL_SECONDS sekund przetwarza oczekujące zdarzenia (UCA, EDA, harmonogram) przez graf agentów.
Uruchomienie: python -m baw_assistant.worker
"""
from __future__ import annotations

import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler

from . import db, events, kb
from .data_generator import generate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("baw.worker")
logging.getLogger("apscheduler").setLevel(logging.WARNING)   # bez wpisu co 15 s
logging.getLogger("httpx").setLevel(logging.WARNING)


def enqueue_scan() -> None:
    eid = events.enqueue("SCHEDULER", "scan", {})
    log.info("Harmonogram: dodano skan do kolejki (zdarzenie #%s)", eid)


def drain_queue() -> None:
    done = events.process_pending()
    for ev in done:
        log.info("Zdarzenie #%s (%s %s): %s", ev["id"], ev["source"], ev["type"], ev["status"])


def main() -> None:
    generate()                      # dane syntetyczne, jeśli jeszcze ich nie ma
    try:
        log.info("Baza wiedzy: zindeksowano sekcje: %s", kb.ensure_index())
    except Exception as exc:        # Qdrant może startować dłużej – wyszukiwanie zwróci wtedy pustą listę
        log.warning("Indeksowanie bazy wiedzy nie powiodło się: %s", exc)
    interval = int(os.getenv("SCAN_INTERVAL_MIN", db.get_setting("scan_interval_min")))
    poll = int(os.getenv("QUEUE_POLL_SECONDS", "15"))
    scheduler = BlockingScheduler(timezone="Europe/Warsaw")
    scheduler.add_job(enqueue_scan, "interval", minutes=interval, id="scan")
    scheduler.add_job(drain_queue, "interval", seconds=poll, id="queue", max_instances=1, coalesce=True)
    log.info("Worker uruchomiony: skan co %s min, kolejka co %s s", interval, poll)
    enqueue_scan()
    scheduler.start()


if __name__ == "__main__":
    main()
