"""Testy modułu generowania raportu PDF."""
from __future__ import annotations

from datetime import datetime

from baw_assistant.pdf_report import AnalyticsPDFReport, _fallback_ascii, generate_analytics_pdf


def test_fallback_ascii_replaces_polish_chars():
    text = "Zażółć gęślą jaźń: 120 zł, „test” – uwaga"
    clean = _fallback_ascii(text)
    assert "Zazolc gesla jazn" in clean
    assert clean.isascii()


def test_generate_analytics_pdf_success():
    process_name = "Obsługa reklamacji"
    period_label = "Ostatnie 12 tygodni"
    markdown_content = """### Raport analityczny procesu
Okres: **Ostatnie 12 tygodni**

#### Kluczowe fakty:
- Liczba zakończonych spraw: **154**
- Mediana czasu realizacji: **3,8 dnia**
- Zrealizowane w SLA (5 dni): **82%** (spadek o 4 p.p.)
- Wąskie gardło: krok *Weryfikacja dokumentów* (średnio 2,1 dnia)

#### Wnioski i rekomendacje:
1. Dodać walidację kompletności załączników już przy rejestracji sprawy.
2. Wzmocnić kontrolę kompletności dokumentów w kanale „Portal klienta”.
3. Uruchomić automatyczne przypomnienie do klienta, gdy brakuje dokumentu.
"""
    gen_time = datetime(2026, 11, 15, 14, 30)
    pdf_bytes = generate_analytics_pdf(process_name, period_label, markdown_content, gen_time)

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_analytics_pdf_empty_content():
    pdf_bytes = generate_analytics_pdf("Proces onboarding", "Ostatnie 30 dni", "")
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500
    assert pdf_bytes.startswith(b"%PDF-")


def test_analytics_pdf_report_fallback_without_unicode_font(monkeypatch):
    monkeypatch.setattr("baw_assistant.pdf_report._find_available_fonts", lambda: None)
    pdf_bytes = generate_analytics_pdf(
        "Zatwierdzanie faktur",
        "Ostatnie 6 miesięcy",
        "Raport z polskimi znakami: ąćęłńóśźż ĄĆĘŁŃÓŚŹŻ",
    )
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500
    assert pdf_bytes.startswith(b"%PDF-")
