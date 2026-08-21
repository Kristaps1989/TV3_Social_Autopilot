# Connecting the TV3 social accounts (secure setup)

The admin UI has a guided flow at **`/connect`** — no passwords ever touch
this system; each platform's official login grants a token, and only the
token is stored (in the service database; env vars work as a fallback).

## Before anything: protect the admin UI

The deployment is on the public internet. Set these Railway variables first:

```
ADMIN_PASSWORD=<strong password>     # login: user "admin"
PUBLIC_BASE_URL=https://<your-app>.up.railway.app
ANTHROPIC_API_KEY=<key>              # enables AI copy + format decisions
```

Also make sure the Railway service uses **Postgres** (`DATABASE_URL`); with
the default SQLite the queue and any connected tokens are wiped on every
deploy, because Railway's filesystem is ephemeral.

## Facebook Page (one-time, ~30 min, needs the Business Manager admin)

1. The person who administers TV3's Business Manager creates an app at
   developers.facebook.com → *Create App* → type **Business**, owned by the
   TV3 Business Manager.
2. App settings → add the **Facebook Login for Business** product → Valid
   OAuth Redirect URIs → add `https://<your-app>.up.railway.app/connect/facebook/callback`.
3. Set `META_APP_ID` and `META_APP_SECRET` in Railway.
4. Open `/connect` in the admin UI → **Savienot ar Facebook** → log in as a
   person who manages the tv3.lv Page → pick the Page.

The stored Page token is derived from a long-lived login and does not
expire. While the app is in Development Mode this works for admins/testers
of the app who manage the Page — for anyone else Meta requires App Review
(`pages_manage_posts`), a short screencast of this exact flow.

Alternative without OAuth: Business Settings → System Users → create one,
assign the Page, generate a never-expiring token with `pages_manage_posts` +
`pages_read_engagement`, and set `FB_PAGE_ID` / `FB_PAGE_ACCESS_TOKEN`.

## Threads (same Meta app)

1. In the same app add the **Threads API** use case with `threads_basic` +
   `threads_content_publish`, and its own app-level credentials.
2. Redirect URI: `https://<your-app>.up.railway.app/connect/threads/callback`.
3. Set `THREADS_APP_ID` / `THREADS_APP_SECRET` in Railway.
4. `/connect` → **Savienot ar Threads** → log in as the TV3 sports account.

Threads tokens last 60 days. The daily maintenance job refreshes them
automatically ~2 weeks before expiry and alerts (log/Slack) if a refresh
fails or any token has < 7 days left.

## X (no button — env vars only)

X requires a paid API tier and offers no practical OAuth shortcut:

1. Log in to developer.x.com **as the @TV3Zinas account**, subscribe to a
   tier that covers ~1,200 posts/month.
2. Create an app, permissions **Read and Write**.
3. Generate and set all four values: `X_API_KEY`, `X_API_SECRET`,
   `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.

## Security model

- Tokens live in the `credentials` DB table (or env), never in git.
- OAuth round-trips are CSRF-protected (single-use `state`).
- The whole admin UI sits behind HTTP Basic auth once `ADMIN_PASSWORD` is set.
- Rotating a token = clicking the connect button again; nothing to redeploy.
- Keep `DRY_RUN=true` until proposed posts have been reviewed; flipping a
  channel live is a separate, deliberate step.
