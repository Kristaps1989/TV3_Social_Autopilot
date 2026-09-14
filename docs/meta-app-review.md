# Meta App Review (Threads) — kā iesniegt tā, lai apstiprina

Pirmais iesniegums (2026-09-13) neizgāja. No paša iesnieguma redzams, kāpēc
pārbaudītājs nevarēja pabeigt darbu, pat ja atteikuma teksts nav pie rokas:

1. **Pārbaudītājs nevarēja ienākt lietotnē.** Datu panelis ir aiz paroles, bet
   laukā «test credentials» bija `n/a`. Meta pārbauda, lietojot lietotni pats —
   bez pieteikšanās viņš redz tikai login formu.
2. **Instrukcijas bez soļiem.** «Web reviewer instructions» aprakstīja, kas
   lietotne ir, bet ne *kur klikšķināt*, lai redzētu katru atļauju darbībā.
3. **Video nesakrita ar aprakstu.** `threads_manage_replies` aprakstā teikts,
   ka pēc katra ieraksta top atbilde ar saiti; noteikumos tas bija izslēgts
   (`threads_link_in_reply: false`), tāpēc video to nevarēja parādīt.
   `threads_read_replies` solīja atbildes «tajā pašā ekrānā, kur ierakstu
   apstiprināja» — tāda ekrāna nebija (tikai diagnostikas JSON).
4. **Data handling atbilde** («We have postion DPO who would prepreare…») —
   ar kļūdām un bez juridiskā nosaukuma. Meta to lasa.
5. **Domēns.** Lietotnes domēns ir tv3.lv, bet pārbaudāmā saskarne ir
   `tv3socialautopilot-production.up.railway.app`. Ja Settings → Basic →
   App Domains nesatur arī Railway domēnu, pārbaudītājam adrese izskatās pēc
   svešas lietotnes.

Kods tagad dod visu, ko instrukcijas sola: pārbaudītāja paroli (Konti →
«Pārbaudītāja pieeja»), lietotājvārdu pie Threads «savienots», skatījumus un
atbildes ieraksta priekšskatījumā ar pogu «Pielikt saiti atbildē», un
`threads_link_in_reply: true` pēc noklusējuma.

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

## Video (viens ieraksts, ~3 min, bez montāžas)

1. Ienāc ar **administratora** paroli (video jārāda īstā darbība).
2. **Konti → Threads → «Pārslēgt / atjaunot savienojumu»** — parādi visu
   OAuth logu līdz atgriešanai ar «savienots · @tv3.lv». (threads_basic)
3. **Rinda** → atver ieplānotu Threads ierakstu → priekšskatījums →
   «▶ Publicēt tagad». (threads_content_publish)
4. Atver **threads.net/@tv3.lv** — ieraksts redzams, zem tā tv3.lv atbilde
   ar saiti. (threads_manage_replies)
5. Atpakaļ ieraksta priekšskatījumā: sadaļa «Threads: skatījumi un
   atbildes» ar skatījumiem un atbilžu sarakstu. (threads_manage_insights,
   threads_read_replies) Ja atbilžu vēl nav, atver ierakstu, kam tās ir.
6. **Diagnostika → «Pārbaudīt Threads izsaukumus»** — JSON ar trim `ok: true`.

Katrai atļaujai formā var pievienot to pašu video; Meta pieņem vienu failu
vairākās vietās.

## Data handling (atbildes)

- **Data processors:** No. (Railway ir hostings; ja Meta jautā par
  hostinga pakalpojumu, tad: Railway Corp., USA — hosting only.)
- **Responsible entity:** juridiskais nosaukums no Uzņēmumu reģistra, piem.
  «SIA "All Media Latvia" (TV3 Group Latvia)» — pārbaudi precīzo formu.
  Country: Latvia.
- **Public authority requests:** No. Policies: «Required review of the
  legality of these requests.»

## Atļauju apraksti — kas jāmaina pret pirmo iesniegumu

Apraksti kā tādi bija labi. Divas rindas jāpielabo, lai tie atbilst tam,
ko video rāda:

- `threads_read_replies`: «…shows them to the editor on the post's preview
  page in the section "Threads: skatījumi un atbildes"…»
- `threads_manage_insights`: «…views and likes are shown on the post's
  preview page and aggregated on the "Statistika" page…»
