# The best-practice playbook (what the system enforces and why)

Goal: maximum referral clicks to tv3.lv per post, without burning reach or
trust. Editors control *what* may be posted (WP status); everything below is
this tool's job.

## Copy (enforced in `app/best_practices.py`, guided in `prompts/`)

| Practice | How it's applied |
|---|---|
| Hook first, don't repeat the headline verbatim | AI prompt; fallback uses headline + lead |
| Leave a reason to click, never lie | Prompt + clickbait phrase filter in code |
| No "You won't believe…", "ŠOKS", `!!`, ALL-CAPS shouting | Regex filter, auto-removed |
| Max one question per post | Auto-fixed in code |
| ≤2 emoji, matching tone; **zero** on tragedy/crime | Auto-fixed; hashtags also stripped on sober topics |
| Platform length: X 280 (URL=23), Threads 500, FB ~400 (125 visible) | Truncated at word boundary with budget for the link |
| Hashtags: X ≤2 topical, FB ≤1, Threads ≤1 | Trimmed in code |
| Latvian with correct diacritics | Prompt (style guides are editor-editable) |

## Timing & cadence (rule engine + slot allocator)

- Peak windows preferred: 7–9, 12–14, 19–22 local (default curve, replaced by
  measured priors after 2 weeks of data).
- Min gap per channel (FB 45', X 15', Threads 30') — flooding kills reach.
- Daily caps and quiet hours per channel.
- News older than 12 h (sport 8 h, entertainment 48 h) is not posted — stale
  news gets no clicks and hurts credibility. Evergreen is exempt.
- Sensitive content (nudity/party/alcohol) only 22:00–06:00 unless editor
  said `now`.

## Feed diversity (rule engine)

- In any rolling window of 6 posts per channel: ≥2 sections and ≥2 formats.
- No 2 near-duplicate stories in the last 3 posts (similarity guard).
- Never the same article twice on the same channel.

## Format → clicks

- Link post = best CTR to site → default for news/politics/sport.
- Photo album only for real galleries (≥4 images); photo post for
  entertainment/visual stories.
- Instagram is reach, not clicks (no links in captions) — lowest weight for
  the pageview KPI, optional in phase 1.

## Measurement

- Every link carries `utm_source/{platform}, utm_medium=social,
  utm_campaign=autopilot, utm_content={post_id}` → GA4 sessions per post.
- Phase 3 replaces all manual weights with measured sessions-per-post by
  channel × format × section × hour.
