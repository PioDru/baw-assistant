"""Moduł generowania raportów analitycznych w formacie PDF (fpdf2 + markdown)."""
from __future__ import annotations

from datetime import datetime
import os
import re
import unicodedata

from fpdf import FPDF
import markdown

FONT_CANDIDATES: list[dict[str, str | None]] = [
    # Linux / Docker (Debian/Ubuntu)
    {
        "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "italic": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    },
    {
        "regular": "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "bold": "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "italic": "/usr/share/fonts/TTF/DejaVuSans-Oblique.ttf",
    },
    {
        "regular": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "bold": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "italic": "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    },
    # Windows
    {
        "regular": "C:/Windows/Fonts/arial.ttf",
        "bold": "C:/Windows/Fonts/arialbd.ttf",
        "italic": "C:/Windows/Fonts/ariali.ttf",
    },
    {
        "regular": "C:/Windows/Fonts/segoeui.ttf",
        "bold": "C:/Windows/Fonts/segoeuib.ttf",
        "italic": "C:/Windows/Fonts/segoeuii.ttf",
    },
    {
        "regular": "C:/Windows/Fonts/calibri.ttf",
        "bold": "C:/Windows/Fonts/calibrib.ttf",
        "italic": "C:/Windows/Fonts/calibrii.ttf",
    },
    # macOS
    {
        "regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
        "bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "italic": "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    },
    {
        "regular": "/Library/Fonts/Arial.ttf",
        "bold": "/Library/Fonts/Arial Bold.ttf",
        "italic": "/Library/Fonts/Arial Italic.ttf",
    },
]


def _find_available_fonts() -> dict[str, str] | None:
    """Wyszukuje pierwszy dostępny zestaw czcionek TrueType ze wsparciem Unicode."""
    for candidate in FONT_CANDIDATES:
        reg = candidate.get("regular")
        if reg and os.path.exists(reg):
            res = {"regular": reg}
            bold = candidate.get("bold")
            if bold and os.path.exists(bold):
                res["bold"] = bold
            else:
                res["bold"] = reg
            italic = candidate.get("italic")
            if italic and os.path.exists(italic):
                res["italic"] = italic
            else:
                res["italic"] = reg
            return res
    return None


def _fallback_ascii(text: str) -> str:
    """Zastępuje znaki specjalne znakami ASCII w przypadku braku czcionki Unicode."""
    polish_map = {
        "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z",
        "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z",
        "„": '"', "”": '"', "•": "*", "–": "-", "—": "-", "…": "...",
    }
    for pl, asc in polish_map.items():
        text = text.replace(pl, asc)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


class AnalyticsPDFReport(FPDF):
    """Szablon raportu analitycznego PDF z nagłówkiem i stopką."""

    def __init__(self, process_name: str, period_label: str, generated_at: datetime | None = None) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.process_name = process_name
        self.period_label = period_label
        self.generated_at = generated_at or datetime.now()
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(left=18, top=18, right=18)

        # Konfiguracja czcionki
        fonts = _find_available_fonts()
        if fonts:
            self.font_family_name = "UnicodeFont"
            self.add_font(self.font_family_name, "", fonts["regular"])
            self.add_font(self.font_family_name, "B", fonts["bold"])
            self.add_font(self.font_family_name, "I", fonts["italic"])
            self.has_unicode_font = True
        else:
            self.font_family_name = "helvetica"
            self.has_unicode_font = False

    def sanitize_text(self, text: str) -> str:
        """Sanityzacja tekstu pod kątem używanej czcionki."""
        if not self.has_unicode_font:
            return _fallback_ascii(text)
        return text

    def header(self) -> None:
        self.set_font(self.font_family_name, "B", 9)
        self.set_text_color(100, 116, 139)
        title = self.sanitize_text(f"BAW Assistant · Raport analityczny procesu: {self.process_name}")
        self.cell(0, 6, title, align="L")
        self.ln(7)
        # Linia separatora pod nagłówkiem
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.3)
        self.line(18, self.get_y(), 210 - 18, self.get_y())
        self.ln(6)

    def footer(self) -> None:
        self.set_y(-15)
        # Linia separatora nad stopką
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.3)
        self.line(18, self.get_y(), 210 - 18, self.get_y())
        self.ln(2)

        self.set_font(self.font_family_name, "I", 8)
        self.set_text_color(148, 163, 184)
        date_str = self.generated_at.strftime("%Y-%m-%d %H:%M")
        footer_text = self.sanitize_text(f"Wygenerowano: {date_str} | Strona {self.page_no()}/{{nb}}")
        self.cell(0, 8, footer_text, align="C")


def generate_analytics_pdf(
    process_name: str,
    period_label: str,
    markdown_content: str,
    generated_at: datetime | None = None,
) -> bytes:
    """Generuje dokument PDF z treści raportu w formacie Markdown."""
    pdf = AnalyticsPDFReport(process_name, period_label, generated_at)
    pdf.alias_nb_pages()
    pdf.add_page()

    # Karta tytułowa / Nagłówek raportu
    pdf.set_font(pdf.font_family_name, "B", 18)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 10, pdf.sanitize_text("Raport analityczny AI"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font(pdf.font_family_name, "", 10)
    pdf.set_text_color(71, 85, 105)
    meta_text = f"Proces: {process_name}   |   Okres: {period_label}"
    pdf.cell(0, 6, pdf.sanitize_text(meta_text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Akcentowa linia pozioma
    pdf.set_draw_color(59, 130, 246)
    pdf.set_line_width(0.8)
    pdf.line(18, pdf.get_y(), 210 - 18, pdf.get_y())
    pdf.ln(6)

    # Przygotowanie i konwersja Markdown na HTML
    sanitized_md = pdf.sanitize_text(markdown_content or "*(Brak zawartości raportu)*")
    html_content = markdown.markdown(sanitized_md, extensions=["extra", "nl2br", "sane_lists"])

    pdf.set_font(pdf.font_family_name, "", 10)
    pdf.set_text_color(30, 41, 59)
    pdf.write_html(html_content)

    return bytes(pdf.output())
