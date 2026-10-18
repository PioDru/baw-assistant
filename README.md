# Asystent operacyjny IBM BAW – PoC v2

System 5 współpracujących agentów AI z dokumentu projektowego, etapy 0–6: wszystkie 4 role
(pracownik, kierownik, właściciel procesu, administrator) oraz tryb systemowy (zdarzenia UCA, EDA
i harmonogram). Dane są syntetyczne, bez połączenia z IBM BAW.

```
                 ┌────────────────────────── LangGraph ───────────────────────────┐
Pytanie (UI) ──► │ Dyspozytor ─┬─► Agent Instancji ─┐                              │
Zdarzenie    ──► │ (intencja,  ├─► Agent Analityk ──┼─► Redaktor-Weryfikator ─► [powiadomienia]
(kolejka)        │  uprawnienia)└─► Agent Monitor ──┘   (składa, weryfikuje)      │
                 └────────────────────────────────────────────────────────────────┘
                 SQLite (dane, kolejka, ślad) · Qdrant (baza wiedzy) · GPT-6 Luna · Langfuse
Akcja sugerowana ──► przygotowanie treści ─► interrupt() ─► Zatwierdź / Odrzuć ─► powiadomienie (symulacja)
```

## Co zawiera PoC v2

| Etap | Element | Zakres |
| --- | --- | --- |
| 0–3 | Dyspozytor, Agent Instancji, Redaktor-Weryfikator | intencje (LLM lub reguły), uprawnienia w kodzie, fakty o sprawach, grounding odpowiedzi |
| 4 | Agent Analityk | KPI w pandas: obciążenie zespołu, SLA, czasy w krokach, wąskie gardła, kanały zgłoszeń; rekomendacje; Pulpit zespołu i Analityka procesu z raportem AI |
| 5 | Agent Monitor + kolejka zdarzeń | alerty z logów, kolejek, danych i SLA (deduplikowane), diagnoza z bazy wiedzy; zdarzenia UCA/EDA/harmonogram; worker z APScheduler; powiadomienia: zadanie BAW, e-mail, Teams (symulowane) |
| 6 | Człowiek w pętli, Qdrant, rejestr agentów | akcje sugerowane z `interrupt()` i checkpointerem SQLite; baza wiedzy (opisy procesów, instrukcje incydentów) z filtrowaniem po roli; włączanie agentów, statystyki, ślad przebiegów |

**Zasady z dokumentu projektowego:**
- liczby liczy kod (pandas, SQL), a model tylko je opisuje;
- Redaktor-Weryfikator sprawdza, czy każda liczba, data, godzina i numer sprawy w odpowiedzi ma pokrycie w faktach;
- agenci mają tylko odczyt, poza powiadomieniami; akcje są sugerowane i wymagają zatwierdzenia;
- uprawnienia sprawdza kod, nigdy model.

## Szybki start (Docker Compose)

Wymagania: Docker z Compose v2. Dla pełnego stosu z Langfuse (ClickHouse, Postgres, Redis, MinIO)
przydziel Dockerowi co najmniej 8 GB RAM.

```bash
cp .env.example .env          # uzupełnij OPENAI_API_KEY (albo ustaw LLM_MODEL=mock)
docker compose up -d --build
```

| Usługa | Adres / rola |
| --- | --- |
| `app` | http://localhost:8501 – Streamlit |
| `worker` | harmonogram skanów (co 5 min) i przetwarzanie kolejki zdarzeń (co 15 s) |
| `qdrant` | http://localhost:6333/dashboard – baza wiedzy agentów |
| `langfuse-web` | http://localhost:3000 – ślady i koszty (login: `admin@baw.local` / `baw-admin-123`) |

Bez Langfuse:

```bash
LANGFUSE_ENABLED=false docker compose up -d --build app worker qdrant
```

Ponowne wygenerowanie danych syntetycznych:

```bash
docker compose exec app python -m baw_assistant.data_generator --force
```

## Uruchomienie bez Dockera

```bash
cd app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_MODEL=mock LANGFUSE_ENABLED=false QDRANT_URL=local:./data/qdrant
python -m baw_assistant.data_generator
streamlit run streamlit_app.py            # w drugim terminalu opcjonalnie: python -m baw_assistant.worker
```

Bez workera kolejkę przetwarza przycisk „Przetwórz kolejkę” w Centrum monitoringu.
Lokalny Qdrant (`local:` lub `":memory:"`) obsługuje tylko jeden proces – z workerem uruchom serwer:
`docker compose up -d qdrant` i `QDRANT_URL=http://localhost:6333`.

## Konfiguracja modeli

| Zmienna | Wartości |
| --- | --- |
| `LLM_MODEL` | `openai:gpt-6-luna` (domyślnie), `openai:openai/gpt-6-luna` + `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, `mock` (bez LLM) |
| `EMBEDDING_MODEL` | `auto` (OpenAI `text-embedding-3-small`, gdy jest klucz; inaczej lokalny embedding haszujący), `openai`, `hash` |

Gdy modelu nie da się użyć (brak klucza, błąd sieci), agenci przechodzą na reguły i szablony,
a panel boczny pokazuje ostrzeżenie.

## Scenariusze demo

| Rola | Gdzie | Co zrobić | Wynik |
| --- | --- | --- | --- |
| Pracownik (Anna Kowalska) | Asystent, sprawa REK-2026-0412 | „Dlaczego ta sprawa stoi?” | Instancji i Monitor pracują równolegle: brak protokołu szkody, brak błędów, opis kroku z bazy wiedzy |
| Pracownik | ta sama odpowiedź | kliknij „Przygotuj przypomnienie do klienta” | treść do edycji → Zatwierdź → e-mail w historii powiadomień |
| Pracownik | Asystent | „Dlaczego REK-2026-0377 stoi?” | odmowa: sprawa kolegi |
| Kierownik (Marek Nowak) | Pulpit zespołu | wykres obciążenia, „Kto ma najwięcej przeterminowanych zadań?” | Jan Mazur, 3 sprawy czekają na dział techniczny; akcja „Zaproponuj przesunięcie spraw” |
| Właściciel procesu (Ewa Wiśniewska) | Analityka procesu | „Generuj raport AI” | wąskie gardło „Weryfikacja dokumentów”, braki dokumentów częstsze w kanale telefonicznym, rekomendacje |
| Administrator | Centrum monitoringu | „Uruchom skan teraz” | alert krytyczny awarii repozytorium dokumentów z diagnozą z instrukcji, powiadomienia (zadanie BAW, e-mail, Teams) |
| Administrator | Centrum monitoringu | wstrzyknij „EDA · nowa wersja dokumentu” i przetwórz | Agent Instancji stwierdza komplet dokumentów, Anna dostaje powiadomienie w Teams i zadanie w BAW |
| Administrator | Zarządzanie agentami | wyłącz Agenta Analityka | kierownik dostaje komunikat „agent wyłączony”; wyłączenie Monitora tylko pomija go w diagnozie |

## Testy

```bash
cd app && pytest -q          # albo: docker compose exec app pytest -q
```

48 testów w trybie mock, z Qdrant w pamięci:
- `golden_questions.yaml`: 18 pytań ze znaną odpowiedzią dla 4 ról (intencja, decyzja, agenci, werdykt, wymagane fakty) i kryterium trafności routingu ≥ 90%;
- `test_v2_agents.py`: KPI kontra surowe dane, alerty i progi, deduplikacja powiadomień, zdarzenia UCA/EDA, zatwierdzanie i odrzucanie akcji, filtr roli w bazie wiedzy, wyłączanie agentów;
- `test_verifier_and_llm_path.py`: grounding, poprawka i blokada halucynacji, uprawnienia odporne na błędną klasyfikację modelu (model zastąpiony atrapą);
- `test_tools_and_rbac.py`: narzędzia odczytu i widoczność spraw dla ról.

## Struktura

```
docker-compose.yml          app, worker, Qdrant, Langfuse (web, worker, Postgres, ClickHouse, Redis, MinIO)
app/
  streamlit_app.py          wejście: role, nawigacja, start bazy wiedzy
  ui/                       Asystent, Pulpit zespołu, Analityka procesu, Centrum monitoringu, Zarządzanie agentami
  knowledge/                baza wiedzy (Markdown → Qdrant): opisy procesów, instrukcje incydentów
  baw_assistant/
    graph.py                graf LangGraph (pytania i zdarzenia)
    agents/                 dispatcher, instance_agent, analyst_agent, monitor_agent, editor_verifier, notifier
    analytics.py            KPI (pandas)          monitoring.py   reguły alertów
    events.py               kolejka zdarzeń       notifications.py  powiadomienia (symulacja)
    actions.py              akcje z interrupt()   kb.py           Qdrant i embeddingi
    worker.py               harmonogram (APScheduler)
    data_generator.py, db.py, tools.py, rbac.py, llm.py, tracing.py
  tests/
```

## Kolejne etapy

- JEV / BALAS jako filtry wejścia i wyjścia oraz testy odporności agentów.
- Integracja z BAW na Cloud Pak: REST API zamiast SQLite, Kafka zamiast kolejki w SQLite, UCA w obu kierunkach.
- Prawdziwe kanały powiadomień: zadania w BAW, SMTP, webhook Teams.
- Automatyzacja wybranych, bezpiecznych akcji; osadzenie czatu w Coach UI; logowanie SSO/LDAP.
