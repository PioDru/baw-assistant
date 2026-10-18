---
title: Instrukcja – opóźnienie kolejki zdarzeń (event-emitter)
kind: runbook
process_id: ALL
audience: [admin]
---

## Objawy
Komponent event-emitter zgłasza ostrzeżenie „Opóźnienie kolejki zdarzeń”. Zdarzenia procesów trafiają do
analityki i agentów z opóźnieniem.

## Najczęstsze przyczyny
Chwilowy wzrost liczby zdarzeń, przeciążenie brokera Kafka albo zatrzymany konsument.

## Działania
Sprawdź opóźnienie grup konsumentów w brokerze i liczbę partycji tematu zdarzeń. Jeśli opóźnienie rośnie,
zwiększ liczbę konsumentów. Ostrzeżenie jednorazowe przy niskim ruchu można obserwować bez interwencji.
