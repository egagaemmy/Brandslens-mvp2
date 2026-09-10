# BrandsLens MVP — Live, Wired, and Tested

Real accounts, real monitoring, a real escalation protocol, and now real
branded reporting — a working backend proven with live end-to-end tests, plus
a new landing page and web app built around BrandsLens's actual brand system
(logo, colors, typography, tagline).

## What's new in this pass

- **Full rebrand** to BrandsLens across the backend, database models, legal
  drafts, and documentation.
- **A real landing page + login/signup + web app**, in `brandslens.html`,
  wired to the live backend below — not a disconnected demo.
- **Server-generated, brand-styled PDF and Excel reports** (`/api/reports/pdf`,
  `/api/reports/excel`) — genuinely tested, not mocked. See `app/branding.py`
  for the single source of truth on colors used across the dashboard, PDF,
  and Excel exports.
- **A pre-aggregated analytics endpoint** (`/api/analytics/{workspace_id}`)
  powering real Chart.js charts on the dashboard, Analytics tab, and the
  Reports preview.
- **A workspace settings endpoint** (`PATCH /api/workspaces/{id}`) so
  keywords, brand domains, and RSS feeds can actually be edited after signup,
  not just set once at creation.
- **A `/api/me` convenience endpoint** for simple frontend session bootstrap.

## Brand system (app/branding.py)

Colors are taken directly from the logo files in `assets/`: deep navy canvas
(`#0B0F17` → `#0F172A`), amber/gold primary accent (`#F59E0B` → `#D97706`),
red for critical/alert states (`#EF4444`). Typography is Instrument Serif
(display) paired with Inter (body/UI) — the same pairing set in the logo
itself. The frontend and the PDF/Excel generator both read from this same
palette, so a color never means something different in one place than
another.

## Running it (same as before, ~10 minutes)

```bash
cd brandslens-mvp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt --break-system-packages

cp .env.example .env       # at minimum, set ANTHROPIC_API_KEY

python -m scripts.seed     # creates a real example account via the real signup path
uvicorn app.main:app --reload
```

Then open `brandslens.html` in a browser. By default it points at
`http://localhost:8000` — override with:
```html
<script>window.BRANDSLENS_API = "https://your-deployed-api.com";</script>
```
placed before the Chart.js script tag, if you're hosting the frontend
separately from the API.

## Verifying the reporting features yourself

```bash
# after signing up and getting a token (see TESTING.md for the full sequence):
curl http://localhost:8000/api/reports/pdf?ws_id=<id> -H "Authorization: Bearer <token>" -o report.pdf
curl http://localhost:8000/api/reports/excel?ws_id=<id> -H "Authorization: Bearer <token>" -o export.xlsx
curl http://localhost:8000/api/analytics/<id> -H "Authorization: Bearer <token>"
```

Both exports were generated and rendered during this build to confirm they
actually produce a valid, branded file — not just that the endpoint returns
`200`.

## What's still ahead

- Actual deployment to a public URL (needs your hosting account)
- Real Stripe/Paystack keys (checkout fails safely with a clear message
  until these are set — see `app/services/billing.py`)
- Legal review of the Terms of Service and Privacy Policy drafts in `legal/`
- Facebook/Instagram, TikTok, and X (still off by default, exactly as scoped)

See `brandslens-architecture-blueprint.md` for the full technical detail
behind everything above, and `TESTING.md` for the original live auth/isolation
test run this backend was built against.
