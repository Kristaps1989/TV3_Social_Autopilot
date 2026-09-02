# Maksas izplatīšanas stratēģija — Google un Meta vienā autopilotā

Mērķis ir viens: **vairāk klikšķu uz tv3.lv**, ar nosacījumu, ka daļa
budžeta pastāvīgi strādā zīmola atpazīstamībai, lai klikšķi nāk arī bez
maksas — no Discover, no meklēšanas, no ieraduma atvērt tv3.lv.

## 1. Kāpēc Google, ja Discover jau rāda rakstus

Google Discover un Google News ir organiski: tur nav ko pirkt, un tur
uzvar tehniskā kvalitāte (NewsArticle, lieli attēli, ātrums) un virsraksti
bez klikbeita. Maksas ceļš tajā pašā plūsmā ir **Demand Gen** — Google
reklāmu formāts, kas rādās Discover, YouTube (arī Shorts) un Gmail plūsmās,
izskatās pēc satura kartītes un mērķē pēc interesēm. Ziņu portālam tas ir
tuvākais «boost» analogs Facebook boostam: viens raksts, viena kartīte,
klikšķis uz portālu.

Google reklāmu produkti, kas ziņu portālam der, un tie, kas neder:

| Produkts | Loma tv3.lv | Kāpēc |
|---|---|---|
| **Demand Gen** (Discover/YouTube/Gmail plūsmas) | klikšķi uz rakstiem | vienīgais maksas ceļš Discover plūsmā; kartītes formāts = raksta virsraksts + foto |
| **Display, mērķa CPM** | zīmols: sasniedzamība | lēti tūkstoši parādījumu, biežuma griesti, zīmola kreatīvs bez klikšķa spiediena |
| **Search, zīmola vaicājumi** | aizsardzība + lēti klikšķi | «tv3», «tv3 ziņas», «tv3 play» — augsts CTR, centi par klikšķi, konkurenti nevar nopirkt tavu vārdu |
| Search, vispārīgas ziņu frāzes | **nē** | «ziņas šodien» maksā dārgi un atved lasītāju, kas nāk vienreiz |
| Performance Max | **nē** | prasa konversijas ar vērtību; ziņu portālam nav pirkuma |
| YouTube video reach | vēlāk | prasa video YouTube kanālā (YouTube Data API) — lentes tur vēl nenonāk |

## 2. Budžeta arhitektūra

Viens dienas budžets, divi slāņi, divas platformas:

```
dienas budžets
├── zīmola daļa (brand_share %, noklusējums 20 %)
│   ├── Google zīmola meklēšana — vienmēr ieslēgta (brand_search_daily €, ~3 €)
│   └── zīmola franšīzes — sasniedzamība (atlikums, dalīts Google/Meta pēc google_share)
└── konversiju daļa (pārējais)
    ├── Google Demand Gen — raksti Discover plūsmā (google_share %, noklusējums 50 %)
    └── Meta boosts + variantu reklāmas (pārējais)
```

Katrs raksts saņem vismaz 5 € dienā (zem tā mācīšanās fāze nesākas), un
vienā platformā vienlaikus ir ne vairāk kā 6 klikšķu reklāmas. Ja abas
platformas nav pieslēgtas, viss iet uz to, kura ir.

## 3. Ko un kad reklamē — automātika

**Klikšķu reklāmas (traffic).** Kandidāti ir pēdējo 48 h Facebook ieraksti
ar saiti, foto vai karuseli — svaigas ziņas, kam boost vēl var dot lasītāju.
Veto secība: traģēdija/noziegums → politika un sabiedriskie jautājumi
(ES TTPA drošības tīkls, kas Google pusē ir vēl stingrāks — politiskās
reklāmas ES tur vairs nav vispār) → AI vērtējums «boostable» → režīms.
Tas pats raksts drīkst iet gan Meta, gan Google: platformas mēra atsevišķi.

**Zīmola franšīzes (awareness).** «Dienas TOP 3», «Nogales TOP 5», «Nedēļas
skaitlis», «Nedēļa 30 sekundēs» un pārējās franšīzes ir TV3.lv zīmola
saturs: tās nesola vienu rakstu, tās sola ieradumu. Tieši tāpēc tās ir
sasniedzamības kampaņu kreatīvs — Google Display ar mērķa CPM un 3
parādījumiem dienā vienam cilvēkam, Meta ar sasniedzamības mērķi. Vienā
platformā vienlaikus viena franšīzes kampaņa; jaunākā franšīze nomaina
iepriekšējo.

Atbilde uz jautājumu «vai TOP 5 kopsavilkumi ir zīmola atpazīstamība?»:
**saturs — jā, kampaņa — vēl ne.** Kopsavilkums pats par sevi ir zīmolu
veidojošs (tas māca: tv3.lv = vieta, kur vienā vietā ir svarīgākais), bet
atpazīstamības kampaņa tam ir vajadzīga trīs lietās, ko organiskais
ieraksts nedod: plaša auditorija ārpus sekotājiem, kontrolēts biežums un
mērs, kas nav klikšķi. To arī autopilots pieliek.

**Zīmola meklēšana (brand_search).** Viena Search kampaņa ar frāzes
atbilstības atslēgvārdiem («tv3», «tv3 ziņas», «tv3 play», «tv3.lv»), CPC
griesti 0,40 €, vienmēr ieslēgta. Tā nav «izaugsmes» kampaņa — tā ir
apdrošināšana pret konkurentu reklāmām uz tava zīmola un lētākie klikšķi,
kādi Google pusē vispār ir.

## 4. Kreatīvs

- Google Demand Gen prasa vienu attēlu trijās proporcijās (1.91:1, 1:1,
  4:5) un kvadrātisku logo — tos sistēma griež pati no raksta foto ar to
  pašu Chromium, kas zīmē kartītes.
- Virsraksti līdz 40 zīmēm (Display 30), apraksti līdz 90 — griezti pie
  vārda robežas no raksta virsraksta un AI reklāmas variantiem (tie paši
  trīs āķi, ko izmanto Meta variantu reklāma).
- Kampaņas nosaukums vienmēr sākas ar «TV3 Autopilots», lai kontā tās var
  atšķirt no aģentūras kampaņām un metrikas lasa tikai savas.
- Mērķauditorija: Latvija, latviešu valoda. Detalizētu interešu mērķēšanu
  Demand Gen dara pats; sākumā tai netraucējam.

## 5. Mērīšana un pārdale

- Katrai reklāmai ir savs `utm_content=a<id>` un `utm_source=google_paid`
  vai `facebook_paid`; GA4 sesijas pa reklāmām ienāk tāpat kā līdz šim, un
  rezultāts uz eiro ir sesijas (klikšķi, kamēr GA4 vēl klusē).
- Reizi dienā klikšķu reklāmas **katras platformas ietvaros** salīdzina
  savā starpā: virs vidējā +20 % budžeta, zem 35 % no vidējā — pauze. Google
  un Meta savā starpā nesalīdzina (CPC un CPM nav salīdzināmi), un zīmola
  kampaņas ar klikšķiem nemēra vispār.
- Zīmola kampaņu mērs: parādījumi un sasniedzamība par eiro (CPM), zīmola
  meklēšanas kampaņas parādījumu skaits kā zīmola pieprasījuma aizstājējs,
  un GA4 tiešā/zīmola plūsma pārskatā. Ja pēc mēneša zīmola meklēšanas
  parādījumi neaug, sasniedzamības kampaņas nestrādā — tad jāmaina kreatīvs,
  ne budžets.
- Pārskatā (Overview) Google kanāli («Paid Search», «Cross-network»,
  «Display») ir GA4 skatījums uz VISU Google naudu — arī aģentūras; mūsu
  pašu reklāmas rāda «mūsu reklāmas» bloks ar tēriņu no abām platformām.

## 6. Ieviešanas secība

1. **Dry-run** ar abām platformām pieslēgtām: nedēļu paskatīties plānu —
   vai kandidāti un franšīzes ir tie, ko gribētu redzēt reklāmā.
2. **Approve** režīms: zīmola meklēšana (3 €/dienā) un viena Demand Gen
   kampaņa dienā ar roku apstiprināt. Pārbaudīt GA4: vai `google_paid`
   sesijas parādās pa `a<id>`.
3. **Auto** ar 20–30 €/dienā, zīmola daļa 20 %, Google daļa 50 %. Pēc
   divām nedēļām paskatīties €/sesija Google pret Meta un ar `google_share`
   pārbīdīt naudu uz lētāko platformu — tas ir vienīgais regulators, kas
   jāgroza ar roku.
4. Nākamais slānis, kad lentes nonāks YouTube kanālā: video sasniedzamības
   kampaņas ar tām pašām franšīzēm (Shorts), tas pats budžeta slānis.

## 7. Aizsargi

- Nekad: politika, vēlēšanas, sabiedriskie jautājumi, traģēdijas, noziegumi
  (Meta konta bloķēšanas risks; Google ES aizliegums).
- Naudu tērē tikai režīmā `approve` vai `auto`, un tikai pieslēgtos kontos.
- Dienas budžeta griesti ir stingri: aktīvās reklāmas skaitās pret tiem,
  jauni kandidāti dala tikai atlikumu.
- Katra kampaņa top apturēta un tiek ieslēgta tikai pēc veiksmīgas
  izveides; neizdevusies palaišana ierakstu atzīmē kā noraidītu ar iemeslu.
