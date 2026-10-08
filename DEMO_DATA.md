# Demo data for the tutorial videos

Builds a fictional brand, Acme Foods, on three subscriber accounts (Standard, Growth, Professional) so the
tutorial videos can be recorded on the real screens without showing any real customer or brand.

## What it creates, for each account
- 40 mentions across News, X, Facebook, YouTube, Reddit, Nairaland and Domain Watch: 4 HIGH, 8 MEDIUM, 28 WATCH,
  mixed sentiment, a visible spike, and 6 older items found by a historical search.
- 12 Media Room cases, one per HIGH or MEDIUM mention: an open HIGH case with about three hours left, one awaiting
  approval, one under review, one just detected, and eight closed. Every audit trail is a genuine hash chain.
- 3 escalation contacts and 8 threat categories (Counterfeit Product Report is left out so a video can add it).
- Competitors (2 on Standard, 3 on Growth and Professional), a team of three, and referral earnings in every status.

## Run it
1. Deploy, then run the schema sync once (this release adds `organizations.is_demo`):
   `/api/setup/sync-schema?secret=YOUR_SETUP_SECRET`
2. Seed: `/api/setup/seed-demo?secret=YOUR_SETUP_SECRET`
   The response lists the three logins and their passwords. They are shown once. Save them.
3. Before each recording block, rebuild so dates and response clocks are fresh:
   `/api/setup/seed-demo?secret=YOUR_SETUP_SECRET&reset=true`
4. When the series is finished, remove everything:
   `/api/setup/remove-demo?secret=YOUR_SETUP_SECRET`

Or from a terminal with the same database settings: `python -m app.seed_demo` (add `--reset` or `--remove`).

## Why it is safe
Demo organisations are flagged `is_demo`, and every system job checks the flag. They are never scanned (a made up
brand would only pull in real coverage of real companies), never emailed (escalations are logged as sent but
nothing leaves), and never counted as subscribers. The Scan, Scan Competitors and Search Historical buttons reply
normally and run nothing. Removal only ever touches flagged organisations.
