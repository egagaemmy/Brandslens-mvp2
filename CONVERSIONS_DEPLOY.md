# Conversions from Super Admin (no redeploy per conversion)

After this one deployment of the backend and the app, every new conversion is added in Super Admin, Tracking. No code change, no redeploy.

## What changed
* Backend: new table `tracking_conversions` (created automatically at start up, nothing to migrate). Admin routes:
  `POST /api/admin/tracking/tags/{id}/conversions`, `PATCH` and `DELETE /api/admin/tracking/conversions/{id}`.
  The public `/api/public/tracking-config?scope=app` now carries `conversions: [{on, value}]` per tag.
* App: `src/lib/conversions.ts` reads that list once per visit and fires what matches. The app reports the moments
  `trial_signup` (TrialScreen), `purchase` (after a confirmed payment) and `login`. Page conversions (`page:/dashboard`,
  `page:/reports*`) need no code at all.
* Super Admin, Tracking: a "Runs on" choice (Marketing site, App, Both) and a Conversions panel under each tag.

## Formats per platform
Google Ads (AW- tag only): conversion label. Meta and TikTok: event name. LinkedIn: conversion ID. X: event ID (tw-…).

## The trial signup conversion
1. Super Admin, Tracking. Add tag: Google tag, ID `AW-16962957208`, Runs on **App** (or Both).
2. Click **Add** in its Conversions column. When: Free trial account created. Conversion label: `cVOXCJCfj7cdEJjfyJg_`.

The hardcoded purchase conversion in adTracking.ts still fires as before and is skipped from the Super Admin list,
so it is never counted twice.
