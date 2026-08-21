# WP social feed contract (Phase 0 — to verify against live)

URL pattern: `https://www.tv3.lv/api/1/social/{termIds}/{status}/{timeframe}/`

The live endpoints could not be fetched from the build sandbox (network is
restricted), so the ingestor (`app/ingest.py`) is deliberately tolerant:

- **JSON** — a top-level list, or `{items|posts|articles: [...]}`. Field
  lookup tries several names per field (`title|headline`, `url|link|permalink`,
  `lead|excerpt|description|summary`, `image|featured_image|thumbnail|og_image`
  or `images[]`, `term_ids|terms|categories_ids`, `status`, `timeframe`,
  `published_at|date|pubDate`).
- **RSS/Atom** — parsed with feedparser; `media:content` / enclosures become
  images; per-item status is not expected in RSS.
- If items carry no per-item `status`, the **status segment of the feed URL**
  is applied to every item (e.g. a `/must/` feed ⇒ all items `must`).

## To confirm with Artis (checklist)

1. Response format: JSON or RSS? Exact field names → adjust `normalize_json_item`.
2. Are `status`/`timeframe` included per item, and are the URL segments exact
   filters or "at least"?
3. The non-standard path `/26336/60119,.../must/evergreen/` — how does the API
   parse it? (Currently treated as an opaque URL; only the `must` segment is used.)
4. Images: featured only or full gallery? Gallery is needed for photo albums.
5. Does an article whose status changes to `dont` still appear in feeds (so we
   can cancel scheduled posts), or does it vanish? If it vanishes, we need the
   planned `/all/` endpoint (Phase 4) or a per-article status check.
6. Full term-ID → section mapping for `rules/feeds.yaml` (`term_sections`).

Once confirmed, record the real schema here and tighten the parser.
