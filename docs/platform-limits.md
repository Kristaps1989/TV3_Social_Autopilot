# Platform access & limits (Phase 0 — to confirm)

| Platform | Access needed | Notes |
|---|---|---|
| Facebook Page | System User token via Business Manager (long-lived, no re-auth). Permissions: `pages_manage_posts`, `pages_read_engagement` | Who owns the Business Manager? Which Page ID? |
| Threads | Threads API token for the account (`threads_basic`, `threads_content_publish`) | Container→publish two-step; TEXT+link, IMAGE, VIDEO, CAROUSEL (≤20) and reply supported by the adapter; rate limit 250 posts/24 h |
| X | API v2, **paid tier** (v1.1 media upload for images and chunked video) | Basic tier: ~3k posts/month, $200/mo (verify current pricing). Adapter: ≤4 images per tweet, chunked video with processing poll, reply for link-in-reply. Queues + retries on 429 |
| Instagram | Graph API via the linked FB Page (`instagram_basic`, `instagram_content_publish`) | Links don't work in captions — the system posts the link as the first comment and appends a pointer to the caption; reels, carousels, photos and stories supported |
| GA4 | Service account with Data API read access to the tv3.lv property | For sessions/pageviews by `utm_content` |

Enforced in code (`app/best_practices.py` PLATFORM_SPECS):

- X: 280 chars, every URL counts as 23; ≤2 hashtags.
- Threads: 500 chars; ≤1 hashtag. Link stays in the text (`threads_link_in_reply` moves it to a reply).
- Instagram: no link in caption; link goes to the first comment + `ig_link_pointer` in the caption; 3–5 hashtags.
- FB: no practical hard limit, but copy is capped at 400 chars (engagement
  drops sharply past ~125 visible chars); ≤1 hashtag.
- All: ≤2 emoji; zero emoji/exclamation on tragedy or crime content.

## Google News / Discover

Not a posting channel — coverage is on-site (news sitemap, `NewsArticle`
structured data, Publisher Center, ≥1200 px images, `max-image-preview:large`).
A site health checker job that validates sitemap + structured data is planned
alongside Phase 3; it alerts to Slack on failure.
