# Launch release: backend (24 November 2026)

This release adds the launch offer, the read-only public demo, the visit tracker and a tool to re-score old mentions.

## New in the database
- Three new tables: `promo_codes`, `signup_attributions`, `launch_events`.
- One new column: `org_members.demo_viewer` (a flag on the shared demo visitor).
Run the schema sync once after deploying: `/api/setup/sync-schema?secret=...`. It reports what it added; check `org_members.demo_viewer` is listed.

## One-time setup after deploying (same setup secret as the other routes)
1. `/api/setup/seed-launch-codes?secret=...` creates PH15, HN15, PEERLIST15 and IH15 (15% off the first year of an annual plan, from midnight Lagos time on 24 Nov 2026 for 31 days). Safe to run twice; it never overwrites a code you edited. Options: `percent`, `starts_on`, `days`.
2. `/api/setup/seed-demo?secret=...&reset=true` creates the fictional Acme Foods demo accounts (never scanned, never emailed, never counted as subscribers). The public demo button uses the Professional one. If a demo already exists, the button still works: it creates its visitor on demand.
3. After topping up the AI credit: `/api/setup/ai-check?secret=...`, then `/api/setup/reclassify?secret=...` (a dry run: it reports what it would change). When happy: add `&dry_run=false`.

## What the worker now does
Every 3 hours it rebuilds ONLY the Professional demo so its response clocks always show a realistic time remaining. The Standard and Growth demos are untouched and the Professional owner keeps their password. Switch off with `DEMO_AUTO_RESET=off`; change the interval with `DEMO_RESET_HOURS`.

## New public endpoints (no login)
- `POST /api/public/demo-session` : logs a visitor into the read-only demo (6 hours, rate limited).
- `GET  /api/public/promo/{code}` : what a code would take off, shown before payment.
- `POST /api/public/track` : counts one anonymous funnel step (no IP address stored).

## New admin endpoints (BrandsLens team accounts only)
`GET/POST /api/admin/promos`, `PATCH /api/admin/promos/{id}`, `GET /api/admin/launch/summary`. The app's Super Admin has a Launch tab for all of these.

## How the offer is charged
A recurring Flutterwave plan has a fixed amount, so a discounted first year is charged as ONE-TIME prepaid year at the reduced price (like the pay-in-naira route). When it ends the customer renews at the regular price. Buying several years at once discounts only the first.

## How the demo stays read-only
The lock is in `app/deps.py`, in the one dependency every protected route uses, so it covers all current and future routes. A demo visitor may only call the few harmless actions listed there. AI Draft and Ask BrandsLens return fixed sample answers (no AI spend). Ask BrandsLens answers sentiment questions (for example "What is driving negative sentiment today?") from the demo's real sentiment numbers.

## Tests
`pip install -r requirements-dev.txt` then `python -m pytest tests`. 99 tests, about a minute and a half.
