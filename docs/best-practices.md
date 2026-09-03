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

## Svaigums ir griesti, ne tikai vārti

`max_age_hours` sākotnēji pārbaudīja tikai **lēmuma brīdi**: vai raksts vēl
ir pietiekami svaigs, lai par to tagad lemtu. Slots pēc tam varēja aizceļot
līdz 48 h uz priekšu (`SEARCH_HORIZON_HOURS`), un, ja rinda statusa logā bija
pilna, cauruļvads atmeta termiņu pilnībā — tā šodienas ziņa nokļuva parīta
stāstā.

Tāpēc `Verdict` tagad nes divus dažādus termiņus:

| Lauks | Kas tas ir | Vai atmetams |
|---|---|---|
| `latest` | statusa termiņš («must» — `must_max_delay_hours`) | jā, kad rinda pilna |
| `fresh_until` | satura derīgums (`max_age_hours` no publicēšanas) | **nekad** |

Pirmais ir mūsu solījums redakcijai, otrais — paša satura īpašība: vakardienas
ziņa rīt nekļūst svaigāka. `evergreen` raksti griestus nedabū.

Tas pats attiecas uz rindu: `stale_publish_guard` tieši pirms publicēšanas
atceļ ierakstu, kura raksts pa gaidīšanas laiku ir novecojis — citādi
labojums aizsniegtu tikai jaunos rakstus, un jau ieplānotie tik un tā iznāktu
kā vakardienas ziņa šodienas stāstā.

Sargs atceļ **tikai to, ko automātika ieplānojusi pati**. Divi izņēmumi, abi
atzīmēti uz paša ieraksta, ne uzminēti pēc receptes veida:

- `extra.timeless` — nedēļas franšīze («nedēļas TOP», «nedēļas skaitlis»,
  kvīzs). Tie ir atskatoši pēc būtības: raksts tur ir atsauce, ne temats, un
  dienas vecums ir plāns, ne nolaidība.
- `extra.manual` — redaktors formātu pieprasījis pats. Cilvēka apzinātu
  lēmumu automātika neatceļ.

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
| Švīkošanas norāde | sarkana nodaļu josla baltajā apakšjoslā + «N/M ›» pie logo |
| Lente 9:16, ≤60 s | 1080×1920; par garu lente zaudē PĒDĒJO NODAĻU veselu, nevis balsi pusteikumā (`_trim_to_budget`) |
| Kadru nosaka TĀ PAŠA kadra ieruna | `plan_durations`: katram kadram sava sintēze, kadrs = tās garums |
| Kadrā tik teksta, cik paspēj izlasīt | vismaz `MIN_FRAME_SECONDS`; klusam kadram 5.5 s (punkts 2.8 s) |
| Balss nelasa to, kas jau ir uz ekrāna | `chapter_voice`: nodaļas virsraksts ir vizuāls marķieris, balss saka tikai tekstu |
| Ieruna latviski, rakstīta RUNĀŠANAI | sadaļu `body`; izrunas vārdnīca (`tv3.lv` → «tv trīs punkts lv») |
| Balss un temps pa sadaļām | `reel_voice_by_section`, `reel_voice_rate_by_section` — izklaidei cita balss un ātrāks temps nekā pierobežas ziņai |
| Skatītājs redz, cik tālu stāsts | josla kadra augšā skaita VISUS kadrus, ne tikai nodaļas |
| Vāks runā tikai virsrakstu | atsevišķs āķis atkārtoja to pašu, ko pirmā nodaļa |
| Skaitļi izrunāti latviski | `lvnum`: «59. minūtē» → «piecdesmit devītajā minūtē»; «pret 64.» → «pret sešdesmit četri» |
| Kadrs nekad nav plakans krāsas laukums | foto → izpludināta photopost grafika → gradients. Tas attiecas uz VISIEM pilnekrāna formātiem: vāki, sadaļu kartītes, lentes kadri, «Nedēļas skaitlis», jautājuma karte |
| Stāsts = tā pati lente, ne statisks attēls | `story_reuses_reel`; stāstos skaņa tiešām skan |
| Karuselis un lente nepārņem plūsmu | `format_daily_cap` (2+2 dienā) un saites grīda `format_mix` attiecas arī uz AI izvēli; pārsniegums kļūst par saiti ar iemeslu vērtējumā un `post.extra.format_notes`; promptā AI redz šodienas kvotas kā faktus |
| Lentes neveiksme nav klusa | `record_render_failure("reel")` + piezīme «bija reel → …» lapā «Kāpēc» |
| Digest ieraksts ved uz to, ko sola | «TOP 5», «Nedēļa 30 sekundēs», gids: galvenā saite ir lasītākais raksts (ne sākumlapa), tekstā numurēti virsraksti, pirmajā komentārā saites katram ar `utm_term=<franšīze>-N` (`post.extra.items`, `pipeline.reading_list`) |
| Franšīze nekad nav par franšīzi | `week_top` izlaiž `_digest` rakstus; «Trešdienas jautājums» ņem tikai rakstu ar attēlu — citādi kadrs ir plakans krāsas laukums un saite ved uz tv3.lv sākumlapu |
| Galvas paliek kadrā | gatavu (photopost) grafiku vākā rāda VESELU uz savas izpludinātās kopijas (`cover_fit=contain`), ja tā nav plata (< 1.2:1) — kvadrāts 1080×906 laukā zaudēja 174 px augšas; pašu foto griezums enkurots pie `center 22%` (galvas ir augšējā trešdaļā); vākam un foto ierakstam priekšroka platam tīram foto (`imageinfo.wide_image`) |
| Diagnostika bez Railway konsoles | sadaļa **Diagnostika** (`/logs`): vide, formātu sargi pa kanāliem, katra ieraksta iemesls, maksas rezultāti pa formātiem, simulācija «ko izvēlētos šobrīd» un pēdējie žurnāla ieraksti; `/logs/export.json` dod visu vienā failā (atslēgas izfiltrētas) |
| Kāpēc tieši šis formāts — redzams | `formats.explain` dod pilnu pēdu (svari × izmērītais × sadaļa × reklāma × piesātinājums × AI bonuss, bloķētie sargi); tā nonāk Railway logos, ieraksta `extra.format_trace` un `python scripts/format_report.py` izdrukā |
| Sargi nekonfliktē | saites kartītes labojums (`link_card_hurts`) padodas vienveidības sargam; saites grīda karuseli atceļ tikai tad, ja šim rakstam saite VISPĀR iespējama; kad neviens formāts nav tīrs, izvēlas mazāko ļaunumu (pēc kārtas = smagāks pārkāpums nekā daļas griesti) |
| Kvota nav rotācijas rīks | dienas kvota ir galējais drošinātājs (`card_carousel` 8, `reel` 4 — lentei arī TTS izmaksu dēļ); rotāciju dara `format_max_share` un atkārtojuma sargs. Kvota 2 pie ~30 ierakstiem dienā formātu pēc diviem ierakstiem izslēdza uz visu dienu |
| Viena formāta kanālam sargu nav | stāstu kanālā «pēdējie 6 ir story» nav vienveidība, bet vienīgā iespēja (`formats.single_format`) |
| Foto griesti pēc platformas | X, Threads un Instagram saites kartīte virsrakstu nerāda, tāpēc tur brendēts foto ir ieteicamais formāts: griesti 70 %, ne 50 % (`PLATFORM_MAX_SHARE`) |
| Plūsma nav vienveidīga | `max_same_format_in_row` (2) un `format_max_share` (karuselis/lente 35 %, foto 50 %) — pārsniegtais formāts konkursā nepiedalās, arī tad, ja tā ir AI izvēle un dienas kvota vēl brīva |
| Maksas puse formātu izvēli informē ar DATIEM | `ads_inform_format`: boostot var visus trīs formātus (saites kartīte ar CTA, karuseļa kartītes ir saites, foto — saite aprakstā un komentārā), tāpēc sesijas par eiro pa formātiem mēra `formats.ad_multipliers` (±20 %, no 3 reklāmām formātā, tikai approve/auto) |
| Lente, kas top PĒC stāsta, stāstu tomēr sasniedz | rokas «Uztaisīt» un nākamie viļņi atjauno vēl nepublicēto stāstu; pirms publicēšanas stāsts ar attēlu vēlreiz paskatās, vai lente nav parādījusies |

### Kāpēc kadru HTML top pēc apgriešanas

Sākumā kadrus zīmējām, tad rēķinājām ierunu, tad apgriezām lenti budžetā.
Izdzīvojušie kadri tad nesa veco kopskaitu, un progresa josla solīja «1 no 3»
lentē, kurā nodaļu bija divas. Tagad ir plāns (`plan_beats`) → ieruna →
apgriešana → un tikai tad HTML. Katrs kadrs plānā nes savu ierunu un savu
ilgumu, tāpēc teksti un kadri vairs nav divi paralēli saraksti, kas var
izšķirties.

### Balss temps

`reel_voice_rate` ir procenti pret pakalpojuma noklusējumu; noklusējums ir
**-4%**, jo lentē skatītājs vienlaikus lasa arī kadra tekstu. Vienu skaitli
saprot abi pakalpojumi: Azure to saņem kā SSML `prosody rate`, ElevenLabs —
kā `voice_settings.speed` reizinātāju.

ElevenLabs `speed` tiek sūtīts **tikai tad, kad temps ir mainīts**. Vecāki
modeļi šo lauku nepieņem, un nederīgs lauks nozīmētu klusu lenti visiem, ne
tikai tiem, kam gribējām ātrāk.

Temps ir arī keša atslēgā. Bez tā ātrāka izklaides ieruna atbildētu ar veco,
lēnāko failu, un iestatījums izskatītos pēc neieviesta.

**Kāds temps ir pareizs.** Orientieris ir TV ziņu diktors — ap 130–150
vārdiem minūtē; sarunvaloda ir ātrāka, un tieši tāpēc neironu balss
noklusējums ziņām parasti ir par ātru. Bet procenti paši par sevi neko
nepasaka: tie ir attiecība pret pakalpojuma noklusējumu, un tas Azure un
ElevenLabs atšķiras. Tāpēc priekšskatījums pie uzbūvētas lentes rāda **īsto
izmērīto tempu** (vārdi / izmērītais runas garums) — pēc tā regulē, nevis pēc
procentiem.

**Piemērs Noteikumos nav iestatījums.** Sadaļu balsis un tempi
(`reel_voice_by_section`, `reel_voice_rate_by_section`) piegādāti tukši, un
zem tiem ir izkomentēts piemērs. Izkomentēta rinda ar pareizu balss ID no
ekrāna izskatās tieši tāpat kā strādājošs iestatījums — tā ir visbiežākā
vieta, kur tiek mainīts kaut kas, kas neko nedara. Tāpēc: piemēru rindas ir
uzrakstītas tā, ka pietiek noņemt `#` (atkāpe paliek), priekšskatījums pie
ierunas raksta, ar KURU balsi un KĀDĀ tempā lente tiešām tika ierunāta un vai
tas nāca no sadaļas vai no kopīgā noteikuma, un Konti lapa saka, cik sadaļām
izņēmums vispār ir. Ja gribi mainīt balsi visur, tas ir `reel_voice_name`, ne
sadaļu saraksts.

Mūsu gadījumā ir vēl viens ierobežojums, kura TV diktoram nav: **kadra garumu
nosaka ieruna**. Ātrāka runa nozīmē īsāku kadru, bet teksts uz ekrāna paliek
tikpat garš — tāpēc temps augšpusē ir ierobežots ar to, cik ātri to tekstu var
izlasīt, ne ar to, cik ātri to var pateikt.

### Kāpēc skaitļus pārrakstām pirms sintēzes

Balss «59. minūtē» lasa kā «piecdesmit devītā minūtē»: punkts aiz cipara tai
nozīmē kārtas skaitli nominatīvā. Latviski tur vajag lokatīvu, un locījumu
nosaka NĀKAMAIS vārds — analīzi, ko sintēze nedara. `app/lvnum.py` to izdara
pirms teksts aiziet uz Azure; ekrānā redzamais «59. minūtē» paliek neskarts.
Tas pats punkts strādā arī otrādi: teikuma **beigās** punkts nav kārtas
skaitļa zīme. «Serbija uzvarēja ar rezultātu 76 pret 64.» balss nolasīja kā
«sešdesmit ceturtais»; tur vajag pamata skaitli. Kontekstu nosaka nākamais
burts — mazais nozīmē locījumu, lielais (vai teksta beigas) nozīmē jaunu
teikumu.

Apzināta robeža: pārrakstām tikai tad, kad kontekstu var pateikt droši;
citādi atstājam ciparus, jo uzminēts locījums skan sliktāk nekā tas, ko balss
dara šodien. Zināma nepilnība — «1. Maija ielā» iznāk kā «viens. Maija ielā»,
jo lielais burts te nav jauns teikums; ielu nosaukumi ziņu ierunā ir retāk
nekā rezultāti, un tādu gadījumu risina izrunas vārdnīca Noteikumos.

### Kāpēc ieruna ir pa kadriem

Sākotnēji balss bija viens gabals pār visu lenti, un kadru garumus mēroja
proporcionāli tā kopgarumam. Tas ir pareizi tikai tad, ja katra nodaļa runā
tieši tik ilgi, cik liela daļa kadru tai pieder — praksē nekad. Rezultāts:
attēls aizskrēja priekšā, CTA kadrs stāvēja, kamēr balss vēl stāstīja
iepriekšējo nodaļu. Tagad katram kadram tiek sintezēts savs gabals, kadra
garums ir `lead + runa + elpa`, un video ar skaņu saliek no tiem pašiem
segmentiem — noiet nav no kā rasties. Sedz
`test_per_frame_narration_end_to_end_keeps_video_and_voice_together`.

## MI marķējums (ES Regula 2024/1689, 50. pants)

Parakstus, birkas, kartīšu tekstus un lentes ierunu raksta mākslīgais
intelekts. 50. panta 2. punkts prasa mākslīgi ģenerētu audio, attēlu, video
un tekstu marķēt; 4. punkts prasa to atklāt arī tāpēc, ka ziņas ir saturs
par sabiedrībai nozīmīgiem jautājumiem. Marķējumam jābūt skaidram un
pamanāmam, tāpēc tas ir trijās vietās vienlaikus:

**Apjoms (`ai_disclosure_scope`, noklusējums `voiced_reels`): tikai lentes,
kurās tiešām skan sintezēta balss.** Tā ir vienīgā daļa, kas ir mākslīgi
ģenerēts *medijs*. Rakstu raksta žurnālists; parakstu un kartīšu tekstus MI
palīdz formulēt no viņa raksta, un tos redakcija apstiprina. Atruna zem katra
ieraksta lasījās kā apgalvojums, ka MI ir uzrakstījis RAKSTU — tas nav
taisnība un maldina lasītāju tikpat lielā mērā, cik marķējuma trūkums.
`all` atgriež marķējumu uz visiem formātiem (plašākā interpretācija).

| Kur | Kas |
|---|---|
| Uz grafikas | «MI · Veidots ar MI» zīmīte ierunātas lentes kadros |
| Grafikas beigās | pilns teikums lentes CTA kadrā |
| Parakstā | atsevišķa pēdējā rinda; X — īsā forma (280 zīmes) |
| Skaļi | pēc noklusējuma NĒ (`ai_disclosure_spoken: ""`) — izrunāta tā nāca kā liekais teikums aiz aicinājuma; parakstu ekrānlasītājs nolasa tāpat |

Vieta parakstā tiek **rezervēta pirms** teksta apgriešanas
(`sanitize_copy(reserve_chars=…)`), citādi tvīts ar atrunu pārsniegtu limitu
tieši tad, kad teksts ir garš. Ja AI paraksts par MI jau ieminas pats, otro
reizi nepieliekam (`disclosure.in_caption`).

Formulējumu var mainīt Noteikumos (`ai_disclosure_text`, `_short`,
`_spoken`). Noklusējums ir **ieslēgts**, un koda noklusējums darbojas arī
tad, ja instances rediģējamajā kopijā atslēgas vēl nav — bet Noteikumu lapa
tagad par tādu novirzi brīdina (`config.missing_rules`), lai redaktors zina,
ko viņš neredz.

## Saites kartītes apgriezums

Saites ierakstā attēlu **izvēlas Facebook, ne mēs**: Graph API `/feed` pieņem
tikai `message` un `link`, un attēlu tā paņem no raksta `og:image`. Kartītes
rāmis ir 1.91:1, un šaurāku attēlu tā griež pa vertikāli:

| Attēls | Cik augstuma pazūd |
|---|---|
| 1.91:1 | 0% |
| 16:9 | 7% |
| 3:2 | 21% |
| 4:3 | 30% |
| kvadrāts | 48% |
| portrets 4:5 | 58% |

Ziņu fotogrāfijā galvas ir augšējā trešdaļā, tāpēc tieši tās pazūd pirmās.
Mūsu pašu grafikās šīs problēmas nav: visi mūsu rāmji (4:5, 1:1, 9:16) ir
šaurāki par tipisko 3:2 foto, tāpēc `cover` tur griež SĀNUS un augstums
paliek vesels.

Tāpēc vienīgā svira ir formāts:

- `link_card_max_crop` (0.20) — virs šī raksts kļūst par photo ierakstu, ja
  vien kanālam nepietrūkst saites postu (`format_mix` grīda).
- `link_card_force_crop` (0.40) un vertikāls attēls — te grīda vairs neaiztur.

Otrais slieksnis ir tāpēc, ka grīdas jēga ir turēt plūsmā **strādājošus**
saites ierakstus. Pie 58% nogriezta augstuma tāda nav: piespiest ierakstu
palikt saitē tikai tāpēc, ka kvota nav pilna, nozīmē uztaisīt sliktu ierakstu,
kas kvotu tik un tā nepilda — un vēl iemācīt izmērītajiem svariem, ka saites
posti nestrādā. Trūkstošo kvotu aizpilda nākamais raksts ar derīgu attēlu.

Pārslēgšana notiek arī **rindā gaidošiem** ierakstiem tieši pirms
publicēšanas (`retarget_queued_link_post`) — tāpat kā grafiku pārzīmēšana,
citādi labojums aizsniegtu tikai tos rakstus, par kuriem lēmums pieņemts pēc
izvietošanas. Priekšskatījumā redaktors redz gan īsto apgriezumu (kartīte tur
zīmēta 1.91:1), gan procentu, gan to, kurš noteikums nostrādās.

Pārslēgšana tomēr **padodas vienveidības sargam**: ja plūsmas galā jau ir divi
foto pēc kārtas, arī sabojāta saites kartīte iet ārā kā saite (tieši tā
notika ar Ostapenko ierakstu — 58 % nost un galva ārpus kadra).

### Savs kartītes attēls: attēls «nobīdās zemāk»

Graph API `/feed` pieņem arī `picture` — publisku URL kartītes attēlam — **ja
lapa ir verificējusi saites domēnu** Meta Business Manager (Brand Safety →
Domains; tv3.lv pieder tam pašam biznesam, tāpēc tas ir vienas dienas darbs).
Tad kartītes attēlu izvēlamies mēs (`link_card_custom_picture`):

- tas pats raksta foto (vai platākais no raksta attēliem), iegriezts 1200×628
  un **piesiets augšai** (`PHOTO_FOCUS`, 22 % no augšas) — kadrs kartītē
  nobīdās zemāk un galvas paliek;
- zīmēts tieši pirms publicēšanas (`link_picture_for`), lai fails nevar
  pazust rindā stāvot; ceļš paliek `extra["link_picture"]`;
- pirmais ieraksts ir pārbaude: ja Facebook atbild «Only owners of the URL…»,
  adapteris to pašu ierakstu nosūta bez attēla, statuss `rejected` paliek
  nedēļu, un Konti lapa pasaka, kas jāverificē; pēc pieņemšanas statuss `ok`
  un saites ieraksti vairs netiek pārslēgti uz photo — kartīte vairs nav
  sabojāta.

Kamēr domēns nav verificēts, viss iepriekšējais (sliekšņi, pārslēgšana uz
photo, sargs) darbojas kā līdz šim.

## tv3.lv/video arhīvs

Īsts vertikāls klips sit slideshow; tāpēc raksta lente un stāsts top no
piesaistītā tv3.lv/video klipa, un klipi bez raksta kļūst par reel/story ar
savu dienas limitu. Saite šādos ierakstos ved uz konkrēto video lapu, ne uz
rakstu — tā skatītājs paliek portāla video plūsmā. Sīkāk: `docs/video-archive.md`.

## Format → clicks

- Link post = best CTR to site → default for news/politics/sport.
- Photo album only for real galleries (≥4 images); photo post for
  entertainment/visual stories.
- Instagram is reach, not clicks (no links in captions) — lowest weight for
  the pageview KPI; the link lives in the first comment and the caption
  says so.

## Platformu stratēģija (mērķis: tv3.lv apmeklējumi)

Facebook fotoieraksts, kartīšu galerija, lente un saites ieraksts ir
atstrādāti; pārējās platformas dabū TO PAŠU, ne savu versiju. Grafika ir
raksta, ne kanāla: lenti un galeriju uzbūvē vienu reizi (pirmais kanāls
rindā, `order_channels`), pārējie to pārizmanto (`built_media`,
`share_built_media`). Viens fails, viena ieruna, viens rēķins par TTS — un
visos kanālos redzama tā pati versija.

| | Facebook | Instagram | Threads | X |
|---|---|---|---|---|
| Ātra ziņa | link (kartīte ar virsrakstu) | photo | link (teksts + saites kartīte) | link |
| Vizuāls stāsts | photo (brendēts attēls) | photo | photo | photo (X kartīte virsrakstu nerāda) |
| Skaidrojums, «kas jāzina», analīze | card_carousel / reel | reel → card_carousel | reel / card_carousel | reel; card_carousel = tvīts ar 4 attēliem (vāks + pirmās nodaļas) |
| Galerija (≥ 4 attēli) | photo_album | photo_album | photo_album (karuselis) | photo_album (4 attēli) |
| Vertikāls | story (pārizmanto lenti) | story (pārizmanto lenti) | — | — |
| Kur saite | ierakstā + pirmajā komentārā | TIKAI pirmajā komentārā; aprakstā «Saite komentāros 👇» (`ig_link_pointer`) | tekstā (klikšķināma); `threads_link_in_reply` liek atbildē | tekstā; `x_link_in_reply` liek atbildē |
| Hashtag | 0–1 | 3–5 tematiski | 0–1 | ≤ 2 tematiski |
| Ko mēra | GA4 utm + FB ieskati | GA4 utm + reach/likes | GA4 utm + views/likes | GA4 utm |

Kāpēc tieši tā:

- **Lente ir formāts, ko visas trīs platformas ceļ augstāk par citiem** —
  IG Reels un X video ir vienīgie ceļi ārpus sekotāju loka; Threads video ir
  jauns un ar mazu konkurenci. Tāpēc AI to piedāvā visos kanālos reizē
  (`system_base.md`), nevis katram atsevišķi.
- **Saite tekstā ir noklusējums X un Threads.** «Saite atbildē» taktika
  paceļ sasniedzamību, bet katrs papildu pieskāriens maksā klikšķus, un mūsu
  KPI ir klikšķi. Tā ir ieslēdzama (`x_link_in_reply`,
  `threads_link_in_reply`), lai to var izmērīt ar utm, nevis ticēt.
- **Instagram saite nav klikšķināma aprakstā**, tāpēc tur saite iet pirmajā
  komentārā, un aprakstā tiek pateikts, kur tā ir — bez norādes lasītājs
  nezina, ka rakstu vispār var atvērt. Story ar saites uzlīmi API nedod, tāpēc
  IG stāsts ir sasniedzamības, ne klikšķu formāts.
- **X kartīšu galerija ir 4 attēli**, jo vairāk tvītā nevar. Ņemam vāku un
  pirmās nodaļas — beigas lasītājs atrod rakstā, un tieši tas ir mērķis.
- **Katrā kanālā cits āķis** (`hook_type` → `utm_term`): tas pats raksts
  četrās platformās ir starpplatformu A/B tests, ko GA4 izmēra.
- **Kanālu formātu saraksti ir redakcijas lēmums**, tāpēc jauns formāts
  strādājošas instances `channels.yaml` kopijā automātiski nenonāk —
  Noteikumu lapa un `/why` pasaka, kas trūkst (`missing_channel_formats`).

## Measurement

- Every link carries `utm_source/{platform}, utm_medium=social,
  utm_campaign=autopilot, utm_content={post_id}` → GA4 sessions per post.
- Phase 3 replaces all manual weights with measured sessions-per-post by
  channel × format × section × hour.
- A/B dimensijas, ko mēra atsevišķi: āķa stils (`utm_term`), **ieruna pret
  klusu lenti** un **video stāsts pret attēlu** (Statistikas lapā). Visur
  parādīts kā virziena rādītājs ar ierakstu skaitu — ne kā pierādījums.

## Kas apzināti NAV izdarīts

- **Ierunas subtitri.** Nodaļu kadros balss saka tieši to, kas rakstīts uz
  ekrāna — tur paraksts faktiski ir. Vāka kadrā balss lasa virsrakstu, kas
  arī ir ekrānā; nesegts paliek tikai noslēguma aicinājums. Īsti subtitri
  (word-level no Azure `WordBoundary` → burn-in vai SRT) vēl nav.
- **Skaitļi ārpus 1-999 un gadskaitļiem.** `lvnum` pārraksta kārtas skaitļus
  tikai tad, kad nākamais vārds locījumu pasaka, un neaiztiek apaļus
  gadskaitļus («2000. gadā»), decimāldaļas un rezultātus («1:0»). Tie skan
  tā, kā Azure tos lasa šodien.
- **Mašīnlasāms MI marķējums.** Redzamā un dzirdamā atruna ir; C2PA /
  IPTC metadatu marķējums failā vēl nav — 50. panta 2. punkts to sagaida no
  ģenerētāja, un mūsu gadījumā tas ir Azure/Anthropic, ne mēs. Kad publicējam
  pārkodētu MP4, sākotnējie metadati tāpat pazūd, tāpēc te vajag savu soli.
- **Threads alt teksts** sūtām (IMAGE/VIDEO konteineriem), bet ja API to
  noraida, ieraksts iet bez tā — bez apraksta ieraksts ir sliktāks, bez
  ieraksta nav nekāda.
- **Instagram kanāls** izslēgts (`active: false`), līdz konts ir sasaistīts;
  stratēģija, prompts un adapteris ir gatavi.
- **X saite atbildē / Threads saite atbildē** — implementēts, bet izslēgts:
  KPI ir klikšķi, un bez mērījuma pieņemam, ka papildu pieskāriens tos
  samazina. Ieslēdz un salīdzini utm.


## Kadence: ziņas iet ārā svaigas, ne pēc kārtas

Problēma, kas bija redzama rindā: vakara ziņu vilnis ar fiksētu 45 min
atstarpi un klusajām stundām 01:00–06:00 sarindojās līdz nākamā rīta 09:45.
Rīta plūsmā iznāca vakardienas ziņas, un tieši tās vēlāk būtu jāpastiprina
ar reklāmu — novēlotas.

Ko dara lielie ziņu konti (BBC, Guardian, Delfi, LSM): dienā ieraksti ik
pēc 15–30 min, lūzuma ziņa uzreiz, arī 5 min pēc iepriekšējās. Facebook
plūsma ir ranžēta, ne hronoloģiska — divi ieraksti 15 min attālumā
nekonkurē; «sods par biežu postēšanu» attiecas uz mēstulēm, ne uz ziņu
kontu ar iesaisti. Vērtība krīt strauji: cietā ziņa pusi vērtības zaudē
~4 stundās, sporta rezultāts ~2, skaidrojums un izklaide tur dienām.

| Princips | Ieviests |
|---|---|
| Atstarpe ir atkarīga no rindas, ne fiksēta | `slots.adaptive_gap`: tukšā rindā `min_gap_minutes` (45), dziļā saraujas tā, lai rinda iztukšotos `backlog_horizon_hours` (2 h) laikā, bet ne šaurāk par `min_gap_floor_minutes` (FB 15, X 10, Threads 15). Stāstu kanālam adaptācijas nav |
| Rindu kārto vērtība × svaigums, ne ienākšanas kārta | `slots.priority` = statuss (now 3, must 2, can 1) + AI vērtējums, dalīts uz pusi ik pēc `section_half_life_hours` (news 4, sport 2, entertainment 24); `slots.replan_channel` pēc katra viļņa pārkārto kustināmos ierakstus |
| Vecu ziņu labāk nepublicēt nekā publicēt vēlu | pārplānojot ierakstu, kas jau pārsniedz `max_age_hours`, atceļ uzreiz un slots aiziet svaigākam (līdz šim to noķēra tikai publicēšanas brīdī) |
| Klusās stundas nav absolūtas | `quiet_hours_exempt`: sporta rezultāts pēc vakara mača un ziņa ar AI vērtējumu ≥ 0,85 iet arī naktī, ja raksts nav vecāks par 2 h |
| Redaktora un franšīžu ieraksti stāv, kur likti | pārplānošana neaiztiek `manual`, `timeless` un `now` ierakstus; otrā viļņa ieraksts saglabā «ne agrāk» (`not_before`) |
| Reklāmai ieraksts ir svaigs | boostu kandidāti ir pēdējo 48 h ieraksti; svaiga publicēšana = boosts strādā ar aktuālu ziņu |

Diagnostikā (`/logs`) katram kanālam redzama rinda uz priekšu: cik gaida,
kāda atstarpe tagad, katra ieraksta vērtība šobrīd un vai tas pārplānots.
