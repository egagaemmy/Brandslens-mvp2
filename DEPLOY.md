# Deploying BrandsLens — the exact path to live

This is the specific route to get both pieces genuinely live and talking to
each other, using free tiers throughout. It takes about 10 minutes.

## Part 1 — Put the backend on GitHub (so Render can find it)

If you don't already have a GitHub account, create one free at github.com —
then, from the `brandslens-mvp` folder:

```bash
git init
git add .
git commit -m "BrandsLens MVP"
```

Create a new **empty** repository on github.com (no README, no .gitignore —
just the bare repo), then:

```bash
git remote add origin https://github.com/<your-username>/brandslens-mvp.git
git branch -M main
git push -u origin main
```

## Part 2 — Deploy the backend on Render (free)

1. Go to **render.com**, sign up (free, no card required for this tier).
2. Click **New +** → **Web Service**.
3. Connect your GitHub account, select the `brandslens-mvp` repo you just pushed.
4. Render will detect `render.yaml` automatically and pre-fill everything —
   just confirm.
5. Before clicking Deploy, add the one required secret: click **Environment**,
   add `ANTHROPIC_API_KEY` with your real key. Everything else in
   `render.yaml` is already set.
6. Click **Deploy**. First deploy takes 2–3 minutes.
7. When it's done, Render gives you a URL like:
   `https://brandslens-api.onrender.com` — **copy this.**

Test it immediately: open `https://brandslens-api.onrender.com/api/health`
in a browser. You should see `{"ok":true,...}`. If you see that, the backend
is genuinely live.

**Two honest things about the free tier:**
- It **spins down after 15 minutes of no traffic** and takes 30–50 seconds to
  wake back up on the next request. That first slow request after a quiet
  period isn't a bug — it's the free tier waking up.
- The SQLite database file **resets every time the service redeploys or
  restarts**. Fine for testing and demos; for anything real, upgrade to
  Render's persistent disk or a proper Postgres instance later — nothing in
  the code needs to change to do that, just the `DATABASE_URL`.

## Part 3 — Put the frontend online (2 minutes, no account needed)

1. Go to **app.netlify.com/drop**
2. Drag `brandslens.html` straight onto the page.
3. Netlify gives you a live URL instantly, like
   `https://random-name-123.netlify.app`.

## Part 4 — Connect them

Open `brandslens.html` in a text editor one more time, find this line near
the top of the `<script>` block:

```js
const API_BASE = window.BRANDSLENS_API || "http://localhost:8000";
```

Add this line directly **above** the `<script src="https://cdn.jsdelivr.net...">`
tag, with your real Render URL from Part 2:

```html
<script>window.BRANDSLENS_API = "https://brandslens-api.onrender.com";</script>
```

Re-drag the updated file onto Netlify Drop to update it (or drag it into the
same site again — Netlify Drop redeploys on every drop).

## Part 5 — Tighten security (optional, but do this before real customers)

Right now the backend accepts requests from any website (`FRONTEND_ORIGIN=*`
in `render.yaml`). Once you have your real Netlify URL, go back to Render →
your service → Environment, and change `FRONTEND_ORIGIN` to your exact
Netlify URL (e.g. `https://random-name-123.netlify.app`, no trailing slash).
This stops any other website from calling your API directly.

## That's it

At the end of this: a real public URL for the app, a real public URL for
the API, and the two talking to each other over the internet — the exact
"failed to fetch" error you were hitting locally happens precisely because
neither of these existed yet.

## If something doesn't work

Open the browser console (F12) on the live Netlify page and try signing up
again. A real error there — not "failed to fetch" — will say something
specific (a CORS message, a 500, a timeout). Send me that exact text and
I'll fix the precise thing, the same way we found the actual login bug
earlier: by looking at what really happened, not guessing.
