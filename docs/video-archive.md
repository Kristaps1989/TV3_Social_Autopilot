# tv3.lv/video arhīvs sociālajos tīklos

Portāla `tv3.lv/video` ir vertikālo klipu arhīvs — TV3 iekšējā TikTok
versija. Katrs klips ir sava lapa `tv3.lv/video/<id>/`, daļa klipu ir
piesaistīti rakstiem. Šis dokuments ir plāns un ieviestā uzvedība.

## Kāpēc tas ir vērtīgs

- **Īsts video sit slideshow.** Facebook, Instagram un Threads lentes no
  īsta 9:16 klipa iegūst vairāk noskatījumu un dalīšanās nekā mūsu
  kartīšu slideshow ar ierunu. Slideshow paliek tiem rakstiem, kam klipa nav.
- **Saite uz video lapu, ne uz rakstu.** Klikšķis no reel ved uz
  `tv3.lv/video/<id>/`, kur skatītājs paliek video plūsmā (nākamais klips,
  reklāmas inventārs). UTM paliek tas pats (`utm_source`, `utm_content` =
  ieraksts, `utm_term`), tāpēc GA4 rāda, cik sesiju video sadaļa saņem no
  sociālajiem tīkliem un no reklāmām.
- **Stāsti ar video.** Stāstu kanāls klipu nes kā video story (līdz 30 s),
  kas ir dabiskākais story saturs.
- **Papildu plūsma, ne konkurents.** Klipi bez raksta aizpilda tukšumus
  (vakari, nedēļas nogales, klusas stundas starp ziņām), bet neaizņem ziņu
  vietu: garš pusperiods rindas prioritātē, savs dienas limits kanālā.

## Divi ceļi

| Situācija | Kas notiek | Saite |
|---|---|---|
| Raksts, kura lapā ir video (JSON-LD `video`, og:video vai `/video/<id>/` satura blokā) | `pagemeta.enrich` piesaista klipu (`_video_page`, `_video_url`); reel un story top no īstā klipa (`reels.build_video_reel`) | reel/story → video lapa (`link_reels_to_video`); link/photo/karuselis → raksts |
| Klips arhīvā bez raksta | `videos.crawl` (ik 30 min) izveido rakstu rindu `guid=video:<id>` ar `raw_json._video`; AI lemj kā par rakstu, formāts tikai reel vai story | vienmēr video lapa |

Klips, ko kāds raksts jau nes, atsevišķu rindu neveido (`covering_article`).

## Sargi

- `daily_cap` (3) klipiem bez raksta uz kanālu dienā (Rīgas diena).
- `min_seconds`/`max_seconds` (5–180 s); reel griež līdz 45 s, story līdz 30 s.
- `half_life_hours` (48) rindas prioritātē un `max_age_hours` (72) svaigumā:
  klips nav ziņa, tas var pagaidīt, bet ne nedēļu.
- Kanālā bez reel/story formāta klips tiek bloķēts ar skaidru iemeslu
  («video arhīvs: kanālā nav reel/story formāta»).
- Sadaļa nāk no dlEvent kategorijas (`category_sections`), tāpēc kanālu
  `sections:` maršrutēšana strādā kā rakstiem.

## Portāla API (apstiprināts 03.09.2026 ar Diagnostikas zondi)

`tv3.lv/video` ir React aplikācijas čaula (3 KB, vienāda katram maršrutam);
klipi nāk no API, kura adreses bija JS pakotnē:

| Adrese | Kas |
|---|---|
| `https://tv3.lv/api/1/video/feed/?page=N` | klipu saraksts, 20 lapā, `meta.has_more` |
| `https://tv3.lv/api/1/video/menu/` | kategorijas (atdod HTML bez JSON galvenes; nelietojam) |
| `https://tv3.lv/api/5/tv3/video/<id>` | video lietotnes backend, viens klips (rezerve) |

Feed ieraksts:

```json
{"id":196425621,"title":"…","description":"…",
 "image":"https://tv3cdn.lv/video/thumb/196425621/70274191/poster.jpg",
 "share":{"image":"…/share.jpg"},
 "video_url":"https://tv3cdn.lv/video/hls/196425621/70274191.m3u8",
 "tags":[],"duration_ms":31640,
 "related_url":"https://tv3.lv/zinas/arvalstis/…/",
 "content_source":{"name":"tv3.lv","link":"https://www.tv3.lv/"},
 "created_at":"2026-08-29T17:52:35+00:00"}
```

Ko no tā izmantojam:

- `video_url` (HLS) — ffmpeg to lasa tieši, reel/story top no īstā klipa;
- `related_url` — raksts, pie kura klips pieder: piesaiste notiek no feed
  puses (`link_feed_articles`), ne lasot raksta lapu; ja raksts ienāk vēlāk,
  nākamais apgājiens to piesaista un atsevišķo klipa rindu atzīmē kā pārņemtu;
- sadaļa: raksta adreses ceļš (`/zinas/`, `/sports/`) → `content_source.name`
  (`source_sections`: Bez Tabu → izklaide, 900 sekundes → ziņas) → noklusējums;
- `image` (poster) rindas attēlam, `share.image` paliek `_video_share_image`;
- `duration_ms` → sekundes; `created_at` → publicēšanas laiks.

Video lapa `tv3.lv/video/<id>/` ir tikai saites mērķis; tās HTML parsēšana
paliek kā rezerve, ja API pazūd.

## Kas nav darīts apzināti

- Klipi netiek lejupielādēti vai pārkodēti krājumā: reel top publicēšanas
  brīdī no klipa adreses (kā līdz šim ar feed video).
- Reklāmām atsevišķa video kampaņa nav: reel ar saiti uz video lapu iet
  cauri tam pašam boost mehānismam, mērķis paliek portāla sesijas.
