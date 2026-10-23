# Ad and analytics tags, campaign links and campaign reporting: backend notes

## What this adds
- **Admin routes** (BrandsLens team accounts only; a customer gets 403, no login gets 401):
  `GET/POST /api/admin/tracking/tags`, `PATCH/DELETE /api/admin/tracking/tags/{id}`, `GET/POST /api/admin/utm-links`, `DELETE /api/admin/utm-links/{id}`.
- **Public route** the websites read: `GET /api/public/tracking-config?scope=marketing` (or `app`). Cached for a minute, rate limited, and it holds only the
  enabled tags' platform, ID and consent category. No labels, no emails, no timestamps. It always says `consent_required: true`.
- **Platforms:** Meta Pixel, Google tag (Analytics `G-`, Ads `AW-`, `GT-`, Tag Manager `GTM-`), LinkedIn Insight Tag, X pixel, TikTok pixel, Pinterest tag,
  Snap Pixel, Microsoft Clarity. Anything else goes inside Google Tag Manager.
- **Campaign tags:** the launch events and signup records now also keep `utm_term` and `utm_content`, and the launch report has a `campaigns` list
  (the funnel for each exact source, medium and campaign link).

## DEPLOY: run the schema sync straight after this version goes live
`utm_term` and `utm_content` are **new columns on tables that already exist** (`launch_events`, `signup_attributions`). The two tag tables
(`tracking_tags`, `utm_links`) are new and are created automatically, but new columns on existing tables are only added by the sync.
Until it has run, the launch tracker and the launch report will fail.
1. Deploy, wait for Live.
2. Open `/api/setup/sync-schema?secret=YOURSECRET`. It must list `utm_term` and `utm_content` for both tables.
3. Open `/api/public/tracking-config?scope=marketing`. You should see `{"consent_required":true,"tags":[]}`.

## Safety rules (all tested)
- Only an **ID** is ever stored, never code. Each ID is checked against that platform's exact format when saved and **again when published**.
- The **consent category is decided by the server** from the platform and ID, never by whoever adds the tag. Google Analytics and Clarity are analytics;
  everything else, including Google Ads and Tag Manager (which can load anything), is marketing.
- UTM links may only point at BrandsLens' own sites, and campaign names are tidied to lower case letters, digits and `. _ -` (at most 60 characters, 80 for campaign, term and content).

## Fixed while testing this (they were in the existing code)
1. Saving or editing a tag that has conversion IDs crashed the server (`json` was never imported in `main.py`).
2. The destination check meant to refuse an address starting with `//` could never match, so such an address was accepted.
3. The public route published a stored ID without checking it again, so a malformed ID edited into the database by hand would have been published.
4. A launch report test was out of date: the report gained a `campaigns` list and the test still expected the old shape.

## Not built yet
Tags with the scope "app" or "both" (the app's sign-up pages) and the per-platform conversion IDs are stored and validated, but the app does not load them.
The Super Admin screen only creates marketing-site tags for that reason.

Tests: `python -m pytest tests` (163; about two and a half minutes). Tag and link tests: `tests/test_tracking_tags.py` (44). Campaign tests: `tests/test_utm_campaigns.py` (6).

## Classifier fix (found on the live server)
The scoring of mentions threw away a whole batch whenever the AI's reply could not be read, and the re-scoring tool then blamed credit. The live log showed a reply
cut off mid-sentence. The classifier now (1) allows a reply size that grows with the batch, (2) keeps every complete item from a cut-off or wrapped reply,
(3) asks again only for the missing items, at most seven calls per batch, (4) never retries an account problem, and (5) asks the AI for compact, short answers.
Tests: `tests/test_classifier_robustness.py`.
