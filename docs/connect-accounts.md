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

1. In the same app add the **Threads API** use case with `threads_basic`,
   `threads_content_publish`, `threads_manage_replies`, `threads_read_replies`
   and `threads_manage_insights`, and its own app-level credentials. The reply
   permissions are what lets the adapter put the tv3.lv link in a reply under
   the post (`threads_link_in_reply`); `threads_manage_insights` is what makes
   `fetch_insights` return views and likes instead of nothing. If Threads
   refuses a scope that is not granted yet, the authorize page fails with a
   scope error — narrow the list with the `THREADS_SCOPES` env var (or a
   `threads_scopes` credential row) instead of shipping a release.
2. Redirect URI: `https://<your-app>.up.railway.app/connect/threads/callback`.
   Meta will not save the form unless the other two callbacks are filled in
   too: Uninstall `…/connect/threads/uninstall`, Delete
   `…/connect/threads/delete`. Both are real endpoints — they verify Meta's
   `signed_request` and drop the stored token. Confirm each URL from the
   dropdown after pasting, otherwise the field stays empty.
3. Set `THREADS_APP_ID` / `THREADS_APP_SECRET` in Railway.
4. `/connect` → **Savienot ar Threads** → log in as the TV3 sports account.

`/logs/threads-check` (button in Diagnostics) fires one real read call per
Threads permission — profile, insights and replies on the most recently
published Threads post — and prints the service's own answer for each. Use it
to satisfy Meta's "required API test calls" step and to tell a missing
permission apart from an empty result. It publishes nothing.

Next to it, **Pielikt saiti atbildē** writes the tv3.lv link as a reply under
the most recently published Threads post. Publishing does that by itself only
when `threads_link_in_reply` is on *and* the post is a media format, so this is
the way to add the link to a post that already went out without it — and the
only way to exercise `threads_manage_replies` on demand. One reply per post.

Threads tokens last 60 days. The daily maintenance job refreshes them
automatically ~2 weeks before expiry and alerts (log/Slack) if a refresh
fails or any token has < 7 days left.

## X (one button, like Facebook and Threads)

X does have an OAuth shortcut — OAuth 2.0 Authorization Code with PKCE. The
old four-key setup (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`,
`X_ACCESS_TOKEN_SECRET`) still works and takes precedence for nobody: when an
OAuth 2.0 token exists, the adapter uses it.

1. Log in to developer.x.com **as the @TV3Zinas account**, subscribe to a
   tier that covers ~1,200 posts/month.
2. App settings → **User authentication set up**: App permissions
   *Read and write*, Type of App *Web App*, Callback URI
   `https://<your-app>.up.railway.app/connect/x/callback`.
3. Keys and tokens → copy the **OAuth 2.0 Client ID** (and Client Secret if
   the app is confidential) into Konti → X.
4. Press **«Pieslēgties ar X kontu»**, log in, approve. Done.

Two details that otherwise cost a day:

- **`tweet.write` alone is not enough.** Posting text and uploading the image
  are separate permissions; without `media.write` the text goes out and the
  image comes back 403. Both are requested.
- **The access token lives two hours.** `offline.access` gives a refresh
  token, and the system rotates it on use and once a day. Without it the
  connection dies silently after lunch — so the connect page says so when the
  refresh token is missing.

OAuth 2.0 also changes which endpoints are used: media goes through
`/2/media/upload` (v2), because v1.1 `upload.twitter.com` does not accept
bearer tokens. The old key path keeps using v1.1 unchanged.

## Security model

- Tokens live in the `credentials` DB table (or env), never in git.
- OAuth round-trips are CSRF-protected (single-use `state`).
- The whole admin UI sits behind HTTP Basic auth once `ADMIN_PASSWORD` is set.
- Rotating a token = clicking the connect button again; nothing to redeploy.
- Keep `DRY_RUN=true` until proposed posts have been reviewed; flipping a
  channel live is a separate, deliberate step.
