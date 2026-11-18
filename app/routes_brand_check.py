"""routes_brand_check.py: the free Brand Exposure Check. The public request and the results (read with the check's secret token), and the
Super Admin controls. Public routes are rate limited; the worker does the slow part (services/brand_check.process_queue)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .deps import current_member
from .models import BrandCheck, Organization, OrgMember
from .services import brand_check, trial
from .services.ratelimit import RateLimiter

router = APIRouter()
_start_ip, _result_ip = RateLimiter(10, 3600), RateLimiter(600, 3600)      # 10 new checks an hour per IP; generous polling


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def _admin(member: OrgMember, db: Session) -> None:
    if db.get(Organization, member.organization_id).billing_status != "exempt":
        raise HTTPException(403, "The brand check is managed by the BrandsLens team.")


class SettingsBody(BaseModel):
    enabled: bool | None = None
    daily_cap: int | None = None
    per_email: int | None = None
    per_company: int | None = None


class StartBody(BaseModel):
    email: str
    brand: str
    website: str
    marketing_consent: bool = False
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_term: str = ""
    utm_content: str = ""
    session_id: str = ""


@router.get("/api/public/brand-check/status")
def brand_check_status(response: Response, db: Session = Depends(get_db)) -> dict:
    """Is the free brand check on? The website uses this to show the form or a 'not available' message."""
    response.headers["Cache-Control"] = "public, max-age=30"
    return {"open": brand_check.get_settings(db)["enabled"], "news_days": brand_check.NEWS_DAYS}


@router.post("/api/public/brand-check")
def brand_check_start(body: StartBody, request: Request, db: Session = Depends(get_db)) -> dict:
    if not _start_ip.allow(_ip(request)):
        raise HTTPException(429, "Too many checks from this connection. Please try again later.")
    try:
        row = brand_check.start(db, email=body.email, brand=body.brand, website=body.website, consent=body.marketing_consent,
                                utm={"source": body.utm_source, "medium": body.utm_medium, "campaign": body.utm_campaign,
                                     "term": body.utm_term, "content": body.utm_content},
                                session_id=body.session_id, ip_hash=trial.hash_ip(_ip(request)))
    except brand_check.BrandCheckError as e:
        raise HTTPException(e.status, str(e))
    return {"id": row.id, "token": row.token, **brand_check.public_view(db, row)}


@router.get("/api/public/brand-check/{check_id}")
def brand_check_result(check_id: str, t: str, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    if not _result_ip.allow(_ip(request)):
        raise HTTPException(429, "Too many requests. Please wait a moment.")
    row = db.get(BrandCheck, check_id)
    import hmac
    if not row or not t or not hmac.compare_digest(row.token, t):
        raise HTTPException(404, "Check not found.")
    response.headers["Cache-Control"] = "no-store"
    return brand_check.public_view(db, row)


# ---------------------------------------------------------------- Super Admin
@router.get("/api/admin/brand-check/settings")
def admin_settings(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return {"settings": brand_check.get_settings(db), "summary": brand_check.summary(db)}


@router.put("/api/admin/brand-check/settings")
def admin_save_settings(body: SettingsBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return {"settings": brand_check.save_settings(db, member.email, body.model_dump(exclude_none=True)), "summary": brand_check.summary(db)}


@router.get("/api/admin/brand-check/checks")
def admin_list(q: str = "", limit: int = 50, offset: int = 0, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return brand_check.list_checks(db, q=q, limit_=limit, offset=offset)


@router.get("/api/admin/brand-check/export.csv")
def admin_export(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> Response:
    _admin(member, db)
    return Response(brand_check.export_csv(db), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="brandslens-brand-checks.csv"', "Cache-Control": "no-store"})


@router.get("/api/admin/scorecard")
def admin_scorecard(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    """Read only preview of the weekly scorecard email for the last full week: the figures, the accuracy checks, and the email as it
    would be sent, plus who receives it. Sends nothing and changes nothing."""
    _admin(member, db)
    from .services import scorecard
    card = scorecard.build(db)
    subject, body = scorecard.render(card)
    return {"scorecard": card, "subject": subject, "html": body, "recipients": scorecard.recipients()}
