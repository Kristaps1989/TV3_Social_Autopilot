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

## Lapas struktūra: kas jāpārbauda dzīvajā portālā

Būves vidē tv3.lv nav sasniedzams (tīkla politika), tāpēc parsētājs lasa
vairākus signālus un Diagnostikas lapā ir **zonde** (`/logs/video-probe?url=…`):

1. `https://tv3.lv/video/` — jāredz klipu saraksts (`videos`). Ja tukšs,
   saraksts tiek zīmēts ar JavaScript, un vajag `video_archive.feed` (CMS
   JSON/RSS ar video ierakstiem) vai citu saraksta adresi.
2. `https://tv3.lv/video/<id>/` — jāredz `clip` (mp4 vai m3u8). Ja tukšs,
   klipu dod atskaņotājs pēc pieprasījuma; tad CMS jāpadod `contentUrl`
   VideoObject shēmā vai feed laukā `video_url`.
3. Raksts ar video — `video_page` un `video_clip`.

Nokopē zondes JSON izstrādātājam, un parsētāju var pielāgot bez minēšanas.

## Kas nav darīts apzināti

- Klipi netiek lejupielādēti vai pārkodēti krājumā: reel top publicēšanas
  brīdī no klipa adreses (kā līdz šim ar feed video).
- Reklāmām atsevišķa video kampaņa nav: reel ar saiti uz video lapu iet
  cauri tam pašam boost mehānismam, mērķis paliek portāla sesijas.
