"""Command line entry point:  python -m app.seed_demo [--reset] [--remove]
Uses whatever DATABASE_URL the app is configured with."""
import argparse, json
from .db import init_db, SessionLocal
from .services import demo_seed

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Seed or remove the Acme Foods demo accounts.")
    p.add_argument("--reset", action="store_true", help="rebuild the demo accounts with fresh dates and new passwords")
    p.add_argument("--remove", action="store_true", help="delete every demo account and all its data")
    a = p.parse_args()
    init_db()
    db = SessionLocal()
    try:
        print(json.dumps(demo_seed.remove_demo(db) if a.remove else demo_seed.seed_demo(db, reset=a.reset), indent=2))
    finally:
        db.close()
