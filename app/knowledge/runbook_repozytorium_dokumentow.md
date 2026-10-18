---
title: Instrukcja – brak połączenia z repozytorium dokumentów (ecm-gateway)
kind: runbook
process_id: ALL
audience: [admin]
---

## Objawy
W logach baw-server pojawia się błąd DocumentService „Connection refused (ecm-gateway:9443)”, a instancje w krokach
wymagających załączników są wstrzymywane.

## Najczęstsze przyczyny
Zatrzymana lub przeciążona brama integracyjna ecm-gateway albo wygasły certyfikat TLS bramy.

## Działania
Sprawdź stan bramy integracyjnej i ważność jej certyfikatu. Zrestartuj bramę. Po przywróceniu połączenia wznów
wstrzymane instancje i potwierdź, że nowe wywołania DocumentService kończą się powodzeniem.

## Historia
Poprzedni incydent tego typu został rozwiązany restartem bramy integracyjnej.
