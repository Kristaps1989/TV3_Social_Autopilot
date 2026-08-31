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
7. **Video:** tv3.lv/video turētie 9:16 klipi — vai rakstiem feed'ā var dot līdzi
   video URL (tiešs mp4 vai HLS .m3u8)? Ingestors jau saprot laukus
   `video | video_url | videoUrl | video_src | video_file | mp4` (string vai
   objekts ar `url`/`src`); tiklīdz lauks parādās, autopilots no klipa
   automātiski būvē īstus Reels (apgriež līdz 45 s, normalizē 1080×1920,
   pieliek CTA beigu kadru "lasi tv3.lv").

Once confirmed, record the real schema here and tighten the parser.

## Raksta lapas metadati (`app/pagemeta.py`)

Daļu no tā, kā feed'ā trūkst, raksts pats nes savā lapā: tv3.lv GTM slānis
katram rakstam iepush'o `dlEvent` objektu ar CMS metadatiem.

```js
var dlEvent = {"Title":"…","Publish date":"2026-08-31","Post ID":3879950,
  "Page type":"Post","Content length":11587,"Post type":"Video;Gallery",
  "Editor name":"Gundega Gaujere","Source":"tv3.lv","Category level 1":"Ziņas",
  "Tags":"Bauskas iela;Gāzes sprādziens;Rīga","Label":"Tikai tv3.lv",
  "Secondary category":"Sabiedrība","event":"Pageview"};
```

Ko no tā izmantojam:

| Lauks | Kur aiziet |
| --- | --- |
| `Post ID` | tv3.lv pašu īsā saite `tv3.lv/p/<id>` (rules: `cms_short_links`) |
| `Editor name` | autors rakstu sarakstā un priekšskatījumā, AI promptā |
| `Tags` | hashtagi, kad AI savus nedod (redakcijas atslēgvārdi ir precīzāki) |
| `Post type` | `Video`/`Gallery` — vai reel/karuselis vispār ir uz galda |
| `Label` | "Tikai tv3.lv" = ekskluzīvs saturs, ko ir vērts izcelt |
| `Content length` | īsziņa pret garu lasāmgabalu (formāta izvēle) |
| `Category level N` | CMS kategoriju koks AI kontekstam |
| raksta rindkopas | AI kartīšu fakti un reela ierunas teksts |

Lapa tiek ievilkta **vienu reizi uz rakstu** un nokešota
(`raw_json["_page_meta"]`); neveiksme tiek atkārtota ne biežāk kā reizi
6 stundās. Ielases ciklā papildus tiek papildināti līdz 10 vecāki raksti, jo
franšīzes (QUIZ, ICYMI, evergreen) strādā tieši ar tiem. Izslēdz ar
`page_meta: false`.

Neapstiprināts pieņēmums: vai `tv3.lv/p/<id>` novirze **saglabā vaicājuma
virkni** (UTM birkas). Kamēr tas nav pārbaudīts ar roku, `cms_short_links`
ir izslēgts un ierakstos iet pilnā saite; gatavā īsā saite ir redzama raksta
un ieraksta skatā, lai to varētu pārbaudīt ar vienu klikšķi.

### Raksta teksts un ieruna

Metadatiem līdzi no tās pašas lapas tiek izvilkts arī **raksta teksts** —
vispirms schema.org JSON-LD `articleBody`, tad raksta konteinera `<p>` tagi,
un tikai galējā gadījumā visas lapas rindkopas. Ārā filtrējas izvēlne,
kājene, foto paraksti un "Lasi arī" bloki; paturam pirmās rindkopas līdz
3000 zīmēm (ziņu rakstā būtiskais tāpat ir sākumā), jo tas ir darba
materiāls, nevis raksta kopija.

Ko tas maina:

1. **Kartīšu punkti un reela punkti** vairs netop no virsraksta ar ievadu.
   Modelim promptā aiziet pirmās ~1500 zīmes raksta, tāpēc punktos var būt
   skaitļi un detaļas, kas līdz šim vienkārši nebija pieejamas.
2. **`voice_script`** — jauns lauks lēmumu shēmā: reelam AI uzraksta 45-90
   vārdu ierunas tekstu latviski, rakstītu runāšanai (īsi teikumi, bez
   iekavām, saīsinājumiem un URL). Tas glabājas reela receptē un ir redzams
   ieraksta priekšskatījumā, lai redaktors to var izlasīt pirms publicēšanas.
3. **Reels prot skaņas celiņu.** `reels.build_reel(..., voice=fails)` ieliek
   ierunu klusuma celiņa vietā un izstiepj kadrus līdz runas garumam
   (proporcionāli, lai CTA kadrs nestāvētu viens pats), maksimāli 60 s.

Kas vēl **nav** izdarīts: pati balss sintēze. `voice_script` ir teksts;
lai no tā taptu audio fails, vajag TTS pakalpojumu ar latviešu balsi
(piemēram, Azure Speech `lv-LV-EveritaNeural`/`lv-LV-NilsNeural` vai
ElevenLabs multilingual). Kamēr tā nav, reels iznāk kluss tieši tāpat kā līdz
šim — teksts vienkārši gaida receptē.
