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

## Piekļūstamība

- Katram attēla ierakstam aiziet **alt teksts** (FB `alt_text_custom`, IG
  `alt_text`, X `media/metadata/create`). Mūsu grafikas nes virsrakstu, tāpēc
  apraksts ir tas, kas uz tās TIEŠĀM rakstīts — ekrānlasītāja lietotājs dabū
  to pašu, ko redzīgais. Threads API to pagaidām nepieņem.
- Reelu kadros teksts ir SAFE_INSET drošajā zonā (64 px), jo Ken Burns
  tuvinājums apgriež malas; teksta izmērs krītas garam saturam (`fit_size`,
  `body_fit`), lai neviens vārds netiktu nogriezts.

## Karuseļi, lentes un stāsti

| Prakse | Kā piemērots |
|---|---|
| Katra kartīte ir sadaļa: virsraksts + 2-4 teikumi | `card_sections` no raksta TEKSTA, ne virsraksta |
| Katrai kartītei savs foto | raksta galerija pēc kārtas; photopost grafikas izslēgtas |
| Pirmā kartīte āķē, pēdējā ir CTA | vāks + CTA kartīte `build_section_cards_html` |
| Katra kartīte klikšķināma uz rakstu | `card_links` + savs `utm_term` katrai (`quiz-karte2`) |
| Švīkošanas norāde | sarkanās ">>>" bultas |
| Lente 9:16, ≤60 s | 1080×1920; ieruna stiepj kadrus, `VOICE_MAX_SECONDS` |
| Kadrā tik teksta, cik paspēj izlasīt | sadaļas kadrs 5.5 s (punkts 2.8 s) |
| Ieruna latviski, rakstīta RUNĀŠANAI | `voice_script`; izrunas vārdnīca (`tv3.lv` → «tv trīs punkts lv») |
| Stāsts = tā pati lente, ne statisks attēls | `story_reuses_reel`; stāstos skaņa tiešām skan |

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
- A/B dimensijas, ko mēra atsevišķi: āķa stils (`utm_term`), **ieruna pret
  klusu lenti** un **video stāsts pret attēlu** (Statistikas lapā). Visur
  parādīts kā virziena rādītājs ar ierakstu skaitu — ne kā pierādījums.

## Kas apzināti NAV izdarīts

- **Ierunas subtitri.** Kad ierunu ģenerē no sadaļām, runātie vārdi JAU ir
  uz kadra — tā ir dabiska paraksta forma. Bet, ja AI uzraksta atsevišķu
  `voice_script`, teksts un balss atšķiras, un skaņu izslēgušais skatītājs
  runāto nedabū. Īsti subtitri (burn-in vai SRT) vēl nav.
- **Threads alt teksts** — API to nepieņem.
- **Instagram kanāls** izslēgts (`active: false`), līdz konts ir sasaistīts.
