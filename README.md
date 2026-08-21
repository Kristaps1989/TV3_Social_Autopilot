# TV3 Social Autopilot

Automated, AI-driven social publishing for **tv3.lv**. Replaces SocialFlow.
Editors only set one checkbox in WordPress — everything else (whether, when,
where, in which format and with what copy to post) is decided here, with the
single goal of maximum clicks to tv3.lv and a healthy, diverse feed.

## How it works

```
WP feeds → Ingestor → Rule engine (YAML, always wins) → AI decision (Claude)
        → Slot allocator → Queue → FB / X / Threads adapters → Analytics
```

- **Editors** keep two real controls: `now` (ASAP) and `dont` (never).
  `must` is a soft guarantee (published within 6 h), `can` means the AI decides.
- **Rule engine** (deterministic, `rules/rules.yaml`): freshness limits,
  sensitive-content night windows, per-channel routing, blocklists.
- **AI layer** writes platform-native Latvian copy and picks format + slot.
  Every decision is logged with its reason. No API key? A safe deterministic
  fallback keeps `must`/`now` flowing.
- **Best practice is enforced in code** (`app/best_practices.py`), not just
  prompted: char limits (X counts links as 23), hashtag caps per platform,
  max 2 emoji, sober tone on tragedy/crime, no clickbait phrases, no shouting
  caps, UTM tags on every link, posting-time optimization, min gaps, daily
  caps, quiet hours, and section/format diversity in every rolling window.
- **Admin UI** (no build step, server-rendered): queue per channel with
  cancel / publish-now / edit-copy, kill switch, per-channel pause,
  **"Kāpēc nav publicēts?"** lookup, and in-browser editing of all rules
  and AI style guides (hot-reloaded, YAML-validated).

## Run it

```bash
cp .env.example .env        # fill in what you have; empty is fine for dry run
docker compose up --build   # app on http://localhost:8000, Postgres included
```

Local development without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload   # uses SQLite in ./data/
pytest                          # rule engine, slots, copy rules, e2e dry run
```

**Dry run is the default** (`DRY_RUN=true`): the full pipeline runs — ingest,
decisions, scheduling, "publishing" — but nothing reaches a real platform.
Flip `DRY_RUN=false` only after reviewing proposed posts for a week (Phase 2).

## Configuration = the product

| File | What it controls |
|---|---|
| `rules/feeds.yaml` | WP feed URLs, term-ID → section mapping |
| `rules/channels.yaml` | Channels, cadence (min gap, daily cap, quiet hours), formats |
| `rules/rules.yaml` | Editorial rules: freshness, diversity, sensitivity windows |
| `prompts/system_*.md` | AI style guides per platform (Latvian, editable by editors) |

All of these are editable in the admin UI (`/settings`) with validation and
take effect immediately — no deploy, no restart.

## Connecting accounts

`/connect` in the admin UI guides account setup: Facebook Page and Threads
connect with an OAuth button (needs `META_APP_ID`/`META_APP_SECRET` — see
`docs/connect-accounts.md`), X via env vars. Tokens are stored in the DB,
Threads tokens auto-refresh, and expiry warnings go to the log/Slack.
Set `ADMIN_PASSWORD` before connecting anything on a public deployment —
it puts the whole admin UI behind HTTP Basic auth (user `admin`).

## Status / roadmap

- [x] Phase 1 — ingest, rules, AI decisions, scheduler, admin UI, dry run
- [ ] Phase 0 checks — confirm live feed schema (`docs/feed-format.md`),
      platform tokens (`docs/platform-limits.md`)
- [ ] Phase 2 — flip channels live one by one (Threads → X → FB)
- [ ] Phase 3 — GA4 + insights feedback loop, weekly report
- [ ] Phase 4 — WP meta box simplification, Instagram, native video

See `docs/` for the feed contract, platform limits and the best-practice
playbook behind the copy rules.
