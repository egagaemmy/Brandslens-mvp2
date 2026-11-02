# Free trial: backend notes

Full details are in `BrandsLens_Free_Trial_Release_Notes.md`. For the backend:

- **Routes:** `routes_trial.py` (public start, verify, request-access, status, unsubscribe; the signed-in `trial/me`; and the admin controls, leads list, CSV export, bulk extend and nurture sequence). **Logic:** `services/trial.py`.
- **Off by default.** Nothing is public until an admin switches the trial on in Super Admin.
- **Database:** new tables `trial_signups` and `trial_emails` (created automatically) and four new columns on `organizations` (`trial_started_at`, `trial_ends_at`, `trial_ai_drafts`, `trial_ask_messages`). **Run `/api/setup/sync-schema` after deploying.**
- **Settings (environment):** `TRIAL_FROM` (default `BrandsLens <info@mail.brandslens.com>`), `TRIAL_REPLY_TO` and `LEAD_NOTIFY_EMAIL` (default `info@brandslens.com`), `PUBLIC_API_URL` (for the unsubscribe link). Mailchimp uses the existing `MAILCHIMP_*` settings.
- **Mailchimp:** consenting leads are added on verification, and unsubscribing through our link also unsubscribes them there.
- **Offer codes on the signed-in checkout:** `POST /api/billing/checkout` accepts `promo_code` (annual, card checkout only), validated with the same rules as sign-up. The discount is recorded when checkout starts and counted when payment is confirmed (`promos.mark_org_paid`), and only if the amount paid matches the quote. No schema change.
- **Emails:** signed Pat from BrandsLens; defaults only (a saved sequence in Super Admin is kept).
- **Tests:** `tests/test_trial.py`, `tests/test_trial_additions.py` and `tests/test_billing_offer_code.py` (15). Whole suite: 315.

- **Offer code at checkout:** `/api/billing/checkout` (signed-in accounts) accepts `promo_code` (annual, card checkout only) with the same rules as sign-up. The trial emails link to `/billing?promo=CODE`.
- **Email copy:** `DEFAULT_STEPS` in `services/trial.py`, signed "Pat from BrandsLens". The sender default is `TRIAL_FROM`.
