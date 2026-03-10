"""
tefilah_newsletter.py — מחולל ניוזלטר תפילה

מייצר PDF מעוצב (טורקיז | Alef + Heebo) ושולח אותו במייל.
מחזיר טקסט מרקדאון מוכן להדבקה בווצאפ.
"""
from __future__ import annotations

import sys
import base64
import os
import re

# Windows console UTF-8 support
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from dataclasses import dataclass, field
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from fpdf import FPDF
from bidi.algorithm import get_display as _bidi
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── הגדרות ────────────────────────────────────────────────────────────────────

FONTS_DIR        = Path(__file__).parent / "fonts"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE       = "tefilah_token.json"
SCOPES           = ["https://www.googleapis.com/auth/gmail.send"]

# פלטת טורקיז
C_DARK  = "#005F65"
C_MAIN  = "#007F8A"
C_LIGHT = "#4CBFBF"
C_PALE  = "#E4F7F7"
C_TEXT  = "#1A2E2E"
C_WHITE = "#FFFFFF"

# ── מבנה התוכן ────────────────────────────────────────────────────────────────

@dataclass
class PrayerEntry:
    """מייצג ניוזלטר יומי אחד על תפילה."""

    date: str             = field(default_factory=lambda: datetime.now().strftime("%d/%m/%Y"))
    hebrew_date: str      = ""   # לדוגמה: "ט׳ באדר תשפ״ו"
    topic: str            = ""   # נושא היום, לדוגמה: "כוונה בתפילה"
    tefilla_section: str  = ""   # חלק בתפילה: "שחרית", "שמו"ע - ברכת אבות", "קריאת שמע", וכו'

    # ציטוט ראשי
    quote: str        = ""
    quote_source: str = ""   # לדוגמה: "שולחן ערוך או״ח סי׳ צח"

    # סיפור
    story_title: str  = ""
    story: str        = ""
    story_source: str = ""

    # מילים להדגשה בטקסט
    highlight_words: list[str] = field(default_factory=list)

    # שליחה
    recipient_email: str = ""


# ── הורדת פונטים ──────────────────────────────────────────────────────────────

_FONTS_NEEDED = {
    "Alef-Regular.ttf": ("Alef", "400"),
    "Alef-Bold.ttf":    ("Alef", "700"),
    "Heebo-Regular.ttf":("Heebo", "400"),
    "Heebo-Bold.ttf":   ("Heebo", "700"),
}

# css2 API + User-Agent מודרני → Google Fonts מחזיר TTF ישירות
_MODERN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def setup_fonts() -> None:
    """מוריד פונטי Alef ו-Heebo מ-Google Fonts אם לא קיימים."""
    FONTS_DIR.mkdir(exist_ok=True)
    for filename, (family, weight) in _FONTS_NEEDED.items():
        target = FONTS_DIR / filename
        if target.exists():
            continue
        print(f"מוריד פונט {filename}...")
        css_url = f"https://fonts.googleapis.com/css2?family={family}:wght@{weight}&display=swap"
        css = requests.get(css_url, headers={"User-Agent": _MODERN_UA}, timeout=15).text
        match = re.search(r"url\(([^)]+\.ttf)\)", css)
        if not match:
            raise RuntimeError(f"לא מצאתי URL עבור {filename}. בדוק חיבור לאינטרנט.")
        data = requests.get(match.group(1), timeout=15).content
        target.write_bytes(data)
        print(f"  OK {filename}")


def _font_path(name: str) -> str:
    return str(FONTS_DIR / name)


def _r(text: str) -> str:
    """מחיל אלגוריתם bidi לתצוגת עברית נכונה ב-PDF."""
    return _bidi(text, base_dir='R') if text else ''


def _estimate_lines(pdf: FPDF, text: str, width: float, line_h: float) -> float:
    """מחשב גובה משוער של multi_cell לפני ציור הרקע."""
    if not text:
        return line_h
    # מדידת רוחב ממוצע לתו ואומדן שורות
    avg_char_w = pdf.get_string_width('א')
    chars_per_line = max(1, int(width / avg_char_w))
    num_lines = max(1, -(-len(text) // chars_per_line))  # עיגול כלפי מעלה
    return num_lines * line_h


def _section_title(pdf: FPDF, label: str, margin: float, content_w: float) -> None:
    """מצייר כותרת סעיף עם פס טורקיז בצד ימין (RTL)."""
    y = pdf.get_y()
    # פס צבע
    pdf.set_fill_color(76, 191, 191)
    pdf.rect(210 - margin - 5, y + 1, 4, 7, 'F')
    # טקסט כותרת
    pdf.set_font('Alef', 'B', 17)
    pdf.set_text_color(0, 95, 101)
    pdf.set_x(margin)
    pdf.cell(content_w - 6, 9, _r(label), align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)


def _write_highlighted(
    pdf: FPDF, text: str, hl_words: list[str],
    margin: float, width: float, line_h: float,
    normal_font: tuple, hl_font: tuple,
    normal_color: tuple, hl_color: tuple,
) -> None:
    """
    מציג פסקה עברית עם מילים מודגשות.
    RTL: מתחיל מהצד הימני ומתקדם שמאלה — סדר המילים נשמר.
    """
    if not hl_words:
        pdf.set_font(*normal_font)
        pdf.set_text_color(*normal_color)
        pdf.set_x(margin)
        pdf.multi_cell(width, line_h, _r(text), align='R')
        return

    hl_set = set(hl_words)
    pattern = '(' + '|'.join(map(re.escape, sorted(hl_set, key=len, reverse=True))) + ')'
    tokens  = re.split(pattern, text)

    # רשימה שטוחה של (מילה, מודגש) — בסדר המקורי
    word_pairs: list[tuple[str, bool]] = []
    for token in tokens:
        if not token:
            continue
        is_hl = token.strip() in hl_set
        for word in token.split():
            if word:
                word_pairs.append((word, is_hl))

    # רוחב רווח (מדידה בפונט רגיל)
    pdf.set_font(*normal_font)
    space_w = pdf.get_string_width(' ')

    # RTL: מתחילים מהקצה הימני זזים שמאלה
    x_right       = margin + width
    x_cur         = x_right
    y_cur         = pdf.get_y()
    first_on_line = True

    for word, is_hl in word_pairs:
        font  = hl_font  if is_hl else normal_font
        color = hl_color if is_hl else normal_color
        pdf.set_font(*font)
        pdf.set_text_color(*color)

        display = _r(word)
        word_w  = pdf.get_string_width(display)
        gap     = 0 if first_on_line else space_w

        # מעבר שורה אם אין מקום
        if not first_on_line and x_cur - gap - word_w < margin:
            x_cur = x_right
            y_cur += line_h
            gap   = 0

        x_cur -= gap + word_w
        pdf.set_xy(x_cur, y_cur)
        pdf.cell(word_w, line_h, display)
        first_on_line = False

    pdf.set_y(y_cur + line_h)


# ── יצירת PDF ────────────────────────────────────────────────────────────────

def generate_pdf(entry: PrayerEntry) -> bytes:
    """מחזיר את ה-PDF כ-bytes."""
    setup_fonts()

    W, M = 210, 20
    CW   = W - 2 * M
    LH   = 8      # line height mm

    pdf = FPDF('P', 'mm', 'A4')
    pdf.add_font('Alef',  '',  _font_path('Alef-Regular.ttf'))
    pdf.add_font('Alef',  'B', _font_path('Alef-Bold.ttf'))
    pdf.add_font('Heebo', '',  _font_path('Heebo-Regular.ttf'))
    pdf.add_font('Heebo', 'B', _font_path('Heebo-Bold.ttf'))
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    # ─── HEADER ──────────────────────────────────────────────────────────────
    pdf.set_fill_color(0, 95, 101)
    pdf.rect(0, 0, W, 42, 'F')

    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Alef', 'B', 26)
    pdf.set_xy(0, 9)
    pdf.cell(W, 12, _r('בוט התפילה'), align='C', new_x='LMARGIN', new_y='NEXT')

    date_line = entry.hebrew_date or entry.date
    pdf.set_font('Heebo', '', 11)
    pdf.set_x(0)
    pdf.cell(W, 7, _r(date_line), align='C', new_x='LMARGIN', new_y='NEXT')

    if entry.topic:
        pdf.set_font('Heebo', '', 10)
        pdf.set_x(0)
        pdf.cell(W, 6, _r(f'[ {entry.topic} ]'), align='C', new_x='LMARGIN', new_y='NEXT')

    if entry.tefilla_section:
        pdf.set_font('Heebo', '', 9)
        pdf.set_x(0)
        pdf.cell(W, 5, _r(f'| {entry.tefilla_section} |'), align='C')

    # ─── CONTENT ─────────────────────────────────────────────────────────────
    pdf.set_text_color(26, 46, 46)
    header_h = 48 if (entry.topic or entry.tefilla_section) else 44
    pdf.set_y(header_h)

    # כותרת ציטוט
    _section_title(pdf, 'ציטוט היום', M, CW)

    # תיבת ציטוט — גובה עם מרווח ביטחון x1.4
    box_y   = pdf.get_y()
    q_lines = _estimate_lines(pdf, entry.quote, CW - 14, LH)
    src_h   = LH if entry.quote_source else 0
    box_h   = (q_lines + src_h) * 1.4 + 18  # padding + safety

    pdf.set_fill_color(228, 247, 247)
    pdf.set_draw_color(76, 191, 191)
    pdf.set_line_width(0.4)
    pdf.rect(M, box_y, CW, box_h, 'FD')

    # מרכאה דקורטיבית
    pdf.set_font('Alef', '', 44)
    pdf.set_text_color(76, 191, 191)
    pdf.set_xy(W - M - 14, box_y - 3)
    pdf.cell(14, 16, '"')

    # טקסט הציטוט עם הדגשות
    pdf.set_xy(M + 7, box_y + 9)
    _write_highlighted(
        pdf, entry.quote, entry.highlight_words,
        M + 7, CW - 14, LH,
        normal_font=('Alef',  '',  14), hl_font=('Alef',  'B', 14),
        normal_color=(0, 95, 101),      hl_color=(0, 63, 67),
    )

    # מקור
    if entry.quote_source:
        pdf.set_font('Heebo', 'B', 10)
        pdf.set_text_color(0, 127, 138)
        pdf.set_x(M + 7)
        pdf.cell(CW - 14, LH, _r(entry.quote_source), align='L')

    pdf.set_y(box_y + box_h + 7)

    # ─── STORY ───────────────────────────────────────────────────────────────
    if entry.story:
        # קו הפרדה
        pdf.set_draw_color(200, 235, 235)
        pdf.set_line_width(0.8)
        pdf.line(M, pdf.get_y(), W - M, pdf.get_y())
        pdf.ln(7)

        _section_title(pdf, 'סיפור', M, CW)

        if entry.story_title:
            pdf.set_font('Alef', 'B', 13)
            pdf.set_text_color(0, 127, 138)
            pdf.set_x(M)
            pdf.multi_cell(CW, LH, _r(entry.story_title), align='R')
            pdf.ln(2)

        pdf.set_x(M)
        _write_highlighted(
            pdf, entry.story, entry.highlight_words,
            M, CW, LH,
            normal_font=('Heebo', '',  13), hl_font=('Heebo', 'B', 13),
            normal_color=(26, 46, 46),       hl_color=(0, 63, 67),
        )

        if entry.story_source:
            pdf.ln(1)
            pdf.set_font('Heebo', 'B', 10)
            pdf.set_text_color(0, 127, 138)
            pdf.set_x(M)
            pdf.cell(CW, LH, _r(entry.story_source), align='L')

    # ─── FOOTER ──────────────────────────────────────────────────────────────
    pdf.set_y(277)
    pdf.set_fill_color(228, 247, 247)
    pdf.rect(0, 277, W, 20, 'F')
    pdf.set_draw_color(76, 191, 191)
    pdf.set_line_width(0.4)
    pdf.line(M, 277, W - M, 277)
    pdf.set_font('Heebo', '', 9)
    pdf.set_text_color(0, 127, 138)
    pdf.set_x(0)
    footer = f'בוט התפילה  |  {entry.date}  |  כל המקורות מספרי קודש בלבד'
    pdf.cell(W, 10, _r(footer), align='C')

    return bytes(pdf.output())


# ── פורמט ווצאפ ──────────────────────────────────────────────────────────────

def format_whatsapp(entry: PrayerEntry) -> str:
    """מחזיר טקסט מרקדאון מעוצב לווצאפ."""
    date_line    = entry.hebrew_date or entry.date
    tefilla_line = f"🕍 *{entry.tefilla_section}*\n" if entry.tefilla_section else ""
    topic_line   = f"📌 *נושא:* _{entry.topic}_\n" if entry.topic else ""
    meta_block   = (tefilla_line + topic_line + "\n") if (tefilla_line or topic_line) else ""
    q_src        = f"\n📚 _{entry.quote_source}_" if entry.quote_source else ""

    story_block = ""
    if entry.story:
        s_title = f"\n*{entry.story_title}*\n\n" if entry.story_title else "\n\n"
        s_src   = f"\n📚 _{entry.story_source}_" if entry.story_source else ""
        story_block = (
            "\n\n━━━━━━━━━━━━━━━━\n"
            "📖 *סיפור*\n"
            "━━━━━━━━━━━━━━━━"
            f"{s_title}"
            f"{entry.story}"
            f"{s_src}"
        )

    return (
        f"✦ *בוט התפילה* ✦\n"
        f"_{date_line}_\n\n"
        f"{meta_block}"
        f"━━━━━━━━━━━━━━━━\n"
        f"✨ *ציטוט היום*\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"_{entry.quote}_"
        f"{q_src}"
        f"{story_block}"
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"_כל המקורות מספרי קודש בלבד_"
    )


# ── Gmail ──────────────────────────────────────────────────────────────────────

def _get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _send_email(service, entry: PrayerEntry, pdf_bytes: bytes) -> None:
    subject = f"בוט התפילה — {entry.hebrew_date or entry.date}"
    if entry.topic:
        subject += f" | {entry.topic}"

    msg = MIMEMultipart()
    msg["To"]      = entry.recipient_email
    msg["Subject"] = subject
    msg.attach(MIMEText(format_whatsapp(entry), "plain", "utf-8"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    filename = f"tefilah_{entry.date.replace('/', '-')}.pdf"
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[OK] מייל עם PDF נשלח ל-{entry.recipient_email}")


# ── פונקציה ראשית ────────────────────────────────────────────────────────────

def send_daily(entry: PrayerEntry) -> str:
    """
    מייצר PDF → שולח מייל עם קובץ מצורף → מדפיס טקסט ווצאפ.

    Returns:
        str: טקסט מעוצב לווצאפ (להעתקה ידנית או לאינטגרציה).
    """
    pdf = generate_pdf(entry)

    if entry.recipient_email:
        service = _get_gmail_service()
        _send_email(service, entry, pdf)
    else:
        # שמירה מקומית אם לא הוגדר מייל
        out = Path(f"tefilah_{entry.date.replace('/', '-')}.pdf")
        out.write_bytes(pdf)
        print(f"[OK] PDF נשמר: {out}")

    wa = format_whatsapp(entry)
    sep = "-" * 40
    print(f"\n{sep}\nטקסט לווצאפ:\n{sep}\n{wa}\n{sep}\n")
    return wa


# ── דוגמה לשימוש ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from content_fetcher import get_daily_entry
    send_daily(get_daily_entry())
