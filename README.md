# Asystent operacyjny IBM BAW – PoC v1 (MVP)

Pierwsza wersja systemu wieloagentowego z dokumentu projektowego: etapy 0–3, czyli pełny łańcuch
dla pracownika. Dyspozytor rozpoznaje pytanie i sprawdza uprawnienia, Agent Instancji zbiera fakty
o sprawie, a Redaktor-Weryfikator kontroluje odpowiedź, zanim trafi do użytkownika.
Dane są syntetyczne, bez połączenia z IBM BAW.

```
Użytkownik ─► Streamlit ─► Dyspozytor ──► Agent Instancji ──► Redaktor-Weryfikator ─► odpowiedź + źródła + ślad
                              │  (intencja, uprawnienia)   (fakty z SQLite)  ▲ │ (grounding, uprawnienia)
                              │                                 └─ poprawka ─┘ │
                              └── odmowa / brak kontekstu / funkcja PoC v2 ────┘
```

## Co działa w PoC v1

| Element | Zakres |
| --- | --- |
| Dane syntetyczne | 3 procesy, ~590 instancji z historią, zadaniami, dokumentami i logami; scenariusze ze znaną odpowiedzią |
| Streamlit | wybór roli i użytkownika (symulacja logowania), menu zależne od roli, czat w kontekście instancji, źródła, panel „Jak powstała odpowiedź” |
| Dyspozytor | intencja przez LLM (structured output) albo reguły; uprawnienia zawsze w kodzie; odmowy, prośba o kontekst, informacja o funkcjach PoC v2 |
| Agent Instancji | diagnoza sprawy, szczegóły (dokumenty, historia, osoby), przegląd moich spraw, statystyka podobnych spraw, akcje sugerowane |
| Redaktor-Weryfikator | każda liczba, data, godzina i numer sprawy musi wystąpić w faktach; odnośniki [F#]; jedna poprawka, potem blokada tekstu modelu |
| Obserwowalność | ślad agentów w UI i w tabeli `agent_runs`; pełne ślady i koszty w Langfuse |
| Pozostałe strony | Pulpit zespołu, Analityka procesu, Centrum monitoringu: surowe dane (agenci Analityk i Monitor w PoC v2) |

Akcje sugerowane są widoczne, ale nieaktywne: zatwierdzanie przez człowieka (`interrupt()`) to etap 6.

## Szybki start (Docker Compose)

Wymagania: Docker z Compose v2; dla pełnego stosu z Langfuse (ClickHouse, Postgres, Redis, MinIO) przydziel Dockerowi co najmniej 8 GB RAM.

```bash
cp .env.example .env          # uzupełnij OPENAI_API_KEY (albo ustaw LLM_MODEL=mock)
docker compose up -d --build
```

| Usługa | Adres |
| --- | --- |
| Aplikacja | http://localhost:8501 |
| Langfuse | http://localhost:3000 (login: `admin@baw.local` / `baw-admin-123`, zmienisz w `.env`) |
| Qdrant | http://localhost:6333/dashboard (używany od etapu 6) |

Tylko aplikacja, bez Langfuse i Qdrant:

```bash
LANGFUSE_ENABLED=false docker compose up -d --build app
```

Dane syntetyczne powstają przy pierwszym starcie w wolumenie `app_data`. Ponowne wygenerowanie:

```bash
docker compose exec app python -m baw_assistant.data_generator --force
```

## Uruchomienie bez Dockera

```bash
cd app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_MODEL=mock LANGFUSE_ENABLED=false   # albo OPENAI_API_KEY=... dla GPT-6 Luna
python -m baw_assistant.data_generator
streamlit run streamlit_app.py
```

## Model LLM

Domyślnie `openai:gpt-6-luna` przez LangChain `init_chat_model`. Inny model to zmiana jednej zmiennej:

| `LLM_MODEL` | Działanie |
| --- | --- |
| `openai:gpt-6-luna` | GPT-6 Luna przez API OpenAI (wymaga `OPENAI_API_KEY`) |
| `openai:openai/gpt-6-luna` + `OPENAI_BASE_URL=https://openrouter.ai/api/v1` | ten sam model przez OpenRouter |
| `mock` | bez LLM: reguły i szablony, do testów i demo offline |

Gdy modelu nie da się użyć (brak klucza, błąd sieci), agenci automatycznie przechodzą na reguły
i szablony, a panel boczny pokazuje ostrzeżenie. Zasada projektowa: liczby i daty zawsze liczy kod,
a model tylko je opisuje.

## Scenariusze demo

| Rola i użytkownik | Kontekst | Pytanie | Oczekiwany wynik |
| --- | --- | --- | --- |
| Pracownik, Anna Kowalska | REK-2026-0412 | Dlaczego ta sprawa stoi? Co mam zrobić dalej? | czeka na protokół szkody od klienta, brak błędów technicznych, akcja „Przygotuj przypomnienie” |
| Pracownik, Anna Kowalska | — | Dlaczego REK-2026-0377 stoi? | odmowa: sprawa kolegi |
| Pracownik, Anna Kowalska | — | Pokaż sprawy całego działu | odmowa: brak dostępu do przeglądu zespołu |
| Pracownik, Jan Mazur | — | Co mam pilnego? | 3 sprawy po terminie SLA |
| Kierownik, Marek Nowak | REK-2026-0377 | Dlaczego ta sprawa stoi? | czeka na dział techniczny, termin przekroczony |
| Kierownik, Marek Nowak | — | Kto ma najwięcej przeterminowanych zadań? | informacja: Agent Analityk w PoC v2 |
| Administrator | — | Dlaczego REK-2026-0419 stoi? | błąd techniczny repozytorium dokumentów z 18.10.2026 10:24 |

## Testy

```bash
cd app && pytest -q
# albo w kontenerze:
docker compose exec app pytest -q
```

- `tests/golden_questions.yaml`: zestaw pytań ze znaną odpowiedzią (intencja, decyzja, werdykt, wymagane fakty);
- `test_routing_accuracy_meets_target`: kryterium akceptacji, trafność routingu ≥ 90%;
- `test_verifier_and_llm_path.py`: grounding, odrzucanie halucynacji, poprawka i blokada, odporność uprawnień
  na błędną klasyfikację modelu (model zastąpiony atrapą);
- `test_tools_and_rbac.py`: liczby z narzędzi kontra dane z generatora, widoczność spraw dla ról.

## Struktura

```
docker-compose.yml        aplikacja + Qdrant + Langfuse (web, worker, Postgres, ClickHouse, Redis, MinIO)
.env.example              konfiguracja
app/
  streamlit_app.py        wejście: role, nawigacja
  ui/                     strony: Asystent, Pulpit, Analityka, Monitoring, Agenci
  baw_assistant/
    data_generator.py     dane syntetyczne i scenariusze testowe
    db.py, tools.py       SQLite i narzędzia odczytu (docelowo BAW REST API)
    rbac.py               uprawnienia ról
    llm.py, tracing.py    model (init_chat_model) i Langfuse
    graph.py              graf LangGraph
    agents/               dispatcher, instance_agent, editor_verifier
  tests/
```

## Kolejne etapy (z dokumentu projektowego)

4. Agent Analityk: KPI w pandas, Pulpit zespołu i Analityka procesu z raportem AI.
5. Agent Monitor: kolejka zdarzeń UCA/EDA, harmonogram, alerty, historia powiadomień.
6. Akcje sugerowane z `interrupt()` (człowiek w pętli), statystyki agentów, Qdrant (dokumentacja, incydenty, podobne sprawy).

Następnie JEV / BALAS, integracja z BAW na Cloud Pak (REST API, Kafka, UCA), prawdziwe powiadomienia
(zadanie BAW, e-mail, Teams) i osadzenie czatu w Coach UI.
