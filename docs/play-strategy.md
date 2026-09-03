# TV3 Play (play.tv3.lv) sociālajos tīklos — veiktspējas mārketinga plāns

Mērķis: portāla tv3.lv sociālo tīklu auditoriju (tie paši sekotāji, kas
klikšķina ziņas) pārvērst Play skatītājos, nezaudējot ziņu plūsmas uzticamību.
Play ir AVOD: saturs bez maksas ar reklāmām, tāpēc katrs skatījums ir
ieņēmums, un aicinājums vienmēr var būt «skaties bez maksas».

## 1. Ko mērām

| Līmenis | Rādītājs | Avots |
|---|---|---|
| Sasniedzamība | reel noskatījumi, story pabeigšana | platformu insights (jau ir) |
| Klikšķis | sesijas uz play.tv3.lv ar `utm_campaign=play` | GA4 (Play īpašums vai kopīgs) |
| Vērtība | **skatīšanās sākums** (`video_start`) uz sesiju, 7 dienu atgriešanās | GA4 Play notikumi |
| Maksas | cena par skatīšanās sākumu, ne par klikšķi | reklāmu modulis + GA4 |

Klikšķis bez skatīšanās sākuma ir zaudējums ar reklāmas izmaksām; tāpēc
optimizācijas mērķis ir skatīšanās sākums, un uz to tiek mācīti arī formātu
svari (tāpat kā `ad_multipliers` rakstiem).

## 2. Kas vajadzīgs no Play komandas (P0)

1. **Kataloga feed** (JSON, tāpat kā `api/1/video/feed/`): id, nosaukums,
   tips (seriāls/filma/raidījums), žanri, tēmas/tagi, vecuma cenzs,
   apraksts, plakāts 2:3, kadrs 16:9, vertikāls treileris vai klips (m3u8),
   pieejamības logs (kad pazūd), jaunu sēriju grafiks, popularitāte, saite.
2. **Klipu tiesības**: vai treilerus/fragmentus drīkst likt platformās kā
   natīvu video (parasti jā savam saturam, bet licencētām filmām jāpārbauda).
3. **GA4** piekļuve Play īpašumam vai apstiprinājums, ka tas ir kopīgs ar
   portālu, un notikuma nosaukums skatīšanās sākumam.

Kamēr feed nav, izlases var kurēt ar roku `rules/play.yaml` (nosaukums,
saite, žanrs, cenzs, klips). Sistēma ar to strādā tāpat.

**Play lapu metadati (apstiprināts ar zondi 07.09.2026).** Nosaukuma lapa nes
visu vajadzīgo cXense meta tagos, ne og:video vai schema.org `genre`:

| Lauks | Piemērs | Kur aiziet |
| --- | --- | --- |
| `cXenseParse:zfv-playProductTitle` | Kinozvaigzne un kovbojs | tīrs nosaukums (og:title nes « \| Filmas») |
| `cXenseParse:zfv-playProductGenre` | Komēdijas; Drāmas; Romantika | žanri izlasēm un drūmās dienas sargam |
| `cXenseParse:zfv-playProductCategories` | drama; romance; sports | angliskie slugi (sadaļa, žanru sargs) |
| `cXenseParse:zfv-playProductImage3x4` | 468×624 | **vertikālais plakāts** stāstiem un foto |
| `cXenseParse:zfv-playSeriesTitle` / `SeriesLink` | FIBA Pasaules kauss | kuram raidījumam sērija pieder |
| `video:duration`, JSON-LD `duration` | 5253 | ilgums minūtēs kartītē |
| `cXenseParse:zfv-playProductYear` | 2023 | gads AI kontekstam |
| `…ProductLabeloriginalTitle` | The Movie Star and the Cowboy | oriģinālnosaukums AI kontekstam |
| lapas teksts | «Pieejams vēl 3 dienas», «Pēdējā iespēja» | pieejamības logs un derīguma termiņš |
| lapas birka | «10. SEZONA — FINĀLS» | sezona un kataloga notikums (fināls/pirmizrāde/jauna sezona) |

**Pieejamības logs (no nosaukuma lapas ekrāna, 07.09.2026).** Lapā ir atskaite
«Pieejams vēl 3 dienas» un sarkana birka «Pēdējā iespēja». Tas ir gan kataloga
notikums (plāna 4.2. ierosinātājs «pēdējā iespēja»), gan derīguma termiņš:
`expires_at` glabājas pie nosaukuma, pēc tā ieraksts vairs netiek plānots
(«nosaukums Play vairs nav pieejams»), un līdz tam tas iet izlases priekšgalā
ar uzrakstu «pēdējā iespēja» uz kartītes. Slieksni maina `last_chance_days`.

**Sezona un notikums (no sērijas lapas ekrāna, 07.09.2026).** Lapā ir sarkana
birka «10. SEZONA — FINĀLS». Tas ir spēcīgākais iemesls ierakstam tieši šodien,
tāpēc `labels()` no lapas un no sērijas nosaukuma nolasa sezonas numuru un
notikumu: **fināls**, **jauna sezona**, **pirmizrāde**. Notikums parādās uz
izlases kartītes («10. sezonas fināls»), AI to zina, rakstot tekstu, un
steidzamības kārtība rindā ir: pēdējā iespēja, tad notikums, tad pārējie.
Sērijas notikums nāk no tās paša nosaukuma, ne no raidījuma birkas — citādi
visas 62 sērijas būtu «fināls».

**Adrešu paraugi:** raidījums `embed-video/<slug>,serial-<show_id>`, sērija
`serija-62,episode-<ep_id>`, filma `embed-video/<slug>,vod-<id>`.

**Par atskaņotāja saiti.** Nosaukuma lapā ir arī `embedUrl`
(`play.tv3.lv/embed-video/<slug>,vod-<id>`) — tas ir atskaņotāja iframe, ne
fails. To glabājam tikai kā atsauci un NEIZMANTOJAM ierakstiem: aiz tā ir Go3
atskaņotājs ar TV3 pašu pirmsreklāmām, un straumes izvilkšana nozīmētu izplatīt
saturu bez tām — tieši pretēji AVOD mērķim. Sociālajos tīklos Play saturu nes
saite, plakāts un stāsts; treileriem vajadzīgi atsevišķi faili no Play komandas.

**Divi svarīgi secinājumi.** Pirmkārt, **vecuma cenza lapās nav vispār** — nedz
`contentRating`, nedz cits lauks; tāpēc 16+/18+ šķirošana balstās uz adrešu
sarakstu `adult_slugs` (piem. `taizeme-tikai-pieaugusajiem`), un to vērts
papildināt ar roku vai lūgt Play komandai pievienot cenzu lapā. Otrkārt,
**straumes adreses lapā nav** (Go3 atskaņotājs), tāpēc Play formāti paliek
saite, foto un stāsts — lentes no Play satura nav iespējamas.

**Metadatu audits (Diagnostika → «Analizēt visu Play metadatus»).** Tā vietā, lai
lapas pārbaudītu pa vienai, `/logs/play-audit` paņem dažus nosaukumus no katras
sadaļas (filmas, seriāli, šovi, bērniem, sports, podkāsti, A–Z) plus dažas
sērijas no sitemapa, atver to lapas un atdod:

- **lauku pārklājums** procentos (nosaukums, žanri, kategorijas, plakāts,
  ilgums, gads, apraksts, sezona, notikums, pieejamības logs, cenzs);
- **žanru un kategoriju vārdnīca** ar biežumu — tieši tā, kā Play tos raksta;
- **ko tas nozīmē sargiem**: kuri nosaukumi ir bez žanra (drūmā dienā tos
  bloķē), kuri žanri drūmā dienā ir aizliegti, vai kaut kur parādās cenzs,
  kuras sadaļas neatbild vai nedod nosaukumus (nepareiza adrese), un
  **kuri `play` noteikumi uz servera atšķiras no koda** (sk. zemāk).

Viens nosaukums, kas ir vairākās sadaļās, kopskaitā skaitās vienreiz.
Kopsavilkums paliek Diagnostikas lapā; pilnais JSON atveras jaunā cilnē.

**Ko parādīja pirmais audits (07.09.2026) un kas no tā salabots.**

| Atradums | Sekas | Labojums |
| --- | --- | --- |
| Sadaļu lapās pirmās saites ved uz **žanra filtra lapām** («Filmas – Romantika»), ne uz nosaukumiem | 12 no 29 paraugiem bija bez žanra, plakāta un gada | filtra lapu atpazīst pēc tā, ka lapā nav neviena produkta lauka; par ierakstu tā nekļūst, bet no tās paņem īstos nosaukumus, un lapas žanrs tiem noder kā rezerve |
| Katrā sadaļā ir **kopīga izceltā josla** (Bez Tabu, Degpunktā…) | visās sadaļās paraugā nonāca vieni un tie paši četri raidījumi | audits izmet saites, kas atkārtojas trīs un vairāk sadaļās, un paraugu ņem izkliedēti, ne pirmos pēc kārtas |
| Žanri nāk ar HTML entītijām («Bērniem &amp;amp; ģimenei») | vārdnīcā tas pats žanrs divreiz | meta vērtības tiek atšifrētas |
| Ziņu podkāsti (Zviedru Galds, Piķis un ģēvelis) slugu sarakstā nav | tie kļūtu par Play promo | šķiro pēc žanra/kategorijas (`exclude_genres: [ziņas, news]`), un pārbaude notiek **pēc** lapas ielasīšanas, jo pirms tam žanra vēl nav |
| `rules.yaml` mierīgo žanru saraksts bija vecs | drūmā dienā tiktu bloķēta arī animācija, mūzika, sports | saraksts sinhronizēts ar īsto vārdnīcu (latviski + angliski) |
| `/a-z/` neatbild | lieka ielase katrā apgājienā | izņemts no saraksta |

**Ko parādīja otrais audits un kas no tā salabots.**

| Atradums | Sekas | Labojums |
| --- | --- | --- |
| Drūmās dienas saraksts bija **atļauto** žanru saraksts | audits nosauca 12 bloķētus žanru: Romantika, Animācijas, Sports, Detektīvs, Medicīnas, Fantāzija, Piedzīvojumu… — praktiski viss, ko nepaspēja uzskaitīt | pāriets uz **aizliegto** sarakstu (`somber.blocked_genres`: asa sižeta, šausmu, trilleris, kara, noziegumu, katastrofu, vardarbība + tie paši angliski). Jauns žanrs, ko Play izdomās, vairs neapklust pats no sevis; bloķē tikai to, kas tiešām neiederas blakus traģēdijai |
| Serverī rediģējamā `rules.yaml` kopija palika ar veco `allowed_genres` | repo labojums līdz sistēmai nemaz nenonāca — `sync_missing_rules` pieliek tikai jaunas **augšējā līmeņa** atslēgas, bet `play` bloks tur jau ir | `somber` tagad saplūst dziļi ar koda noklusējumu (veca atslēga neko nebloķē), un audits atsevišķi nosauc katru `play` noteikumu, kas uz servera atšķiras no repo faila (`rule_overrides`) |
| `/raidijumi/` atbild, bet nedod nevienu nosaukumu | lieka ielase | izņemts no `browse_pages` |
| `min_seconds: 300` izmeta īsfilmas | «Suns Funs un Rīga» (281 s) nekad nekļūtu par promo | 120 s — ziņas tagad šķiro pēc žanra, ne pēc garuma |

**Katalogs nāk no divām vietām.** Sitemapos ir sērijas un ziņu sižeti; paši
nosaukumi (filmas, seriāli, raidījumi) ir sadaļu lapās — sākumlapā vien 426
saites. Tāpēc `browse_pages` (sākumlapa, /filmas/, /seriali/,
/sovi-un-raidijumi/, /berniem/, /sports/, /vietejais-saturs/) dod nosaukumus,
sitemapi tos papildina ar sīktēlu, ilgumu un datumu, un `page_fetch_per_run`
(12) lēni ielasa nosaukumu lapas žanram un plakātam. Nosaukums bez ielasītas
lapas netiek publicēts, bet gaida nākamo apgājienu.

**Svaigums.** Kataloga nosaukums nenoveco kā ziņa: 2023. gada filma vakar
vakaram der tāpat, tāpēc `max_age_hours: 0` (bez ierobežojuma). Atkārtošanos
tur `title_cooldown_days` un prioritātes pusperiods.

**Atklāts ar Diagnostikas zondi (03.09.2026):** play.tv3.lv ir WordPress
vietne ar servera pusē zīmētu HTML, un `robots.txt` norāda uz
`/sitemaps/sitemap.xml` (mēneša indekss) un `/sitemaps/sitemap-latest.xml`.
Mēneša sitemapi izmanto Google video paplašinājumu: katram ierakstam
`video:title`, `video:description`, `video:thumbnail_loc`, `video:duration`
(sekundes), `video:publication_date`, `video:player_loc` (`/goTo/<id>`) un
lapas adrese (`/filmas/<slug>-<id>/`, `/video/<raidījums>-<id>/<sērija>-<id>/`,
`/tiesraides/...`). Tas ir kataloga feed bez API: P0 no Play komandas
paliek tikai žanrs un vecuma cenzs (ja nosaukuma lapa tos nedod), treileru
tiesības un GA4.

## 3. Formāti, kas AVOD saturam strādā

| Formāts | Kad | Piezīme |
|---|---|---|
| **Reel no treilera/klipa** 15–30 s + CTA «skaties bez maksas Play» | pirmizrādes, jaunas sērijas, top nosaukumi | natīvs video sit plakātu 3–5× sasniedzamībā |
| **Story** ar saiti | «šovakar jauna sērija», «pēdējā diena» | 1 dienā, vakarā |
| **Karuselis «izlase»** 3–5 nosaukumi, katra kartīte ar savu saiti | nedēļas nogale, brīvdienas, sezonas tēma | izmanto esošo digest karuseļa mehāniku (kartīte → sava saite, savs `utm_term`) |
| **Saites ieraksts** uz nosaukuma lapu | kad ir spēcīgs og attēls | rezerve, klikšķu formāts |
| Nedēļas nogales franšīze | sestdiena/svētdiena | Play izlase kā viena no esošajām franšīzēm |

## 4. Aktualitāte: trīs atļautie ierosinātāji

Play ieteikums nekad nav reakcija uz ziņu noskaņu. Tam ir tikai trīs
ierosinātāji, un visi ir «pozitīvi vai neitrāli» pēc būtības:

1. **Kalendārs un laiks.** Vakari 19–22, nedēļas nogales, svētki, skolas
   brīvdienas, lietaina nedēļas nogale (laika prognoze pēc izvēles).
   Tēma: «5 filmas lietainai svētdienai», «seriāls garajiem vakariem».
2. **Kataloga notikumi.** Pirmizrāde, jauna sērija, «pēdējā iespēja» pirms
   nosaukums pazūd, nedēļas skatītākie. Tie ir pašu notikumi, un tie nekad
   nav atkarīgi no ziņām.
3. **Redakcijas tilti tikai pēc entītijas.** Raksts par aktieri, šovu,
   sporta notikumu vai TV3 raidījumu (Bez Tabu, seriāls) → tieši šī
   nosaukuma lapa Play. Sakritība ir pēc nosaukuma, personas vai taga,
   nekad pēc noskaņas vai žanra. Sporta fināls → sporta dokumentālā filma
   ir tilts; traģēdija → asa sižeta filma nav un nevar būt.

## 5. Ētikas sargi (obligāti, kods, ne laba griba)

- **Nekad no bēdām uz izklaidi.** Tilts (4.3) netiek būvēts no rakstiem ar
  jutīgumu `tragedy`, `crime` vai ar drūmiem vārdiem virsrakstā
  (esošais `GRIM_STEMS` saraksts). Noziegumu seriālus nekad nepiedāvā no
  noziegumu ziņām; tos virza tikai kataloga notikumi.
- **Drūmas dienas režīms.** Ja pēdējās stundās traģēdiju/noziegumu daļa
  starp spēcīgākajām ziņām pārsniedz slieksni (liela katastrofa, sēru diena),
  Play ieraksti apstājas vispār vai paliek tikai ģimenes, komēdijas un drāmas
  žanri bez asa sižeta, šausmām, kara un jokainiem tekstiem. Redaktors to var
  ieslēgt arī ar roku (Play pauze), un sistēma to ieslēdz pati.
- **Attālums plūsmā.** Play ieraksts nekad neiet tieši pēc traģēdijas vai
  nozieguma ieraksta tajā pašā kanālā; minimālā atstarpe 90 minūtes un
  vismaz viens cits ieraksts pa vidu. Tas ir tas pats blakusesības sargs,
  kas jau tur līdzīgus virsrakstus šķirti.
- **Vecuma cenzs.** 16+/18+ nosaukumi tikai vakara slotos pēc 21:00, ne
  stāstos, ne formātos, kur platforma ierobežo; plakāti bez vardarbības
  kadriem plūsmā.
- **Cilvēki.** Nekādu tiltu no rakstiem par konkrētu cilvēku nelaimi,
  tiesu vai nāvi, arī ja aktieris vai publiska persona. Personas tilts tikai
  pozitīvā kontekstā (intervija, jubileja, jauns projekts).
- **Caurspīdīgums.** Play ir savs produkts, ne trešās puses reklāma, bet
  ieraksts vienmēr saka «TV3 Play» un «bez maksas», nevis maskējas par ziņu.
  AI ģenerētajām izlasēm paliek esošais MI marķējums.

## 6. Izlašu dzinējs

- **Tēmas** nedēļai piedāvā AI no kalendāra un kataloga (sezona, svētki,
  žanru rotācija); sākumā redaktors apstiprina, pēc mēneša datu — automātiski.
- **Atlase**: 3–5 nosaukumi, žanru dažādība izlases iekšienē, neviens
  nosaukums biežāk kā reizi 14 dienās, priekšroka nosaukumiem ar klipu.
- **Mācīšanās**: katram nosaukumam skatīšanās sākumi uz 1000 sasniegtiem;
  vājie krīt ārā no rotācijas, spēcīgie tiek boostoti (7. sadaļa).

## 7. Kadence un vieta plūsmā

- Play saturs ir **ne vairāk kā 10 % plūsmas**: darbdienās 1 ieraksts
  kanālā, nedēļas nogalēs 2, plus 1 story dienā. Ziņas paliek dominējošas.
- Savs formātu kvotas segments «promo», lai Play neiztukšo karuseļu un lenšu
  kvotas ziņām.
- Vakara sloti (19–22 Rīgā) ar priekšroku; rindas prioritātē Play ir
  «aizpilda tukšumus» klase ar garu pusperiodu, tāpat kā video arhīvs.

## 8. Maksas pastiprināšana

- Meta boost tikai Play reel/karuseļiem, kas organiski sasniedz sliekšņa
  skatīšanās sākumus; mērķis trafiks uz play.tv3.lv, budžets ≤ 15 % no dienas
  reklāmu budžeta, cena par skatīšanās sākumu kā griesti.
- Google Demand Gen pirmizrādēm (YouTube/Discover vertikāls video), zīmola
  meklēšana «TV3 Play», «skatīties online bez maksas» + nosaukums.
- Pārdale reizi dienā pēc cenas par skatīšanās sākumu, ne par klikšķi.

## 9. Mērīšana

UTM: `utm_source=<platforma>`, `utm_medium=social`, `utm_campaign=play`,
`utm_content=<ieraksts>`, `utm_term=<nosaukuma id>-<kartīte>`. Nedēļas
atskaitē sava rinda: Play sesijas, skatīšanās sākumi, labākie nosaukumi,
labākie formāti, drūmās dienas pauzes.

## 10. Ieviešanas secība

| Posms | Kas | Priekšnosacījums |
|---|---|---|
| P0 | kataloga feed specifikācija, klipu tiesības, GA4 | Play komanda |
| P1 | `app/play.py`: kataloga ielase (vai `rules/play.yaml`), nosaukumu rindas, formāti reel/story/link/karuselis, saite ar UTM, ētikas sargi, kadences kvota | P0 vai manuāls katalogs |
| P2 | izlašu franšīze ar apstiprināšanu, entītiju tilti, drūmās dienas režīms ar automātiku | P1 + 2 nedēļu dati |
| P3 | maksas boost, Demand Gen, nosaukumu prioritātes no skatīšanās sākumiem | GA4 Play notikumi |

## Ieviešanas stāvoklis (03.09.2026)

P1 ir kodā (`app/play.py`), bet **izslēgts** (`play.enabled: false`), līdz
redakcija to ieslēdz Noteikumos:

- katalogs no `sitemap-latest.xml` un tekošā mēneša sitemapa reizi stundā;
  ziņu raidījumi (TV3 Ziņas, Degpunktā, 900 sekundes, Bez Tabu, Nekā
  personīga) un tiešraides izslēgtas, klipi zem 5 min izslēgti;
- žanrs, cenzs un plakāts no nosaukuma lapas (`video:tag`, JSON-LD `genre`,
  `contentRating`); sērijas manto raidījuma datus; `genre_overrides` ar roku;
- formāti link / photo / story, saite uz Play lapu ar `utm_campaign=play`;
- sargi kodā: vakara logs 19:00–22:30 (16+/18+ no 21:00), 90 min attālums no
  traģēdijas vai nozieguma ieraksta tajā pašā kanālā, drūmas dienas režīms
  (≥ 40 % traģēdiju/noziegumu pēdējās 6 h → nepublicē asa sižeta, šausmu,
  trilleri, kara, noziegumu un katastrofu žanrus; nezināms žanrs = bloķēts), viena raidījuma atkārtojums ne biežāk kā reizi 14 dienās,
  1 dienā (nedēļas nogalē 2) un 1 stāsts, ne vairāk kā 10 % nedēļas plūsmas;
- Diagnostikā bloks «TV3 Play» ar stāvokli, drūmās dienas rādītāju, katalogu
  un pogu «Pauzēt Play promo».

P2 un P3 arī ir kodā (tie paši slēdži):

- **izlašu karuselis** (`build_selection`): piektdienās, sestdienās un
  svētdienās no 17:00 tiek uzbūvēts 3–5 nosaukumu karuselis vakara logam
  (19:30 Rīgā), pa vienam uz raidījumu, bez 14 dienās rādītajiem, bez 16+,
  drūmā dienā bez aizliegtajiem žanriem, ne vairāk kā 2 viena žanra; katra kartīte
  ved uz savu Play lapu ar savu `utm_term`, saraksts pirmajā komentārā;
  slots atkāpjas no traģēdijas ieraksta; `selection_requires_approval`
  atstāj ierakstu stāvoklī «proposed», līdz redaktors apstiprina;
- **entītiju tilti** (`bridge_for_article`): izklaides vai sporta raksts, kura
  virsrakstā vai tagos ir Play raidījuma nosaukums, saņem rindu «Skaties …
  bez maksas TV3 Play» pirmajā komentārā (FB/IG) vai aprakstā saites
  ierakstam; nekad no traģēdijas/nozieguma vai ar jutīgumu, nekad drūmā
  dienā, ne biežāk kā reizi 3 dienās uz raidījumu, X/Threads tekstā nē;
- **maksas boost** (`ads.candidates`): Play ieraksti nonāk reklāmu kandidātos
  tikai tad, kad organiski sasnieguši slieksni (1000 sasniegti vai 10
  klikšķi), saņem ne vairāk kā `ads:play_share` (15 %) no konversiju
  budžeta, drūmā dienā tiek apturēti; Google trafiks tiem iet caur to pašu
  Demand Gen;
- **nosaukumu prioritātes** (`title_scores`): sesijas (vai klikšķi) uz 1000
  sasniegtajiem pa raidījumiem no Play ierakstu metrikām — kārto izlases un
  reklāmu kandidātus; tiklīdz GA4 dod Play `video_start`, te maina avotu.

## 11. Atklātie jautājumi

0. ~~Pieejamības logs?~~ Atbildēts: lapa to rāda («Pieejams vēl N dienas»), un
   sistēma to lasa — nosaukums pēc termiņa vairs netiek publicēts.
1. ~~Vai Play ir kataloga API?~~ Atbildēts: API nav vajadzīgs, katalogs nāk no
   sadaļu lapām un sitemapiem. **Bet vecuma cenza lapās nav** — vai CMS to var
   pievienot (`contentRating` vai meta tags)? Līdz tam 16+/18+ šķiro adrešu
   saraksts.
2. Treileru tiesības sociālajiem tīkliem licencētām filmām? (Straumes adreses
   lapās nav, tāpēc treilerus vajadzētu kā atsevišķus failus.)
3. GA4: atsevišķs Play īpašums vai kopīgs; skatīšanās sākuma notikuma nosaukums?
4. Vai viss saturs ir bez maksas (AVOD), vai daļa ir abonementā? Tas maina CTA.
5. Kurš apstiprina izlases pirmajā mēnesī — redakcija vai Play mārketings?
