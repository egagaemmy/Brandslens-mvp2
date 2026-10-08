"""routes_guides.py — the 'Watch the guide' links.

Each tutorial video has an ID (V01 to V19). The video's address is stored
here as an ordinary admin editable setting, so a link can be added the day
a video is published, with no code change and no redeploy. The public list
only ever contains videos that have an address, which is how the app knows
whether to show a link at all.
"""
from __future__ import annotations
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .deps import active_member
from .models import OrgMember, Organization
from .services.settings import get_setting, set_setting

router = APIRouter()
KEY = "guides:urls"
ID_PATTERN = re.compile(r"^V\d{2}$")


def _urls(db: Session) -> dict[str, str]:
    raw = get_setting(db, KEY, {}) or {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and v}


@router.get("/api/guides")
def public_guides(response: Response, db: Session = Depends(get_db)) -> dict:
    """Public: a video address is not a secret, and the login and sign up
    screens show a guide before anyone is logged in."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return {"guides": _urls(db)}


class GuidesBody(BaseModel):
    guides: dict[str, str]


def _require_admin(member: OrgMember, db: Session) -> None:
    if db.get(Organization, member.organization_id).billing_status != "exempt":
        raise HTTPException(403, "Guide links are managed by the BrandsLens team.")


@router.get("/api/admin/guides")
def admin_get_guides(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    return {"guides": _urls(db)}


@router.put("/api/admin/guides")
def admin_save_guides(body: GuidesBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """Replaces the whole set. A blank address removes that video's link."""
    _require_admin(member, db)
    clean: dict[str, str] = {}
    for vid, url in body.guides.items():
        url = url.strip()
        if not ID_PATTERN.match(vid):
            raise HTTPException(422, f"'{vid}' is not a valid guide ID. Use V01 to V99.")
        if not url:
            continue
        if not url.startswith("https://") or len(url) > 300 or " " in url:
            raise HTTPException(422, f"{vid}: the address must start with https:// and contain no spaces.")
        clean[vid] = url
    set_setting(db, KEY, clean, updated_by=member.email)
    return {"ok": True, "count": len(clean)}
