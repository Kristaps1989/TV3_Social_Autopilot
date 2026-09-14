"""Angļu paskaidrojumi Meta App Review pārbaudītājam.

Meta noraidīja pirmo iesniegumu ar «Screencast Not Aligned with Use Case
Details» un prasīja ievērot Screen Recording Guide: lietotnes valoda angļu,
paskaidrojumi pogām un saskarnes elementiem. Saskarne ir latviešu, jo to
lieto TV3 redakcija — tāpēc pārbaudītāja sesijā virs katras lapas parādās
angļu bloks, kas nosauc tieši to, kuru Graph API izsaukumu šī lapa lieto un
kur uz tās skatīties.

Panelis ir redzams TIKAI pārbaudītāja sesijā (auth.ROLE_REVIEWER), tāpēc
redakcijas ikdienas skats nemainās, un ekrāna ierakstā katrs elements ir
nosaukts angliski bez atsevišķas montāžas.
"""
from __future__ import annotations

# Ceļš (precīzs vai prefikss ar /) -> panelis. `permissions` parādās kā
# atzīmes, `points` — kur uz lapas skatīties.
GUIDES: list[tuple[str, dict]] = [
    ("/connect", {
        "title": "Accounts — connecting the Threads profile",
        "permissions": ["threads_basic"],
        "intro": ("This screen connects the tool to TV3's own Threads business "
                  "profile. It is the only place where an account is connected, "
                  "and only one Threads profile is ever used."),
        "points": [
            ("Card «Threads» → badge «savienots»",
             "«Connected». Shown when both the profile id and a valid access "
             "token are stored."),
            ("@tv3.lv next to the badge",
             "The username returned by GET /me?fields=id,username "
             "(threads_basic), called once right after the OAuth flow. It lets "
             "an editor confirm the tool is linked to the official TV3 profile "
             "and not to a personal account."),
            ("Button «Savienot ar Threads» / «Pārslēgt / atjaunot savienojumu»",
             "«Connect to Threads» / «Switch or refresh the connection». Starts "
             "the Threads OAuth flow at threads.net/oauth/authorize, where the "
             "user grants the permissions. Disabled in this read-only session."),
            ("«atslēga derīga līdz …»",
             "«Token valid until …» — the 60-day token is refreshed "
             "automatically."),
            ("Button «Atvienot»",
             "«Disconnect» — deletes the stored token. Disabled here."),
        ],
    }),
    ("/logs", {
        "title": "Diagnostics — one live API call per permission",
        "permissions": ["threads_basic", "threads_manage_insights",
                        "threads_read_replies"],
        "intro": ("Operational health of the tool. The Threads row runs the "
                  "permission checks on demand."),
        "points": [
            ("Button «Pārbaudīt Threads izsaukumus»",
             "«Check Threads API calls». Opens /logs/threads-check, which makes "
             "one real read call per permission against the latest published "
             "TV3 post — GET /me (threads_basic), GET /{media-id}/insights "
             "(threads_manage_insights) and GET /{media-id}/replies "
             "(threads_read_replies) — and shows the raw responses. It never "
             "publishes anything."),
            ("Button «Pielikt saiti atbildē»",
             "«Add the article link as a reply» — writes the one reply under "
             "the latest published post (threads_manage_replies). Disabled in "
             "this read-only session."),
            ("Row «Novecojuši kanāli» / «Attēlu renderētājs»",
             "«Outdated channels» / «Image renderer» — internal health rows, "
             "unrelated to the Meta API."),
        ],
    }),
    ("/stats", {
        "title": "Statistics — what the insights numbers are used for",
        "permissions": ["threads_manage_insights"],
        "intro": ("Views and likes collected with GET /{media-id}/insights are "
                  "aggregated per format and per posting hour. This is the "
                  "feedback signal the scheduler uses: formats and times that "
                  "perform poorly are scheduled less often."),
        "points": [
            ("Columns «Skatījumi» / «Klikšķi»",
             "«Views» / «Clicks». Views come from the Threads insights API; "
             "clicks come from our own link tracking, not from Meta."),
            ("Rows per «formāts» and «stunda»",
             "«Format» and «hour» — the planning dimensions these numbers "
             "feed."),
        ],
    }),
    ("/post/", {
        "title": "Post preview — the end-to-end use case",
        "permissions": ["threads_content_publish", "threads_manage_insights",
                        "threads_read_replies", "threads_manage_replies"],
        "intro": ("One news story prepared for TV3's Threads profile. This is "
                  "the screen an editor reviews before the post goes out, and "
                  "returns to after it is published."),
        "points": [
            ("The phone mock-up at the top",
             "Exactly the text and media that will be sent to "
             "POST /{user-id}/threads, then published with "
             "POST /{user-id}/threads_publish (threads_content_publish)."),
            ("Button «▶ Publicēt tagad»",
             "«Publish now» — publishes the post immediately instead of waiting "
             "for its scheduled time. Disabled in this read-only session."),
            ("Section «Threads: skatījumi un atbildes»",
             "«Threads: views and replies». Appears only for posts already "
             "published to Threads."),
            ("«👁 N skatījumi · ❤ N»",
             "«N views · N likes» — GET /{media-id}/insights "
             "(threads_manage_insights) for this post."),
            ("The list under it",
             "Replies to this post, GET /{media-id}/replies "
             "(threads_read_replies), shown next to the post so the editor sees "
             "reader questions and corrections while the story is current."),
            ("«Saite uz rakstu ir atbildē zem ieraksta ✓»",
             "«The article link is in a reply under the post» — the single "
             "reply the tool posts from the TV3 profile after publishing "
             "(threads_manage_replies)."),
            ("Button «Pielikt saiti atbildē»",
             "«Add the article link as a reply» — writes that reply if the post "
             "went out without one. Only ever once per post. Disabled here."),
        ],
    }),
    ("/", {
        "title": "Queue — stories prepared for the TV3 Threads profile",
        "permissions": ["threads_content_publish"],
        "intro": ("Each row is one tv3.lv story prepared for one channel, with "
                  "the time it is scheduled to publish. Open any row to see the "
                  "full preview."),
        "points": [
            ("Section heading per channel",
             "One block per connected channel. «Threads — tv3.lv» is TV3's "
             "Threads profile."),
            ("Table «Laiks» / «Ieraksts»",
             "«Time» / «Post» — when the post is due and its first line. The "
             "post text is a link: it opens the preview screen."),
            ("Link «priekšskatījums · labot»",
             "«Preview · edit» — expands an inline editor for the copy and "
             "links to the full preview."),
            ("Button «▶»",
             "«Publish now» (tooltip «Publicēt tagad»). Moves the post to the "
             "current time; the publisher picks it up within a minute and "
             "calls POST /{user-id}/threads_publish. The same action has a "
             "labelled button on the preview screen. Disabled here."),
            ("Button «✕»",
             "«Cancel» — removes the post from the queue. Disabled here."),
            ("Button «Apstiprināt»",
             "«Approve» — only on posts awaiting editorial approval; moves "
             "them into the scheduled queue. Disabled here."),
        ],
    }),
    ("/why", {
        "title": "Story lookup — preparing a post for a specific article",
        "permissions": ["threads_content_publish"],
        "intro": ("Search for one tv3.lv story and see what the tool decided "
                  "about it. An editor also uses this screen to prepare a post "
                  "for a story the automation did not pick up."),
        "points": [
            ("Search field + «Meklēt»",
             "«Search» — paste a tv3.lv article URL to look it up."),
            ("Table «Lēmumi»",
             "«Decisions» — why the story was or was not posted to each "
             "channel. Internal rules, no Meta API involved."),
            ("Block «Uztaisīt formātu ar roku»",
             "«Build a format manually». The editor picks a channel "
             "(threads_tv3lv is the TV3 Threads profile) and a post format, "
             "then presses «Uztaisīt» («Build»). This creates the draft that "
             "is later published with POST /{user-id}/threads and "
             "threads_publish. Disabled in this read-only session."),
        ],
    }),
]


def guide_for(path: str) -> dict | None:
    """Paskaidrojumu bloks šim ceļam (None, ja lapai tāda nav).

    Garākais sakrītošais ceļš uzvar, lai «/» nepārņemtu visas lapas.
    """
    best: tuple[int, dict] | None = None
    for prefix, guide in GUIDES:
        hit = path == prefix or (prefix != "/" and path.startswith(prefix))
        if prefix == "/" and path == "/":
            hit = True
        if hit and (best is None or len(prefix) > best[0]):
            best = (len(prefix), guide)
    return best[1] if best else None
