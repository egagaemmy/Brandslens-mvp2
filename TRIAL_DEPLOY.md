# Free trial: backend notes

Full details are in `BrandsLens_Free_Trial_Release_Notes.md`. For the backend:

- **Routes:** `routes_trial.py` (public start, verify, request-access, status, unsubscribe; the signed-in `trial/me`; and the admin controls, leads list, CSV export, bulk extend and nurture sequence). **Logic:** `services/trial.py`.
- **Off by default.** Nothing is public until an admin switches the trial on in Super Admin.
- **Database:** new tables `trial_signups` and `trial_emails` (created automatically) and four new columns on `organizations` (`trial_started_at`, `trial_ends_at`, `trial_ai_drafts`, `trial_ask_messages`). **Run `/api/setup/sync-schema` after deploying.**
- **Settings (environment):** `TRIAL_FROM` (default `BrandsLens <info@mail.brandslens.com>`), `TRIAL_REPLY_TO` and `LEAD_NOTIFY_EMAIL` (default `info@brandslens.com`), `PUBLIC_API_URL` (for the unsubscribe link). Mailchimp uses the existing `MAILCHIMP_*` settings.
- **Mailchimp:** consenting leads are added on verification, and unsubscribing through our link also unsubscribes them there.
- **Tests:** `tests/test_trial.py` (111) and `tests/test_trial_additions.py` (16). Whole suite: 290.
