"""app/services/report_generator.py — the real implementation of PRD §6.7
(Analytics & Reporting): server-side PDF and Excel generation, styled to the
brand identity in app/branding.py rather than left at library defaults.

Kept deliberately dependency-light: reportlab for PDF (pure Python, no
headless-browser dependency to deploy), openpyxl for Excel. Both are
genuinely production-usable, not placeholders.
"""
from __future__ import annotations
import io
from collections import Counter
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from ..branding import BRAND, severity_color, sentiment_color
from ..models import Workspace, Incident

NAVY = HexColor("#" + BRAND["navy"])
AMBER = HexColor("#" + BRAND["amber"])
SLATE = HexColor("#" + BRAND["slate"])


def _safe(text: str) -> str:
    """Escapes any text before it goes into a ReportLab Paragraph. This is
    not optional — Paragraph has its own small markup language (for bold,
    colour, etc.), so any '<', '>', or '&' in real content gets interpreted
    as broken markup instead of plain text. Real incidents come from scraped
    news/RSS/forum content and genuinely do contain raw HTML fragments (a
    literal <img alt="..." src="..." /> tag showed up in production and
    crashed PDF generation entirely before this fix existed) — this isn't a
    hypothetical edge case, it's confirmed, reproduced, real content."""
    return (str(text or "")
           .replace("&", "&amp;")
           .replace("<", "&lt;")
           .replace(">", "&gt;"))
OFFWHITE = HexColor("#" + BRAND["off_white"])


# ==================================================================
# PDF REPORT
# ==================================================================
def generate_pdf_report(workspace: Workspace, incidents: list[Incident]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=22 * mm, bottomMargin=18 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()

    brand_style = ParagraphStyle("Brand", parent=styles["Normal"], fontName="Helvetica-Bold",
                                 fontSize=10, textColor=AMBER, spaceAfter=2, characterSpace=2)
    title_style = ParagraphStyle("Title", parent=styles["Title"], fontName="Helvetica-Bold",
                                 fontSize=22, textColor=NAVY, spaceAfter=4)
    sub_style = ParagraphStyle("Sub", parent=styles["Normal"], fontName="Helvetica-Oblique",
                               fontSize=10, textColor=SLATE, spaceAfter=14)
    h2_style = ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold",
                              fontSize=13, textColor=NAVY, spaceBefore=16, spaceAfter=8)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontName="Helvetica",
                                fontSize=9.5, textColor=HexColor("#1F2937"), leading=13)

    story = [
        Paragraph(BRAND["name"].upper(), brand_style),
        Paragraph(f"{_safe(workspace.name)} — Monitoring Report", title_style),
        Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M UTC')} · "
                 f"{len(incidents)} mentions in range", sub_style),
        HRFlowable(width="100%", color=HexColor("#E5E7EB"), thickness=1, spaceAfter=12),
    ]

    # --- Summary KPI row ---
    sev_counts = Counter(i.severity for i in incidents)
    total, high, medium, watch = len(incidents), sev_counts.get("HIGH", 0), sev_counts.get("MEDIUM", 0), sev_counts.get("WATCH", 0)
    kpi_data = [["TOTAL MENTIONS", "HIGH RISK", "MEDIUM", "WATCH"],
               [str(total), str(high), str(medium), str(watch)]]
    kpi_table = Table(kpi_data, colWidths=[42 * mm] * 4)
    kpi_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), SLATE), ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 20), ("TEXTCOLOR", (0, 1), (0, 1), NAVY),
        ("TEXTCOLOR", (1, 1), (1, 1), HexColor("#" + BRAND["severity_colors"]["HIGH"])),
        ("TEXTCOLOR", (2, 1), (2, 1), HexColor("#" + BRAND["severity_colors"]["MEDIUM"])),
        ("TEXTCOLOR", (3, 1), (3, 1), HexColor("#" + BRAND["severity_colors"]["WATCH"])),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
    ]))
    story.append(kpi_table)

    # --- Severity & sentiment breakdown ---
    story.append(Paragraph("Severity &amp; Sentiment Breakdown", h2_style))
    sent_counts = Counter(i.sentiment for i in incidents if i.sentiment)
    breakdown_rows = [["Category", "Count", "Share"]]
    for label, count in list(sev_counts.items()) + list(sent_counts.items()):
        pct = f"{(count / total * 100):.0f}%" if total else "0%"
        breakdown_rows.append([label, str(count), pct])
    bt = Table(breakdown_rows, colWidths=[70 * mm, 30 * mm, 30 * mm])
    bt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), OFFWHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [OFFWHITE, HexColor("#FFFFFF")]),
        ("GRID", (0, 0), (-1, -1), 0.4, HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(bt)

    # --- Top incidents ---
    story.append(Paragraph("Top Incidents", h2_style))
    top = sorted(incidents, key=lambda i: ({"HIGH": 0, "MEDIUM": 1, "WATCH": 2}.get(i.severity, 3), -i.reach))[:12]
    for inc in top:
        color = HexColor("#" + severity_color(inc.severity))
        row = Table([[Paragraph(f'<font color="#{severity_color(inc.severity)}"><b>{inc.severity}</b></font> '
                               f'&nbsp;&nbsp;{_safe(inc.ref)} · {_safe(inc.platform)}', body_style)],
                    [Paragraph(_safe(inc.title[:220]), body_style)]], colWidths=[174 * mm])
        row.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, color),
            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#FAFAFA")),
        ]))
        story.append(row)
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", color=HexColor("#E5E7EB"), thickness=1))
    story.append(Paragraph(f"{BRAND['name']} · {BRAND['tagline']}", sub_style))

    doc.build(story)
    return buf.getvalue()


# ==================================================================
# EXCEL EXPORT
# ==================================================================
def generate_excel_export(workspace: Workspace, incidents: list[Incident]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Incidents"

    headers = ["Ref", "Severity", "Sentiment", "Tags", "Title", "Platform", "Language",
              "Status", "Reach", "Source", "Posted"]
    header_fill = PatternFill(start_color=BRAND["navy"], end_color=BRAND["navy"], fill_type="solid")
    header_font = Font(color=BRAND["off_white"], bold=True, name="Calibri", size=11)
    thin = Side(style="thin", color="E5E7EB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")
        cell.border = border
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    for r, inc in enumerate(incidents, start=2):
        values = [inc.ref, inc.severity, inc.sentiment or "", ", ".join(inc.tags or []),
                 inc.title, inc.platform, inc.lang, inc.status, inc.reach, inc.source,
                 inc.posted_at.strftime("%Y-%m-%d %H:%M") if inc.posted_at else ""]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=(c == 5))
            if c == 2:  # Severity column — brand color-coded, matching the dashboard exactly
                color = severity_color(inc.severity)
                cell.font = Font(color=color, bold=True)

    widths = [10, 10, 11, 22, 55, 14, 8, 11, 9, 12, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Summary sheet
    summary = wb.create_sheet("Summary")
    summary["A1"] = BRAND["name"]
    summary["A1"].font = Font(bold=True, size=16, color=BRAND["amber_dark"])
    summary["A2"] = f"{workspace.name} — Monitoring Export"
    summary["A2"].font = Font(bold=True, size=13, color=BRAND["navy"])
    summary["A3"] = f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M UTC')}"
    summary["A3"].font = Font(italic=True, size=10, color=BRAND["slate"])

    sev_counts = Counter(i.severity for i in incidents)
    row = 5
    summary.cell(row=row, column=1, value="Severity").font = Font(bold=True)
    summary.cell(row=row, column=2, value="Count").font = Font(bold=True)
    for sev in ("HIGH", "MEDIUM", "WATCH"):
        row += 1
        c1 = summary.cell(row=row, column=1, value=sev)
        c1.font = Font(color=severity_color(sev), bold=True)
        summary.cell(row=row, column=2, value=sev_counts.get(sev, 0))
    summary.column_dimensions["A"].width = 16
    summary.column_dimensions["B"].width = 10

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
