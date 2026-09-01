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
| Švīkošanas norāde | sarkana nodaļu josla baltajā apakšjoslā + «N/M ›» pie logo |
| Lente 9:16, ≤60 s | 1080×1920; par garu lente zaudē PĒDĒJO NODAĻU veselu, nevis balsi pusteikumā (`_trim_to_budget`) |
| Kadru nosaka TĀ PAŠA kadra ieruna | `plan_durations`: katram kadram sava sintēze, kadrs = tās garums |
| Kadrā tik teksta, cik paspēj izlasīt | vismaz `MIN_FRAME_SECONDS`; klusam kadram 5.5 s (punkts 2.8 s) |
| Balss nelasa to, kas jau ir uz ekrāna | `chapter_voice`: nodaļas virsraksts ir vizuāls marķieris, balss saka tikai tekstu |
| Ieruna latviski, rakstīta RUNĀŠANAI | sadaļu `body`; izrunas vārdnīca (`tv3.lv` → «tv trīs punkts lv») |
| Skatītājs redz, cik tālu stāsts | josla kadra augšā skaita VISUS kadrus, ne tikai nodaļas |
| Vāks runā tikai virsrakstu | atsevišķs āķis atkārtoja to pašu, ko pirmā nodaļa |
| Skaitļi izrunāti latviski | `lvnum`: «59. minūtē» → «piecdesmit devītajā minūtē» |
| Kadrs nekad nav plakans krāsas laukums | foto → izpludināta photopost grafika → gradients. Tas attiecas uz VISIEM pilnekrāna formātiem: vāki, sadaļu kartītes, lentes kadri, «Nedēļas skaitlis», jautājuma karte |
| Stāsts = tā pati lente, ne statisks attēls | `story_reuses_reel`; stāstos skaņa tiešām skan |

### Kāpēc kadru HTML top pēc apgriešanas

Sākumā kadrus zīmējām, tad rēķinājām ierunu, tad apgriezām lenti budžetā.
Izdzīvojušie kadri tad nesa veco kopskaitu, un progresa josla solīja «1 no 3»
lentē, kurā nodaļu bija divas. Tagad ir plāns (`plan_beats`) → ieruna →
apgriešana → un tikai tad HTML. Katrs kadrs plānā nes savu ierunu un savu
ilgumu, tāpēc teksti un kadri vairs nav divi paralēli saraksti, kas var
izšķirties.

### Kāpēc skaitļus pārrakstām pirms sintēzes

Balss «59. minūtē» lasa kā «piecdesmit devītā minūtē»: punkts aiz cipara tai
nozīmē kārtas skaitli nominatīvā. Latviski tur vajag lokatīvu, un locījumu
nosaka NĀKAMAIS vārds — analīzi, ko sintēze nedara. `app/lvnum.py` to izdara
pirms teksts aiziet uz Azure; ekrānā redzamais «59. minūtē» paliek neskarts.
Apzināta robeža: pārrakstām tikai tad, kad nākamais vārds locījumu tiešām
pasaka; citādi atstājam ciparus, jo uzminēts locījums skan sliktāk nekā tas,
ko balss dara šodien.

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
- **Threads alt teksts** — API to nepieņem.
- **Instagram kanāls** izslēgts (`active: false`), līdz konts ir sasaistīts.
