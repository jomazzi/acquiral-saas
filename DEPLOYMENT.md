# Deploying Acquiral

## Recommendation: Render

Render was chosen over Railway, Fly.io, and AWS for this deployment because:
- **Cheapest realistic option for a small, always-on production app.** The
  Starter web service + Starter Postgres combination runs close to the
  lowest cost of any of these that doesn't risk your data (see the cost
  note below on why the *free* Postgres tier is not used here).
- **Simplest to operate without a dedicated ops person.** HTTPS,
  restarts, and basic scaling are automatic; there's no server to patch.
- **Managed Postgres with a normal connection string** — no extra setup
  needed for our Row-Level Security approach (see `ARCHITECTURE.md`):
  the database user Render gives you is never a superuser or
  `BYPASSRLS` role, which is exactly what RLS requires, and
  `scripts/setup_rls.sql` already works against it unchanged.
- AWS would give more control and lower cost *at scale*, but needs far
  more manual security/networking configuration to do safely — not
  worth it yet for one small NGO's app.

Everything below is built around a **Docker image**
(`Dockerfile` at the repo root), so if you ever want to move to Railway,
Fly.io, or DigitalOcean App Platform instead, the same image works there
with only the environment-variable setup below changing — see
"Other platforms" at the end.

## Cost (approximate, September 2026 pricing — check Render's pricing page for current numbers)

| Item | Plan | ~Cost/month |
|---|---|---|
| Web service | Starter | ~$7 |
| Postgres | Starter (1GB) | ~$7 |
| **Total** | | **~$14/month** |

**Why not Render's free tier?** Render's free Postgres databases are
automatically deleted after a fixed period. That's fine for a demo you
can rebuild, but this app holds real accounting/payroll data — losing it
silently is not an acceptable risk, so `render.yaml` and the steps below
use the cheapest **non-expiring** tier instead. The free *web service*
tier (which just spins down when idle, no data-loss risk) is a
reasonable way to try things out before upgrading — see the note in
Step 3.

## Prerequisites

1. This codebase pushed to a GitHub repository (Render deploys from a
   git repo, not a zip upload). If you don't have one yet:
   ```bash
   git init
   git add .
   git commit -m "Acquiral SaaS"
   # create an empty repo on GitHub first, then:
   git remote add origin https://github.com/<you>/acquiral-saas.git
   git push -u origin main
   ```
2. A free Render account at https://render.com (sign in with GitHub —
   makes connecting the repo a one-click step).

## Option A: One-shot Blueprint deploy (recommended)

1. In the Render Dashboard: **New +** → **Blueprint**.
2. Connect your GitHub account if you haven't, then select the
   `acquiral-saas` repository. Render will detect `render.yaml`
   automatically and show you the two resources it will create (the web
   service and the database).
3. Click **Apply**. Render provisions the Postgres database, builds the
   Docker image, runs `scripts/release.sh` (migrations + RLS setup) as
   the pre-deploy step, and starts the web service — all env vars
   (`DATABASE_URL`, a random `SECRET_KEY`, `APP_ENV=production`) are
   wired up automatically per `render.yaml`.
4. Wait for the deploy to finish (a few minutes the first time), then
   open the service's `.onrender.com` URL — you should see the Acquiral
   landing page.

That's it — skip to **After the first deploy** below.

## Option B: Manual click-through (if you'd rather not use Blueprints)

1. **Create the database first.** Dashboard → **New +** → **PostgreSQL**.
   - Name: `acquiral-db`
   - Plan: **Starter** (not Free — see cost note above)
   - Create it, then copy its **Internal Database URL** once it's ready.
2. **Create the web service.** Dashboard → **New +** → **Web Service**.
   - Connect the `acquiral-saas` GitHub repo.
   - Runtime: **Docker** (Render will find the `Dockerfile` automatically).
   - Plan: Starter (or Free, to try it first — see note below).
   - **Pre-Deploy Command**: `sh scripts/release.sh`
   - **Environment Variables**:
     - `APP_ENV` = `production`
     - `SECRET_KEY` = (click "Generate" for a random value)
     - `DATABASE_URL` = paste the Internal Database URL from step 1
   - Click **Create Web Service**.
3. Wait for the build + deploy to finish, then open the given
   `.onrender.com` URL.

**About the free web-service plan**: it works, but spins the app down
after 15 minutes of no traffic and takes ~30-60 seconds to wake back up
on the next request. That's a fine way to try the deployment before
paying anything, but switch to Starter before relying on it for real use
so it doesn't feel broken to a user hitting a cold start.

## After the first deploy

- **There is no seed data.** Visit the site and use **"Start free"** to
  create your first organization and admin user — this is the normal
  signup flow, exactly like running it locally. There is one tenant per
  organization created this way; each gets its own independently-seeded
  chart of accounts.
- **Verify HTTPS and login work end-to-end**: sign up, log in, create a
  funder or an account, log out, log back in. This exercises the
  `ProxyFix`/secure-cookie config added for this phase — if login
  redirects loop or cookies don't stick, double check `APP_ENV=production`
  is actually set (see `config.py` / `app/__init__.py`: this is what
  turns on `BEHIND_PROXY` and secure cookies).
- **Check RLS actually applied**: Render's dashboard → your database →
  **Shell**, then:
  ```sql
  SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
  WHERE relname IN ('accounts','journal_entries','invoices','bank_transactions');
  ```
  All should show `t` in both columns (this is exactly what
  `scripts/release.sh` applies automatically on every deploy via
  `scripts/setup_rls.sql` — the query above just double-checks it landed).

## Backups

Render's Starter Postgres plan includes automated daily backups with a
short retention window (check current details on Render's pricing page,
as this changes). For anything beyond that window, or extra peace of
mind, run a manual dump periodically:
```bash
pg_dump "$DATABASE_URL" > acquiral-backup-$(date +%F).sql
```
(from the Render dashboard's database page, under "Connect", using the
**External** connection string so this works from your own machine).

## Custom domain

Not set up yet (per the agreed roadmap — this phase just gets a working
URL). When you're ready: Render's web service → **Settings** →
**Custom Domains** → add your domain and follow Render's DNS
instructions (a `CNAME` record, usually). HTTPS is issued automatically
once DNS is verified — no extra configuration needed on the app side.

## Other platforms (same Docker image, different env var setup)

Since everything is packaged as a standard Docker image, moving to
another host mainly means re-doing the environment-variable and
release-step setup, not touching the app itself:

- **Railway**: New Project → Deploy from GitHub repo (auto-detects the
  `Dockerfile`) → add a Postgres plugin (gives you `DATABASE_URL`
  automatically) → set `APP_ENV=production` and `SECRET_KEY` → set
  `sh scripts/release.sh` as a **Deploy → Release Command**.
- **Fly.io**: `fly launch` (detects the `Dockerfile`), `fly postgres
  create` + `fly postgres attach` (wires up `DATABASE_URL`), set
  `APP_ENV`/`SECRET_KEY` with `fly secrets set`, and add
  `scripts/release.sh` as a `release_command` in the generated
  `fly.toml`.
- **DigitalOcean App Platform**: create an App from the repo (it
  detects the `Dockerfile`), add a managed Postgres database resource,
  set the same three env vars, and set the Pre-Deploy Job command to
  `sh scripts/release.sh`.

In every case, the two things that matter are: (1) `DATABASE_URL` points
at a Postgres role that is **not** a superuser and does **not** have
`BYPASSRLS` (true by default on every managed Postgres offering), and
(2) `scripts/release.sh` runs once per deploy, before traffic reaches
the new instance(s).
