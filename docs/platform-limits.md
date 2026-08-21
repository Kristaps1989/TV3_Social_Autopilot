# Platform access & limits (Phase 0 — to confirm)

| Platform | Access needed | Notes |
|---|---|---|
| Facebook Page | System User token via Business Manager (long-lived, no re-auth). Permissions: `pages_manage_posts`, `pages_read_engagement` | Who owns the Business Manager? Which Page ID? |
| Threads | Threads API token for the account | Container→publish two-step; rate limit 250 posts/24 h |
| X | API v2, **paid tier** | Basic tier: ~3k posts/month, $200/mo (verify current pricing). Adapter queues + retries on 429 |
| Instagram | Graph API (optional, Phase 4) | Links don't work in captions — low click value |
| GA4 | Service account with Data API read access to the tv3.lv property | For sessions/pageviews by `utm_content` |

Enforced in code (`app/best_practices.py` PLATFORM_SPECS):

- X: 280 chars, every URL counts as 23; ≤2 hashtags.
- Threads: 500 chars; ≤1 hashtag.
- FB: no practical hard limit, but copy is capped at 400 chars (engagement
  drops sharply past ~125 visible chars); ≤1 hashtag.
- All: ≤2 emoji; zero emoji/exclamation on tragedy or crime content.

## Google News / Discover

Not a posting channel — coverage is on-site (news sitemap, `NewsArticle`
structured data, Publisher Center, ≥1200 px images, `max-image-preview:large`).
A site health checker job that validates sitemap + structured data is planned
alongside Phase 3; it alerts to Slack on failure.
