"""app/services/settings.py — the read/write layer for anything a platform
admin can edit from the Super Admin dashboard, without a code deploy.

Every consumer in the codebase (billing.py's plan catalog, legal_content,
the email templates) keeps its existing hardcoded value as the *default*.
get_setting() only overrides that default once an admin has actually saved
a change through the dashboard — so nothing breaks, and nothing requires
re-seeding, the moment this feature ships. It only starts mattering the
first time someone actually edits something.

Keys use a "category:name" convention (e.g. "pricing:standard_annual_usd",
"legal:terms_of_service") purely so the dashboard can group and filter them
by category — there's no separate category column, just a naming
convention, since AppSetting's existing schema is intentionally minimal.
"""
from __future__ import annotations
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, now_utc

log = logging.getLogger("settings")


def get_setting(db: Session, key: str, default: Any = None) -> Any:
    """Returns the admin-saved value for `key` if one exists — otherwise
    returns `default` completely untouched, exactly as the calling code
    already had it hardcoded. AppSetting.value is a native JSON column, so
    no manual encode/decode is needed here."""
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: Any, updated_by: str = "") -> None:
    """Creates the row on first edit, or updates it on every edit after
    that. `key` should use the "category:name" convention so the dashboard
    can group settings — e.g. set_setting(db, "pricing:standard_annual_usd", 1500, ...)."""
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value, updated_by=updated_by)
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
        row.updated_at = now_utc()
    db.commit()


def get_settings_by_category(db: Session, category: str) -> dict[str, Any]:
    """Returns only the settings an admin has actually edited so far, for a
    given category prefix (e.g. "pricing") — used to show 'currently
    overridden' state in the dashboard. Does NOT include un-edited
    defaults; the caller merges those in from code."""
    rows = db.scalars(select(AppSetting).where(AppSetting.key.like(f"{category}:%"))).all()
    return {row.key.split(":", 1)[1]: row.value for row in rows}
