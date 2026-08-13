"""app/branding.py — the single source of truth for BrandsLens's visual
identity on the backend. The frontend has its own copy of these tokens (kept
in sync by hand for now — see the shared /api/branding endpoint below for a
lower-maintenance alternative once there's a build step on the frontend).

Colors are taken directly from the brand's logo files: a deep navy canvas,
an amber/gold gradient for the primary accent, and a red for alert/critical
states — not chosen independently of the brand.
"""

BRAND = {
    "name": "BrandsLens",
    "tagline": "See what matters. Understand the risk. Act confidently.",
    "attributes": ["Clarity", "Trust", "Intelligence", "Protection", "Impact"],

    # Core palette — hex, no leading '#', for easy use in reportlab/openpyxl
    "navy_dark": "0B0F17",
    "navy": "0F172A",
    "amber": "F59E0B",
    "amber_dark": "D97706",
    "red": "EF4444",
    "slate": "475569",
    "slate_light": "94A3B8",
    "off_white": "F8FAFC",

    # Severity color mapping — identical across dashboard, PDF, and Excel,
    # per the PRD's Design Philosophy requirement that these never diverge.
    "severity_colors": {
        "HIGH": "EF4444",
        "MEDIUM": "F59E0B",
        "WATCH": "10B981",
    },
    "sentiment_colors": {
        "Positive": "10B981",
        "Negative": "EF4444",
        "Neutral": "94A3B8",
    },

    # Typography — Instrument Serif (display) + Inter (body/UI), as set by
    # the logo. PDF generation falls back to Helvetica where a serif display
    # face isn't practical for dense body text (see report_generator.py).
    "font_display": "Instrument Serif",
    "font_body": "Inter",
}


def severity_color(severity: str) -> str:
    return BRAND["severity_colors"].get(severity, BRAND["slate"])


def sentiment_color(sentiment: str) -> str:
    return BRAND["sentiment_colors"].get(sentiment, BRAND["slate"])
