# Connecting the TV3 social accounts (secure setup)

The admin UI has a guided flow at **`/connect`** — no passwords ever touch
this system; each platform's official login grants a token, and only the
token is stored (in the service database; env vars work as a fallback).

## Accessing the admin UI (login)

The whole admin UI sits behind a login page. On the **first visit after
deploy** the app shows a one-time setup screen (`/setup`) where you create
the administrator password — open your Railway URL immediately after
deploying and claim it. After that every visit asks for the password at
`/login`; sessions last 30 days, and "Iziet" in the header logs out.

- Change the password: Konti page → "Nomainīt administratora paroli".
- Forgot it: set a temporary `ADMIN_PASSWORD` env var in Railway (it is
  always accepted as a valid password), log in, set a new password, then
  remove the env var.

## AI (Claude) key — no env vars needed

Konti page → "AI — Claude (Anthropic)" card: paste an API key from
console.anthropic.com → API Keys. The key is verified with a real API call
when you save it, stored in the database, and shown masked afterwards.
(`ANTHROPIC_API_KEY` as an env var still works as a fallback.)

Set `PUBLIC_BASE_URL=https://<your-app>.up.railway.app` in Railway (needed
for the OAuth redirects below), and make sure the service uses **Postgres**
(`DATABASE_URL`); with the default SQLite the queue, tokens, and the admin
password are wiped on every deploy, because Railway's filesystem is ephemeral.

Mount a **Railway Volume** at `/app/data` (or wherever `CARDS_DIR` points)
as well: rendered cards, reels and the TTS cache live there. Without it every
deploy wipes them — a queued reel or carousel can no longer be published, a
story that reused the reel falls back to its static image, and every
re-render pays ElevenLabs again. The app logs a warning at startup when queued
posts point at files that are gone.

## Google Ads (optional — the paid Discover / brand layer)

The ads autopilot can run Google campaigns next to Meta boosts: Demand Gen
(the paid feed on Discover, YouTube and Gmail) for article clicks, Display
with a CPM target for brand-franchise reach, and a small always-on Search
campaign on brand queries. Strategy and budget rules: `docs/ads-strategy.md`.

1. Google Ads → Tools → **API Center**: apply for a developer token (Basic
   access is enough for one account; approval can take a few days).
2. Google Cloud console → **OAuth client** (Desktop app). Consent screen with
   the `https://www.googleapis.com/auth/adwords` scope; obtain a **refresh
   token** for the Google account that manages the ads account (OAuth
   Playground or `oauth2l` both work).
3. Konti → **Google reklāmas**: customer ID (`123-456-7890`), developer
   token, OAuth client ID + secret, refresh token; the manager (MCC) ID only
   when you access the client account through a manager.
4. Env fallbacks: `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_DEVELOPER_TOKEN`,
   `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`,
   `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.

Every campaign the autopilot creates is named `TV3 Autopilots · …`, targets
Latvia in Latvian and declares that it contains no EU political advertising
(the politics/tragedy vetoes that guard Meta apply to Google as well).

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
