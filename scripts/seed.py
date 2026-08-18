"""Seed one example organization + owner + workspace so you can see the whole
thing work end to end in minutes, via the real signup path (not a shortcut —
this exercises the exact code a real customer's signup would run).
Run: python -m scripts.seed  (from the mvp/ directory)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import SessionLocal, init_db
from app.models import OrgMember, Workspace
from app.services import auth

NG_RSS = ["https://nairametrics.com/feed/", "https://businessday.ng/feed/", "https://punchng.com/feed/"]

def main() -> None:
    init_db()
    db = SessionLocal()
    if db.query(OrgMember).filter_by(email="owner@acme.example").first():
        print("Example account already exists — skipping. Log in with owner@acme.example / password12345")
        return
    member, token = auth.signup(db, company="Acme Example Brand", sector="Financial Services",
                                plan="professional", name="Ada Owner", email="owner@acme.example",
                                password="password12345")
    ws = db.query(Workspace).filter_by(organization_id=member.organization_id).first()
    ws.rss_feeds = NG_RSS
    ws.brand_domains = ["acme.example.com"]
    ws.youtube_query = "Acme brand"
    db.commit()

    print("Seeded organization:", member.organization_id)
    print("Owner login: owner@acme.example / password12345")
    print("Session token (for quick testing):", token)
    print(f"\nTrigger a scan with:\n  curl -X POST http://localhost:8000/api/scan/{ws.id} -H \"Authorization: Bearer {token}\"")
    print(f"\nCheck results with:\n  curl http://localhost:8000/api/workspaces/{ws.id} -H \"Authorization: Bearer {token}\"")

if __name__ == "__main__":
    main()
