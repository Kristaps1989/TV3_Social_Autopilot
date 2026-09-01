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

### Balss (`app/tts.py`)

Ierunas tekstu nolasa **Azure Speech** ar latviešu neironu balsi
(`lv-LV-EveritaNeural` sievietes, `lv-LV-NilsNeural` vīrieša; izvēle
noteikumos `reel_voice_name`). Teksts aiziet kā SSML: teikumu robežas kļūst
par 260 ms pauzēm, temps ir par 4% lēnāks nekā noklusējums, jo lentē
skatītājs vienlaikus lasa arī kadra tekstu.

Atslēgu pievieno **Konti → Reelu balss**. Saglabājot tiek ierunāts parauga
teikums (apejot kešu, ar tieši šo atslēgu), tāpēc nepareizs reģions vai
atslēga parādās uzreiz, nevis pēc nedēļas klusiem reeliem.

### Izruna

Latviski punkts aiz cipara nozīmē kārtas skaitli, tāpēc Azure normalizētājs
`tv3.lv` nolasīja kā «tv **trešais** punkts lv». Domēnā tas ir tikai punkts.
Ne prompts, ne balss izvēle to nelabo — tekstu modelim jāpasniedz tā, kā to
jāizrunā, tāpēc `tts.spoken_text()` pirms SSML pielieto izrunas vārdnīcu:

```yaml
tts_pronunciation:
  "tv3.lv": "tv trīs punkts lv"
  "tv3 play": "tv trīs pleij"
  "tv3": "tv trīs"
```

Papildināt var Noteikumos, bez deploy. Garākie ieraksti tiek aizstāti
vispirms, lai `tv3.lv` netiktu sadalīts pa `tv3`. **Rakstiskais scenārijs
paliek neskarts** — priekšskatījumā redaktors redz `tv3.lv`, nevis fonētisko
pierakstu; mainās tikai tas, ko balss nolasa.

Tieši šī vieta ir arī galvenais arguments par labu Tildei (skat. zemāk):
tur izrunas vārdnīca ir pakalpojuma daļa, nevis mūsu pašu uzturēts saraksts.

Viena un tā paša teksta ieruna tiek kešota pēc `sha256(balss + IZRUNĀTAIS
teksts)` — pielabojot vārdnīcu, vecais ieraksts atkrīt pats. Tāpēc
reela pārzīmēšana par to pašu skaņu Azure otrreiz nemaksā. Bez atslēgas, ar
`reel_voice: false`, vai ja Azure neatbild, reels iznāk kluss tieši tāpat kā
līdz šim — teksts glabājas receptē un ir redzams priekšskatījumā.

#### Pakalpojuma maiņa (Tilde)

`tts_provider` noteikumos izvēlas pakalpojumu; `_SYNTHS` vārdnīcā katrs ir
viena funkcija `(teksts, balss, sesija) -> audio baiti`. Kešs, SSML
sagatavošana, atslēgas pārbaude un kļūdu apstrāde ir kopīga, tāpēc jauna
pakalpojuma pievienošana ir viena funkcija plus viens ieraksts. Nepazīstams
`tts_provider` nozīmē klusu reelu, nevis kļūdu.

Nopietnākais kandidāts Azure vietā ir **Tilde** (tilde.ai): latviešu balsis,
kas taisītas latviešu valodai, **pielāgojamas izrunas vārdnīcas** (tieši tur
Azure klūp — īpašvārdi lokatīvā, "Bauskas ielā", un saīsinājumi), izvietošana
ES mākonī vai uz vietas, un iespēja licencēt savu balsi. Cena un API līgums
nāk caur sarunu ar pārdošanu, nevis no publiskas dokumentācijas, tāpēc
adapters nav uzrakstīts. Ko noskaidrot sarunā:

1. REST galapunkts, autentifikācija un audio formāti (mums der mp3 vai wav).
2. Vai ir SSML vai cits veids, kā uzlikt pauzes un tempu.
3. Kā papildina izrunas vārdnīcu (tas ir galvenais iemesls izvēlēties Tildi):
   pašapkalpošanās vai caur atbalstu?
4. Cenas modelis: par zīmi, par pieprasījumu vai abonements. Mūsu apjoms ir
   ~600 zīmes uz reelu, daži reeli dienā.
5. Latency reāllaika izsaukumam (mums der līdz ~30 s, reels top fonā).
6. Vai balsi drīkst izmantot publicētā sociālo tīklu saturā (licences apjoms).

## Kad kāds formāts neparādās (reeli, karuseļi)

Reels un card_carousel iet pa citu ceļu nekā parastie formāti, un tāpēc tie
"pazūd" klusi. Ķēde, kurai visai jāsakrīt:

1. **Kanāla `formats` sarakstā formāts ir.** Bez tā `resolve_format` AI
   ierosinājumu klusi nomet un atgriežas pie saites ieraksta.
2. **AI to ierosina.** Diversitātes dzinējs reelus un karuseļus NEKAD
   neizvēlas (`formats.suitable_formats` tos izlaiž) — tiem vajag
   `card_points`, ko var uzrakstīt tikai modelis.
3. **AI to grib.** Promptā ir teikts taupīt (~2 reeli dienā kanālā) un
   ikdienas ziņām dot priekšroku saites ierakstam.
4. **Renderētājs strādā.** `reels.available()` = ffmpeg + Chromium.
   Konteinerā abi ir; bez tiem formāts atkal klusi atkāpjas.
5. **Rakstā ir vismaz 2 spēcīgi punkti.**

Visbiežākais iemesls praksē ir **pirmais**, un tas ir viltīgs:
`rules/channels.yaml` ir repo noklusējums, bet lietotne lasa rediģējamo
kopiju `data/rules/channels.yaml` (vai `RULES_DIR`). Tā tiek uzsēta **vienu
reizi** un pēc tam vairs netiek aiztikta — tas pasargā redaktora labojumus,
bet nozīmē, ka kodā vēlāk pievienots formāts vai kanāls uz jau strādājošas
instances neparādās nekad.

Tāpēc `config.missing_channels()` un `manual.unavailable()` šo novirzi
tagad parāda raksta skatā: "neviens aktīvs kanāls nepieņem formātu reel" ar
norādi uz Noteikumiem, kur to izlabot.

## Rokas vadība (`app/manual.py`)

Raksta skatā (`/why`) redaktors var pieprasīt konkrētu formātu konkrētam
rakstam, negaidot, kad AI tam piekritīs. Ieraksts iet caur to pašu ceļu, ko
automātiskais: tā pati grafika (`resolve_format` / `format_media`), tā pati
ieruna un tas pats laika plānotājs. Atšķiras tikai tas, kurš izvēlējās
formātu — un ieraksts tiek atzīmēts ar `extra["manual"] = True`.

Kanāla atstarpes un klusās stundas paliek spēkā arī rokas režīmā: tās sargā
kontu, nevis ierobežo redaktoru, tāpēc "tūlīt" nozīmē "nākamajā derīgajā
logā". Kartīšu punkti reelam un karuselim nāk no **raksta teksta** (skat.
`pagemeta`), nevis no virsraksta.

## Lente stāstā (video stāsti)

Stāsts un reels ir viens un tas pats formāts — 9:16, 1080×1920 — un
publicētājs video stāstus jau prot (`/video_stories` Facebook adapterī).
Trūka tikai tā, kas video stāstam uztaisa video: līdz šim tas radās vienīgi
tad, kad plūsmā bija īsts raksta klips, un tāda lauka plūsmā vēl nav. Praksē
tas nozīmēja, ka **katrs stāsts bija statisks attēls**.

Tagad `story_media` pēc kārtas mēģina:

1. īsto raksta klipu (`reels.article_video`) — vislabākais materiāls;
2. **šī paša raksta jau uzbūvēto lenti** (`article_reel_file`) — viens fails,
   divas vietas, nulle papildu renderēšanas;
3. brendēto stāsta attēlu;
4. neapstrādāto raksta attēlu.

Kāpēc tieši stāstos: **stāstus skatās ar skaņu**, plūsmā lentes bieži sākas
klusas. Tāpēc, ja rakstam ir gan ierunāta, gan klusa lente, stāstā tiek
likta ierunātā — ieruna tur tiešām tiek dzirdēta. Griesti ir 60 s (Facebook
video stāsta limits); garāku lenti stāstā nemēģinām likt.

`order_channels` liek reelu un karuseļu kanālus lēmumu ciklā pirmos — stāsts
lenti var pārizmantot tikai tad, ja tā jau eksistē. Bez tā video stāsts
sanāktu atkarībā no tā, kādā secībā AI kanālus uzskaitīja.

Izslēdz ar `story_reuses_reel: false`.

**Ko atcerēties, vērtējot rezultātu:** stāstā saite caur API nav klikšķināma
(`click_weight: 0.3` kanāla konfigurācijā). Tāpēc statistikā «Stāsti: video
pret attēlu» rezultāts gandrīz vienmēr nozīmē sasniegumu, nevis klikšķus uz
tv3.lv. Video stāsts var uzvarēt pēc noskatīšanās un tomēr nedot vairāk
apmeklējumu — ja mērķis ir klikšķi, izšķirošais paliek saites ieraksts.

## Sadaļu kartītes (stāsts pa daļām)

Kvalitātes latiņa ir tas, ko dara labākie ziņu konti: karuselis, kur katra
kartīte ir stāsta SADAĻA — pilns foto fonā (katrai kartītei savs no raksta
galerijas), pa vidu puscaurspīdīgs balts panelis ar treknu virsrakstu un
2-4 teikumiem faktu, sarkanas ">>>" švīkošanas bultas, un pēdējā sadaļa ar
praktisko daļu (kur zvanīt, ko darīt), ja rakstā tāda ir.

- **Saturs**: lēmumu shēmā `card_sections` = [{title, body}] — raksts,
  sadalīts sadaļās, balstoties raksta TEKSTĀ (pagemeta). `card_points`
  paliek kā rezerve, kad teksta nav.
- **Karuselis**: `cards.build_section_cards_html` / `render_section_cards` —
  vāks (līdzšinējā stilā + bultas) → sadaļu kartītes → CTA kartīte.
- **Lente**: tie paši sadaļu kadri vertikāli (`_section_frame_html`,
  6 s kadrā), un bez atsevišķa scenārija balss nolasa tieši kadros rakstīto
  (title + body). Ar ierunu kadri stiepjas līdz runas garumam.
- **Pārzīmēšana**: recepte glabā `sections`; vecās receptes ar `points`
  zīmējas pa vecam.
- **Rokas vadība**: /why «Uztaisīt formātu ar roku» tagad prasa AI sadaļas
  (Virsraksts | Teksts pa rindai), ne punktus.

## Ko vēl dod raksta lapa (pārbaudīts pret īstu tv3.lv rakstu)

Raksta DOM apskate atklāja, ka lapā ir daudz vairāk, nekā ņēmām — un vienu
īstu kļūdu.

**Kļūda:** raksta teksts tv3.lv dzīvo `<section class="tv3-single-content">`.
Mūsu konteinera regex to nepazina (meklēja tikai `article-content` u.tml. un
tikai `<div>`/`<article>`), tāpēc krita atpakaļ uz "visi lapas `<p>`" — un
tas ievilka sānjoslu **"Tevi varētu interesēt"**. AI kartītēs tad varēja
nonākt fakti no PAVISAM CITA raksta. Tagad konteiners tiek atrasts, un
ievads (`<p class="lead">`, kas ir ārpus konteinera) tiek pielikts priekšā.

**Jaunie metadati** (`<meta>` tagi ir noturīgāki par dataLayer — ja tas
mainīsies, šie, visticamāk, paliks):

| Tags | Kas tas ir | Kur aiziet |
| --- | --- | --- |
| `article:tag` (vairāki) | redakcijas atslēgvārdi | hashtagi, AI konteksts |
| `article:section` (vairāki) | sadaļu koks | AI konteksts |
| `cXenseParse:zfv-articleId` | Post ID | īsā saite `tv3.lv/p/<id>` |
| `cXenseParse:zfv-articleDisplayCategory` | kur raksts portālā tiešām rādās | AI promptā |
| `dr:say:img` / `twitter:image` | raksta foto **bez** iecepta virsraksta | vāki un sadaļu kartītes |
| `cXenseParse:zfv-featuredFrontPage(+Position)` | vai izcelts sākumlapā un kurā vietā | AI promptā kā redakcijas svarīguma signāls |
| `og:description` | ievads | rezerve, kad plūsmā ievada nav |

`clean_image` ir īpaši vērtīgs: plūsmā bieži ir TIKAI photopost grafika ar
iecepto virsrakstu, un vāks, kas zīmē savu virsrakstu, tad palika bez foto.
Tagad `unbranded_image()` un `section_backgrounds()` pirms padošanās
pameklē lapas metadatos.

`featuredFrontPage` ir redakcijas pašas vērtējums — pozīcija 0 nozīmē
galveno stāstu. Tas aiziet AI promptā kā konteksts, nevis kā automātisks
reitinga pacēlums: lēmumu joprojām pieņem modelis kopā ar pārējo.
