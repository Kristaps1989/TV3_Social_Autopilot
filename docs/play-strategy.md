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
  (≥ 40 % traģēdiju/noziegumu pēdējās 6 h → tikai mierīgi žanri; nezināms
  žanrs = bloķēts), viena raidījuma atkārtojums ne biežāk kā reizi 14 dienās,
  1 dienā (nedēļas nogalē 2) un 1 stāsts, ne vairāk kā 10 % nedēļas plūsmas;
- Diagnostikā bloks «TV3 Play» ar stāvokli, drūmās dienas rādītāju, katalogu
  un pogu «Pauzēt Play promo»;
- vēl nav (P2/P3): izlašu karuseļi, entītiju tilti no rakstiem, maksas boost.

## 11. Atklātie jautājumi

1. Vai Play ir kataloga API, un vai tajā ir vecuma cenzs un pieejamības logs?
2. Treileru tiesības sociālajiem tīkliem licencētām filmām?
3. GA4: atsevišķs Play īpašums vai kopīgs; skatīšanās sākuma notikuma nosaukums?
4. Vai viss saturs ir bez maksas (AVOD), vai daļa ir abonementā? Tas maina CTA.
5. Kurš apstiprina izlases pirmajā mēnesī — redakcija vai Play mārketings?
