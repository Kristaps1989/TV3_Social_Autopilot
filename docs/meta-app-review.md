# Meta App Review (Threads) — kā iesniegt tā, lai apstiprina

## Ko Meta tiešām atbildēja (2026-09-14)

Noraidījums nav par aprakstiem. Katrai atļaujai atnāca viens un tas pats
iemesls:

> **Screencast Not Aligned with Use Case Details** — Developer Policy 1.6.
> «We have determined that your apps' use case is allowed, however, the
> submitted screencast fails to demonstrate the end-to-end experience of the
> use case described in the submission notes, hence the requested
> permission/feature is rejected.»

«Use case is allowed» nozīmē, ka teksti ir kārtībā. Iesniegtais video rādīja
tikai konta pieslēgšanu sadaļā Konti — tas ir viens solis no pieciem, ko Meta
prasa redzēt vienā ierakstā:

1. pilnu pieteikšanās plūsmu;
2. lietotāju, kas piešķir atļauju;
3. **atļaujas lietojumu no sākuma līdz beigām** (šī bija tā, kuras trūka);
4. angļu saskarnes valodu, parakstus un pogu nozīmes skaidrojumu;
5. ja lietotne strādā no servera vai ar system user token — to jāpasaka.

Tātad jālabo ir video, ne apraksti.

Visām piecām atļaujām atteikuma teksts ir **burtiski viens un tas pats** —
pārbaudīts katrā formā atsevišķi (threads_basic, threads_content_publish,
threads_manage_replies, threads_read_replies, threads_manage_insights). Viens
jauns ieraksts salabo visas piecas; atsevišķi video katrai nevajag.

## Divi punkti, kas formā palikuši neizpildīti

Katras atļaujas formā labajā pusē ir kontrolsaraksts. Divas rindas ir bez
ķeksīša, un abas bloķē iesniegšanu:

- **«Upload screencast showing the end-to-end user experience»** — jaunais
  video (scenārijs zemāk).
- **«Agree that you will comply with allowed usage»** — rūtiņa *«If approved,
  I agree that any data I receive through … will be used in accordance with
  the allowed usage»* formas apakšā. Tā ir neatzīmēta **visām piecām**
  atļaujām. Bez tās iesniegums neaiziet, lai cik labs būtu video. Atzīmē to
  katrā formā, pirms spied Save.

Pārējās divas rindas ir zaļas: API izsaukumi ir izdarīti («Completed»), un
atļauju savstarpējās prasības (threads_basic priekš pārējām) ir izpildītas.

## Sīkums, ko vērts salabot aprakstos

`threads_basic` apraksts sākas ar «Threads_Autopilot is an internal editorial
publishing tool…». Lietotne tagad saucas **TV3 Social Autopilot**. Nosaukums
aprakstā vairs nesakrīt ne ar lietotni, ne ar video; nomaini to. Pārējos
četros aprakstos nosaukums neparādās.

## Angļu valoda — kā atrisināts

Saskarne ir latviešu, jo to lieto TV3 redakcija. Pārsaukt visu angliski nozīmē
pasliktināt rīku īstajiem lietotājiem. Tā vietā pārbaudītāja sesijā virs katras
lapas ir **angļu paskaidrojumu bloks**: lapas nosaukums, atļauju atzīmes un
saraksts «šī poga latviski → ko tā nozīmē un kuru Graph API izsaukumu lieto».
To redz tikai pārbaudītājs (Konti → Pārbaudītāja pieeja), un ekrāna ierakstā
tas nozīmē, ka katrs elements ir nosaukts angliski bez atsevišķas montāžas.

Video tāpat jāieraksta ar **angļu parakstiem vai balsi** — panelis ir papildus,
ne vietā.

## Pirms iesniegšanas (Meta lietotnē)

- **Settings → Basic → App Domains:** `tv3.lv` **un**
  `tv3socialautopilot-production.up.railway.app`.
- **Settings → Basic → Website → Site URL:**
  `https://tv3socialautopilot-production.up.railway.app/`.
- **Privacy Policy URL / Terms:** tv3.lv privātuma politikas lapa (jāatveras
  bez pieteikšanās). **Data deletion:** callback jau ir
  (`/connect/threads/delete`), var norādīt arī to.
- **Business verification:** Threads atļauju *advanced access* prasa
  verificētu uzņēmumu (Business Settings → Security Centre). Bez tā
  iesniegums var būt «approved» uz papīra, bet atļaujas paliek standarta
  piekļuvē. Verificē SIA ar reģistrācijas dokumentu.
- **App name** nedrīkst saturēt «Threads»: `Threads_Autopilot` → piem.
  `TV3 Social Autopilot`.
- **Konti → Pārbaudītāja pieeja:** uzliec paroli, ieraksti to formā. Pēc
  apstiprinājuma dzēs.
- **Diagnostika → «Pieņemt koda vērtības»** noteikumiem, lai serverī ir
  `threads_link_in_reply: true` (citādi video nerādīs atbildi).

## Web reviewer instructions (ielīmēt formā)

```
WHAT THE APP IS
TV3 Social Autopilot is a private, internal editorial tool operated by TV3
Group Latvia (a national broadcaster). It publishes TV3's own news stories
from https://tv3.lv/ to TV3's own Threads profile (@tv3.lv) and reads back
the views and replies of those posts. There is no public sign-up and no
other Threads profile is ever accessed.

The tool itself (the interface you will review) is hosted at:
https://tv3socialautopilot-production.up.railway.app/
The news content comes from tv3.lv, which the company owns; that is why the
app domain is tv3.lv while the tool runs on a separate host.

HOW TO LOG IN
1. Open https://tv3socialautopilot-production.up.railway.app/login
2. Password: <REVIEWER PASSWORD>
   This is a read-only reviewer account: every screen is visible, but
   publishing, disconnecting accounts and changing settings are disabled,
   so you cannot post to the live TV3 profile by accident. A yellow banner
   at the top confirms the reviewer session.

NOTE ON WRITING ACTIONS
The reviewer account is read-only on purpose: it cannot publish to the live
TV3 profile, disconnect the account, or change settings. Those controls are
visible but greyed out, and clicking one shows a short explanation instead of
doing anything. In particular, the "Connect to Threads" button on the
Accounts screen is disabled, because starting the OAuth flow would replace
TV3's live connection. The profile is already connected, and the complete
authorisation flow — the Threads consent screen listing all five permissions
— is recorded in the screencast attached to this submission.

NOTE ON LANGUAGE
The interface is in Latvian because it is used by the TV3 newsroom in Riga.
For this review, every page you open in the reviewer session shows an
English panel directly under the yellow banner: it names the screen, lists
the permissions it uses, and explains each Latvian button and element
together with the exact Graph API call behind it. No translation tool is
needed.

WHERE EACH PERMISSION IS USED
threads_basic
  Menu "Konti" (Accounts) → card "Threads". The badge "savienots"
  (connected) and the username @tv3.lv next to it come from
  GET /me?fields=id,username, called right after the OAuth flow and shown
  here so editors can verify the tool is connected to the official profile.

threads_content_publish
  Menu "Rinda" (Queue) lists the posts scheduled for the TV3 Threads
  profile. Open any post → "Priekšskatījums" (preview) shows exactly the
  text and media that go out. At the scheduled time the app creates a
  container (POST /{user-id}/threads) and publishes it
  (POST /{user-id}/threads_publish). Published posts are listed under
  "Publicēts" with a link to the live thread.

threads_manage_replies
  After a post is published, the app writes ONE reply under it, from the
  same TV3 profile, containing the link to the full article
  (POST /{user-id}/threads with reply_to_id, then threads_publish). On the
  preview page of any published Threads post the section
  "Threads: skatījumi un atbildes" shows "Saite uz rakstu ir atbildē" once
  that reply exists; the button "Pielikt saiti atbildē" writes it for a
  post that went out without one (disabled in the reviewer session).

threads_read_replies
  Same section on the preview page: the list under the view count is the
  replies to that post (GET /{media-id}/replies), shown to the editor next
  to the post so reader questions and corrections are seen in time.

threads_manage_insights
  Same section: "👁 N skatījumi · ❤ N" is GET /{media-id}/insights
  (views, likes) for the post. Menu "Statistika" aggregates the same
  numbers per format and posting hour to plan future posts.
  Menu "Diagnostika" → "Pārbaudīt Threads izsaukumus" runs one live read
  call per permission (profile, insights, replies) and shows the raw
  responses.

The screencast attached shows the same path: login → Konti (OAuth flow and
connected profile) → Rinda → a post preview → the published thread on
threads.net with the reply under it → back to the preview with views and
replies → Diagnostika.
```

## Screencast — viens ieraksts, ~4 min, bez montāžas

Viens fails visām piecām atļaujām; Meta to pieņem vairākās vietās. Ieraksti
1920×1080, peles kustības lēnas, katrā solī 3–5 sekundes pauze, lai
pārbaudītājs paspēj izlasīt. Angļu paraksti (vai balss) obligāti.

**0. Sākums (10 s).** Rādi pārlūka adreses joslu ar
`tv3socialautopilot-production.up.railway.app`. Paraksts: *«Internal editorial
tool of TV3 Group Latvia. It publishes tv3.lv news to TV3's own Threads
profile.»*

**1. Pieteikšanās (20 s) — prasība nr. 1.** Atver `/login`, ieraksti paroli,
ienāc. Paraksts: *«The tool has its own login. This is not a Meta login —
Meta authentication happens in step 2.»* Neizlaid šo soli: Meta prasa redzēt
«the complete login flow» arī tad, ja tā ir lietotnes pašas parole.

**2. OAuth un atļauju piešķiršana (40 s) — prasība nr. 2.** Konti → kartīte
Threads → «Pārslēgt / atjaunot savienojumu». Rādi **visu** Threads logu:
kontu izvēli, **atļauju sarakstu ar visiem pieciem nosaukumiem** un pogu
«Allow». Šis ekrāns ir tas, ko Meta sauc par «a user granting app access»;
apstājies uz tā vismaz 5 sekundes, lai atļaujas ir salasāmas. Atgriezies
lietotnē — rādi «savienots · @tv3.lv». Paraksts: *«GET /me returns the
username, shown here so the editor can confirm the official TV3 profile.»*
→ **threads_basic**

**3. Ieraksta sagatavošana un publicēšana (60 s) — prasība nr. 3.** Rinda →
atver ieplānotu Threads ierakstu → priekšskatījums. Rādi tekstu un attēlu,
paraksts: *«Exactly what will be sent to POST /{user-id}/threads.»* Nospied
«▶ Publicēt tagad». Atgriezies rindā — statuss «publicēts».
→ **threads_content_publish**

**4. Ieraksts dzīvajā profilā (30 s).** Atver `threads.net/@tv3.lv` jaunā cilnē.
Rādi tikko publicēto ierakstu **un zem tā tv3.lv atbildi ar raksta saiti**.
Paraksts: *«After publishing, the app posts exactly one reply from the same
profile containing the link to the full article.»*
→ **threads_manage_replies**

**5. Skatījumi un atbildes redakcijā (50 s).** Atpakaļ ieraksta
priekšskatījumā, ritini līdz sadaļai «Threads: skatījumi un atbildes». Rādi
skaitļus un atbilžu sarakstu. Paraksts: *«GET /{media-id}/insights gives views
and likes; GET /{media-id}/replies gives the replies, shown to the editor next
to the post.»* Ja atbilžu vēl nav, atver vecāku ierakstu, kam tās ir — tukšs
saraksts šo soli neapliecina.
→ **threads_manage_insights**, **threads_read_replies**

**6. Kam skaitļi der (20 s).** Statistika → rādi formātu un stundu rindas.
Paraksts: *«The same numbers are aggregated per format and hour; formats that
perform poorly are scheduled less often.»* Tas ir «adds value for a person
using your app», ko prasa apraksta lauks.

**7. Pārbaude (20 s).** Diagnostika → «Pārbaudīt Threads izsaukumus» → rādi
JSON ar `ok: true` katrai atļaujai.

Ierakstu taisi ar **administratora** paroli (pārbaudītāja sesijā pogas ir
slēgtas). Pārbaudītāja paroli lieto tikai formā — lai viņš pats var staigāt
pa lapām.

## Kas jāpasaka par pieteikšanās plūsmu (prasība nr. 5)

Iesnieguma lauka beigās katrai atļaujai pievieno šo rindkopu:

```
Note on the authentication flow: this app uses the Threads API with its own
Threads app credentials, so the authorization screen in the screencast is the
Threads OAuth flow at threads.net/oauth/authorize, not the Facebook Login
dialog. Facebook Login is not integrated. The app is not server-to-server and
does not use a system user token: the access token is obtained through the
user-granted OAuth flow shown in the recording and refreshed automatically
every 60 days. The tool's own password login shown at the start of the
recording is our application's login, unrelated to Meta.
```

Tas tieši atbild uz punktu, kurā Meta prasa pateikt, ja «frontend Meta login
authentication flow is not visible».

## Data handling (atbildes)

- **Data processors:** No. (Railway ir hostings; ja Meta jautā par
  hostinga pakalpojumu, tad: Railway Corp., USA — hosting only.)
- **Responsible entity:** juridiskais nosaukums no Uzņēmumu reģistra, piem.
  «SIA "All Media Latvia" (TV3 Group Latvia)» — pārbaudi precīzo formu.
  Country: Latvia.
- **Public authority requests:** No. Policies: «Required review of the
  legality of these requests.»

## Atļauju apraksti — kas jāmaina pret pirmo iesniegumu

Meta tos pieņēma («use case is allowed»), tāpēc pārrakstīt nevajag. Divas
rindas jāpielabo, lai tie atbilst tam, ko jaunais video rāda, un katram
beigās jāpieliek rindkopa par pieteikšanās plūsmu (augstāk):

- `threads_read_replies`: «…shows them to the editor on the post's preview
  page in the section "Threads: skatījumi un atbildes" (Threads: views and
  replies)…»
- `threads_manage_insights`: «…views and likes are shown on the post's preview
  page and aggregated on the "Statistika" (Statistics) page…»

## Iesniegšanas secība

1. Deploy, lai serverī ir angļu paskaidrojumu panelis.
2. Uzliec pārbaudītāja paroli (Konti → Pārbaudītāja pieeja).
3. Ieraksti video pēc scenārija augstāk (ar administratora paroli).
4. Katrai no piecām atļaujām formā:
   - pieliec aprakstam rindkopu par pieteikšanās plūsmu;
   - `threads_basic` arī nomaini lietotnes nosaukumu;
   - augšupielādē **to pašu** video;
   - **atzīmē rūtiņu «If approved, I agree…»**;
   - Save.
5. Reviewer instructions — teksts augstāk, ar īsto pārbaudītāja paroli.
6. Submit. Atbilde parasti 3–5 darba dienās.
