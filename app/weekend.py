"""Satura franšīzes: nosaukti, atkārtojami formāti fiksētos laikos, kas
uzbūvēti no jau IZMĒRĪTĀ satura — bez papildu redakcijas darba.

Franšīze veido ieradumu: lasītājs atgriežas pie formāta, ko pazīst un gaida.
Katram formātam savs slēdzis (Pārskata lapā), savs `hook_type` marķieris
mērīšanai un sava diena nedēļas režģī:

  Pr  monday       — «Nedēļas nogales TOP 5» karuselis (08:00) + stāsts (08:30)
  Ot  number       — «Nedēļas skaitlis»: viens skaitlis uz kartes (12:00)
  Tr  question     — «Trešdienas jautājums»: formāts, kas mērķē uz sarunu (19:00)
  Ce  yearago      — «Šajā dienā pirms gada»: arhīvs ar gadadienas āķi (15:00)
  Pk  guide        — «Nedēļas nogales gids»: izklaides izlase brīvdienām (17:00)
  Se  top5         — «Nedēļas sports: 5 svarīgākie» karuselis (10:00)
      reel         — «Nedēļa 30 sekundēs» slaidrādes reels (12:00)
      icymi        — nepamanītais stāsts: labs raksts, kam klājās vāji (15:00)
  Sv  evergreen    — arhīva raksts, ko joprojām lasa (09:00)
      top5         — kopējais «Nedēļas TOP 5» karuselis (10:00)
      quiz         — nedēļas QUIZ nedēļas lielākajā logā (19:00)
  Pr–Pk daily_story — «Dienas TOP 3» foto mozaīkas stāsts (20:00), stāstu
                      kanālā: atsevišķa auditorija, nulle konkurences ar plūsmu

Maksimums viena plūsmas franšīze darba dienā + vakara stāsts ārpus plūsmas —
pārstrādātais saturs paliek zem ~20% no plūsmas arī klusā ziņu dienā.

Valodas disciplīna: visos tekstos datumi ir absolūti («26. augustā»), nekad
relatīvi — vecs ieraksts, ko Facebook izceļ pēc mēneša, nedrīkst maldināt.
Grafikās datums ir iededzināts (cards.date_chip).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app import config
from app.models import Article, Post, PostMetrics, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

FEATURES = ("top5", "reel", "icymi", "quiz", "evergreen", "monday",
            "daily_story", "guide", "question", "yearago", "number")
CHANNEL = "fb_tv3lv"
STORY_CHANNEL = "fb_stories"   # story formāts dzīvo savā kanālā (savi limiti)

# Latviešu datumu vārdi — lokatīvs («26. augustā»)
MONTHS_LOC = ["", "janvārī", "februārī", "martā", "aprīlī", "maijā", "jūnijā",
              "jūlijā", "augustā", "septembrī", "oktobrī", "novembrī", "decembrī"]

# Relatīvie laika vārdi, kas novecojušā ierakstā melo — AI ģenerētos tekstos
# tos aizstājam ar absolūto datumu vai ierakstu atmetam.
RELATIVE_WORDS = ("vakar", "šodien", "šorīt", "šovakar", "rīt", "parīt",
                  "aizvakar", "šonedēļ", "pagājušonedēļ", "nupat", "tikko")


# Vārdi, kas rakstu izslēdz no IZKLAIDĒJOŠAJIEM formātiem (kvīzs, jautājums,
# «nedēļas skaitlis»). Bojāgājušo skaits nav kvīza jautājums, un traģēdija nav
# spēle — arī tad, ja tieši šo rakstu lasīja visvairāk. AI jutīguma birkas
# («tragedy», «crime») ir pirmā aizsardzība, šī saraksta ir otrā: ārzemju
# katastrofu AI dažkārt atzīmē kā parastu ziņu.
GRIM_STEMS = (
    "bojā gāj", "bojāgājuš", "gāja bojā", "gājuši bojā", "upuri", "upuru",
    "dzīvīb", "mirus", "miruš", "nāve", "nāves", "nāvējoš", "pašnāvīb",
    "cietuš", "ievainot", "avārij", "sadursm", "katastrof", "traģēdij",
    "slepkav", "uzbruk", "vardarb", "izvaroš", "terora", "terorist",
    "karš", "karā", "kara ", "plūdi", "plūdos", "plūdu", "zemestrīc",
    "ugunsgrēk", "sprādzien", "nogruvum", "pazudis", "pazuduš",
)


# Kvīza jautājumam jābūt par NOSLĒGTU faktu. «Kas nepieciešams izlasei, lai
# 28. augustā tiktu uz Pasaules kausu» divas dienas vēlāk ir bezjēdzīgs: spēle
# jau ir aizvadīta, situācija mainījusies. Kvīzs iet ēterā svētdienas vakarā un
# plūsmā dzīvo dienām — atvērtiem jautājumiem tur nav vietas.
OPEN_ENDED_STEMS = (
    "nepiecieš", "vajadzīg", "kas jādara", "jāizdara", "lai tiktu",
    "lai iekļūtu", "lai kvalificētos", "izdosies", "vai spēs", "spēs ",
    "prognoz", "varētu ", "plāno ", "gaidām", "nākotn", "nākamaj", "cerīb",
    "turpmāk", "vai uzvarēs", "kad notiks", "vai notiks", "kas sagaida",
)


def open_ended(text: str) -> str:
    """Pirmais atrastais «vēl nezināms iznākums» vārds, vai tukša virkne."""
    low = (text or "").lower()
    return next((st for st in OPEN_ENDED_STEMS if st in low), "")


def grim_words(text: str) -> str:
    """Pirmais atrastais «smagais» vārds tekstā, vai tukša virkne."""
    low = (text or "").lower()
    return next((st for st in GRIM_STEMS if st in low), "")


def playful_safe(article) -> bool:
    """Vai par šo rakstu drīkst taisīt kvīzu, sarunas jautājumu vai «nedēļas
    skaitli». Traģēdijas un noziegumi izklaidējošos formātos nenonāk nekad —
    ne tāpēc, ka to aizliegtu platforma, bet tāpēc, ka tā ir necieņa pret
    cilvēkiem, par kuriem raksts ir, un tiešs zīmola risks."""
    if any(s in ("tragedy", "crime") for s in (article.sensitivity or [])):
        return False
    return not grim_words(article.title or "")


def lv_date(dt: datetime) -> str:
    """«26. augustā» — bez gada, ja tas ir šis gads."""
    base = f"{dt.day}. {MONTHS_LOC[dt.month]}"
    return base if dt.year == utcnow().year else f"{base} ({dt.year}. gads)"


def lv_date_full(dt: datetime) -> str:
    """«2025. gada 3. septembrī» — arhīva ierakstiem, kur gads ir daļa no
    stāsta («pirms gada»), nevis iekavās pieliktā atruna."""
    return f"{dt.year}. gada {dt.day}. {MONTHS_LOC[dt.month]}"


def week_start(now: datetime | None = None) -> datetime:
    """Šīs nedēļas pirmdienas 00:00 pēc Rīgas laika, kā UTC naive laiks —
    Latvijā nedēļa ir pirmdiena–svētdiena, tāpēc «nedēļas TOP» skaita tikai
    šo kalendāro nedēļu, nevis ritošās 7 dienas."""
    now = now or utcnow()
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def has_relative_words(text: str) -> str:
    low = f" {text.lower()} "
    for w in RELATIVE_WORDS:
        if f" {w}" in low:
            return w
    return ""


# --- slēdži ---------------------------------------------------------------

def settings(session) -> dict[str, bool]:
    return {f: get_setting(session, f"weekend:{f}", "on") == "on"
            for f in FEATURES}


def save_settings(session, enabled: dict[str, bool]) -> None:
    for f in FEATURES:
        set_setting(session, f"weekend:{f}", "on" if enabled.get(f) else "off")


def _ran_key(feature: str, day) -> str:
    return f"weekend:ran:{feature}:{day.isoformat()}"


# --- nedēļas dati ---------------------------------------------------------

def day_start(now: datetime | None = None) -> datetime:
    """Šodienas 00:00 pēc Rīgas laika (UTC naive) — dienas TOP logam."""
    now = now or utcnow()
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def weekend_start(now: datetime | None = None) -> datetime:
    """Tikko beigušās nedēļas nogales sestdienas 00:00 (Rīgas laiks, UTC
    naive). Pirmdienas rīta apkopojumam: viss, kas publicēts no sestdienas."""
    return week_start(now) - timedelta(days=2)


def week_top(session, section: str | None = None,
             limit: int = 5, max_age_days: int | None = None,
             now: datetime | None = None,
             since: datetime | None = None) -> list[Article]:
    """Perioda (noklusēti — šīs kalendārās nedēļas no pirmdienas) raksti ar
    visvairāk izmērītajām sesijām (GA4 caur post_metrics); kamēr metriku
    nav, krīt atpakaļ uz AI vērtējumu."""
    from app import runtime

    since = since or week_start(now)
    q = (select(Article,
                func.coalesce(func.sum(PostMetrics.ga_sessions), 0).label("s"),
                func.coalesce(func.sum(PostMetrics.clicks), 0).label("c"))
         .join(Post, Post.article_id == Article.id)
         .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
         .where(Post.state == "published", Post.published_at >= since,
                Article.title != "")
         .group_by(Article.id))
    if not runtime.is_dry_run():
        # sausās skrējiena ieraksti nekur neaizgāja — tie nav «nedēļas TOP».
        # (Pašā dry-run režīmā tos paturam, citādi franšīzes testējot klusē.)
        q = q.where(Post.dry_run.is_(False))
    if section:
        q = q.where(Article.section == section)
    if max_age_days:
        q = q.where(Article.published_at
                    >= utcnow() - timedelta(days=max_age_days))
    rows = session.execute(q).all()
    scored = sorted(rows, key=lambda r: (-(r.s or 0), -(r.c or 0),
                                         -(r.Article.ai_score or 0)))
    seen, out = set(), []
    for r in scored:
        if r.Article.id in seen:
            continue
        seen.add(r.Article.id)
        out.append(r.Article)
        if len(out) >= limit:
            break
    return out


def _digest_article(session, guid: str, title: str, section: str,
                    url: str) -> Article:
    """Sintētisks raksts digest ierakstam (Post prasa article_id)."""
    existing = session.execute(
        select(Article).where(Article.guid == guid)).scalars().first()
    if existing:
        return existing
    a = Article(guid=guid, url=url, canonical_url=url, title=title,
                section=section, editor_status="can", decided_at=utcnow(),
                published_at=utcnow(), raw_json={"_digest": True})
    session.add(a)
    session.flush()
    return a


def _schedule(session, article: Article, fmt: str, copy: str, media: list,
              link: str, marker: str, at: datetime,
              card_links: list[str] | None = None,
              card_titles: list[str] | None = None,
              channel: str = CHANNEL,
              recipe: dict | None = None) -> Post:
    from app import cards, runtime

    # Franšīzes ieraksti ir ATSKATOŠI pēc būtības: «nedēļas TOP», «nedēļas
    # skaitlis», kvīzs. Tie atsaucas uz nedēļas rakstiem, un tāds raksts
    # publicēšanas brīdī ir dienas vecs pēc plāna, ne aiz nolaidības. Svaiguma
    # sargs (pipeline.stale_now) tos tāpēc izlaiž — citādi tas atceltu visu
    # nedēļas nogales programmu.
    extra: dict = {"timeless": True}
    if media:
        extra["render_version"] = cards.RENDER_VERSION
    if recipe:
        # recepte = no kā grafika bija uzbūvēta, lai redaktors to var
        # pārģenerēt (app.regenerate) bez ieraksta atcelšanas
        extra["recipe"] = recipe
    if card_links:
        extra["card_links"] = card_links
    if card_titles:
        extra["card_titles"] = card_titles
    post = Post(article_id=article.id, channel=channel, format=fmt, copy=copy,
                media=media, link_url=link, hook_type=marker,
                state="scheduled", scheduled_at=at, extra=extra,
                dry_run=runtime.is_dry_run(session))
    session.add(post)
    session.flush()
    return post


def _local_slot(day, hour: int) -> datetime:
    tz = ZoneInfo(config.TIMEZONE)
    local = datetime(day.year, day.month, day.day, hour, 0,
                     tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _clean_image(article: Article) -> str:
    """Pirmais TĪRAIS raksta foto — photopost grafikas ar iestrādātu
    virsrakstu mozaīkā un zem mūsu teksta dublētos, tāpēc tās izlaižam;
    ja rakstam cita attēla nav, atgriež '' (kadrs paliek gradienta/tukšā
    variantā, nevis ar svešu tekstu fonā)."""
    from app.pipeline import prebranded

    return next((img for img in (article.images or [])
                 if img and not prebranded(img)), "")


def _any_image(article: Article) -> str:
    """Pirmais raksta attēls neatkarīgi no veida — der TIKAI izpludinātam
    fonam (cards.point_blur), kur iestrādātais virsraksts vairs nav
    salasāms. Daudziem tv3.lv rakstiem cita attēla par photopost grafiku
    vienkārši nav, un plakana krāsas kartīte plūsmā zaudē."""
    return next((img for img in (article.images or []) if img), "")


def _point_line(i: int, article: Article) -> str:
    dt = article.published_at or article.first_seen_at
    date = lv_date(dt) if dt else ""
    title = article.title.rstrip(".")
    return f"{title} ({date})" if date else title


def _ai_lines(session, prompt: str, max_tokens: int = 400,
              model: str | None = None) -> list[str]:
    """Īss AI izsaukums, kas atgriež tīras teksta rindas (vai tukšu sarakstu).
    Bez AI atslēgas vai pie jebkuras kļūmes formāts vienkārši izlaižas —
    franšīze nekad negāž pārējo grafiku."""
    from app import credentials

    api_key = credentials.get("anthropic_api_key", session)
    if not api_key:
        return []
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model or config.AI_MODEL_STRONG, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:  # noqa: BLE001
        log.warning("AI call failed: %s", e)
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# --- formāti --------------------------------------------------------------

def _photo_stats(images: list[str], blurs: list[str]) -> dict:
    """Cik kartītēm sanāca īsts foto — priekšskatījumā redaktors uzreiz redz,
    vai plakanās kartītes ir datu problēma (rakstiem nav tīra foto) vai
    renderētāja problēma."""
    return {"total": len(images),
            "clean": sum(1 for i in images if i),
            "blurred": sum(1 for i, b in zip(images, blurs) if not i and b)}


def _carousel_digest(session, day, articles: list[Article], title: str,
                     sec: str, link: str, copy: str, guid: str, marker: str,
                     hour: int, tag: str = "#TOP5",
                     ribbon: str = "") -> Post | None:
    """Kopīgais TOP karuseļa celtnieks.

    Facebook karuselī ir tikai 5 kartītes, tāpēc vāka un CTA kartītes te nav:
    piecas kartītes = pieci stāsti, katrs ar savu foto, savu virsrakstu FB
    teksta joslā un savu saiti. Agrāk vāks + CTA aizņēma divas vietas, FB
    nogrieza pārpalikumu, un no «TOP 5» reāli redzami palika trīs stāsti.
    Ievadu nes paša ieraksta teksts, aicinājumu — pēdējās kartītes saite."""
    from app import cards

    used = articles[:5]
    # virsraksts un datums iet atsevišķi: kartītē datums ir sava maza rinda
    points = [a.title.rstrip(".") for a in used]
    point_dates = [lv_date(a.published_at or a.first_seen_at)
                   if (a.published_at or a.first_seen_at) else "" for a in used]
    # katrai kartītei SAVA raksta bilde; photopost grafikas izlaižam, jo tām
    # ir iestrādāts virsraksts, kas dublētos ar mūsu tekstu
    images = [_clean_image(a) for a in used]
    blurs = [("" if img else _any_image(a)) for img, a in zip(images, used)]
    try:
        media = cards.render_cards(
            title, sec, tag, points, "", "",
            date_txt=day.strftime("%d.%m.%Y"), point_images=images,
            point_blur=blurs, point_dates=point_dates,
            include_cover=False, include_end=False,
            label=ribbon or title.upper())
    except Exception as e:  # noqa: BLE001
        log.warning("%s render failed: %s", marker, e)
        return None
    if not media:
        return None
    card_links = [a.canonical_url or a.url for a in used][:len(media)]
    card_titles = [a.title for a in used][:len(media)]
    return _schedule(session, _digest_article(session, guid, title, sec, link),
                     "card_carousel", copy, media, link, marker,
                     _local_slot(day, hour),
                     card_links=card_links, card_titles=card_titles,
                     recipe={"kind": "cards", "title": title, "section": sec,
                             "tag": tag, "ribbon": ribbon or title.upper(),
                             "articles": [a.id for a in used],
                             "dates": point_dates,
                             "date": day.strftime("%d.%m.%Y"),
                             "photos": _photo_stats(images, blurs)})


def build_top5(session, day, section: str | None) -> Post | None:
    from app import cards

    articles = week_top(session, section=section)
    if len(articles) < 3 or not cards.renderer_available():
        return None
    sec = section or "news"
    label = {"sport": "Nedēļas sports", None: "Nedēļas TOP",
             "news": "Nedēļas TOP"}.get(section, "Nedēļas TOP")
    title = f"{label}: 5 svarīgākie notikumi"
    link = ("https://tv3.lv/sports" if section == "sport" else "https://tv3.lv")
    local_now = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    week_from = lv_date(local_now - timedelta(days=local_now.weekday()))
    week_to = lv_date(local_now)
    copy = (f"{label} — pieci notikumi, par kuriem visvairāk lasīja tv3.lv "
            f"({week_from} – {week_to}). Pilnie stāsti portālā.")
    return _carousel_digest(session, day, articles, title, sec, link, copy,
                            f"digest-top5-{sec}-{day.isoformat()}", "digest", 10,
                            ribbon=("NEDĒĻAS SPORTA TOP 5" if section == "sport"
                                    else "NEDĒĻAS TOP 5"))


def build_monday_top5(session, day, now: datetime | None = None) -> Post | None:
    """Pirmdienas rīta karuselis: nedēļas nogales (sestdiena–svētdiena)
    svarīgākais tiem, kas brīvdienās ziņām nesekoja."""
    from app import cards

    articles = week_top(session, since=weekend_start(now))
    if len(articles) < 3 or not cards.renderer_available():
        return None
    title = "Nedēļas nogales TOP 5"
    sat = weekend_start(now).replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    sun = sat + timedelta(days=1)
    copy = (f"Ja nedēļas nogalē biji prom no ekrāniem — pieci notikumi "
            f"({lv_date(sat)} – {lv_date(sun)}), par kuriem runāja Latvija. "
            f"Pilnie stāsti portālā.")
    return _carousel_digest(session, day, articles, title, "news",
                            "https://tv3.lv", copy,
                            f"digest-monday-{day.isoformat()}",
                            "mondaytop5", 8, ribbon="NOGALES TOP 5")


def _mosaic_story(session, day, title: str, images: list[str], guid: str,
                  marker: str, at: datetime,
                  articles: list[Article] | None = None) -> Post | None:
    """Foto mozaīkas stāsts (9:16) ar CTA — stāstu kanālā, kur auditorija ir
    cita nekā plūsmā un konkurences ar saviem plūsmas ierakstiem nav."""
    from app import cards

    try:
        media = [cards.render_mosaic_story(title, "news", images,
                                          date_txt=day.strftime("%d.%m.%Y"))]
    except Exception as e:  # noqa: BLE001
        log.warning("%s render failed: %s", marker, e)
        return None
    return _schedule(session, _digest_article(
        session, guid, f"{title} (stāsts)", "news", "https://tv3.lv"),
        "story", "", media, "https://tv3.lv", marker, at,
        channel=STORY_CHANNEL,
        recipe={"kind": "mosaic", "title": title, "section": "news",
                "articles": [a.id for a in (articles or [])],
                "date": day.strftime("%d.%m.%Y")})


def build_monday_story(session, day, now: datetime | None = None) -> Post | None:
    """Pirmdienas rīta stāsts: nedēļas nogales foto mozaīka ar CTA — otrs
    pieskāriens citai auditorijai (stāstu skatītājiem) 24 h formātā."""
    from app import cards

    articles = week_top(session, since=weekend_start(now))
    images = [i for i in (_clean_image(a) for a in articles) if i]
    if len(articles) < 3 or len(images) < 3 or not cards.renderer_available():
        return None
    return _mosaic_story(session, day, "Nedēļas nogales TOP 5", images,
                         f"digest-monday-story-{day.isoformat()}", "mondaystory",
                         _local_slot(day, 8) + timedelta(minutes=30),
                         articles=articles)


def build_daily_story(session, day, now: datetime | None = None) -> Post | None:
    """«Dienas TOP 3» vakara stāsts (Pr–Pk 20:00): dienas lasītākais kā foto
    mozaīka. Materiālam ņemam piecus dienas TOP rakstus — mozaīka ir kolāža,
    ne saraksts, un tīru foto darba dienā ne vienmēr ir katram rakstam."""
    from app import cards

    articles = week_top(session, limit=5, since=day_start(now))
    images = [i for i in (_clean_image(a) for a in articles) if i]
    if len(articles) < 3 or len(images) < 3 or not cards.renderer_available():
        return None
    return _mosaic_story(session, day, "Dienas TOP 3", images,
                         f"digest-daily-story-{day.isoformat()}", "dailystory",
                         _local_slot(day, 20), articles=articles)


def build_reel_digest(session, day) -> Post | None:
    from app import reels

    articles = week_top(session, limit=5)
    if len(articles) < 3 or not reels.available():
        return None
    points = [_point_line(i, a) for i, a in enumerate(articles, 1)][:5]
    title = "Nedēļa 30 sekundēs"
    imgs = [_clean_image(a) for a in articles]
    try:
        # saturs = 5 punkti pa 6 s (30 s, kā sola nosaukums); klāt īss 3 s
        # intro (rakstu foto mozaīka ar virsrakstu) un 3 s CTA outro — 36 s;
        # punktu kadros attiecīgā raksta foto aptumšotā fonā
        media = [reels.build_reel(title, "news", "", points, max_points=5,
                                  frame_seconds=6.0, edge_seconds=3.0,
                                  cover_images=[i for i in imgs if i],
                                  point_images=imgs)]
    except Exception as e:  # noqa: BLE001
        log.warning("reel digest failed: %s", e)
        return None
    copy = ("Nedēļa 30 sekundēs — pieci notikumi, par kuriem runāja Latvija. "
            "Pilnie stāsti portālā tv3.lv.")
    return _schedule(session, _digest_article(
        session, f"digest-reel-{day.isoformat()}", title, "news",
        "https://tv3.lv"), "reel", copy, media, "https://tv3.lv",
        "digestreel", _local_slot(day, 12),
        recipe={"kind": "reel", "title": title, "section": "news",
                "points": points, "articles": [a.id for a in articles],
                "date": day.strftime("%d.%m.%Y")})


def build_icymi(session, day) -> Post | None:
    """Labs raksts (score >= 0.7) ar vājāko izmērīto atdevi — viena otrā
    iespēja ar godīgu datumu tekstā."""
    since = week_start()
    rows = session.execute(
        select(Article, func.coalesce(func.sum(PostMetrics.ga_sessions), 0)
               .label("s"))
        .join(Post, Post.article_id == Article.id)
        .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Post.state == "published", Post.published_at >= since,
               Post.channel == CHANNEL, Article.ai_score >= 0.7)
        .group_by(Article.id)
    ).all()
    if not rows:
        return None
    reposted = {p.article_id for p in session.execute(
        select(Post).where(Post.hook_type.in_(("icymi", "evergreen")))
    ).scalars().all()}
    candidates = [r for r in rows if r.Article.id not in reposted
                  and not (r.Article.raw_json or {}).get("_digest")]
    if not candidates:
        return None
    weakest = min(candidates, key=lambda r: r.s or 0)
    art = weakest.Article
    dt = art.published_at or art.first_seen_at
    copy = (f"Stāsts, ko daudzi palaida garām: {art.title.rstrip('.')}. "
            f"Publicēts {lv_date(dt)} — joprojām vērts izlasīt.")
    return _schedule(session, art, "link", copy, [],
                     art.canonical_url or art.url, "icymi",
                     _local_slot(day, 15))


def _parse_quiz_lines(lines: list[str], articles: list[Article]
                      ) -> list[tuple[str, Article | None]]:
    """AI atbild formātā «3 | Jautājums?» — cipars ir raksts, no kura
    jautājums nāk. Atbilde ir TAJĀ rakstā, tāpēc kartītei jāved uz to, nevis
    uz portāla sākumlapu. Ja numura nav vai tas ir ārpus saraksta, kartīte
    paliek bez sava raksta (ved uz portālu)."""
    out: list[tuple[str, Article | None]] = []
    for line in lines:
        idx, sep, text = line.partition("|")
        if sep and idx.strip().isdigit():
            n = int(idx.strip())
            art = articles[n - 1] if 1 <= n <= len(articles) else None
            out.append((text.strip(), art))
        else:
            out.append((line.strip(), None))
    return out


def build_quiz(session, day) -> Post | None:
    """Kvīza karuselis no nedēļas TOP — jautājumus raksta AI; bez AI atslēgas
    formāts izlaižas (kvīzs bez īstiem jautājumiem nav publicējams).
    Slots ir svētdienas vakars: nedēļas lielākais ritināšanas logs, kurā
    cilvēkiem ir laiks atbildēt un komentēt.

    Kvīzs tiek būvēts TIKAI no rakstiem, par kuriem drīkst spēlēties
    (playful_safe): bojāgājušo skaits nav kvīza jautājums."""
    from app import cards

    articles = [a for a in week_top(session, limit=8) if playful_safe(a)][:5]
    if len(articles) < 3 or not cards.renderer_available():
        return None
    facts = "\n".join(
        f"{i}. {a.title} ({lv_date(a.published_at or a.first_seen_at)})"
        for i, a in enumerate(articles, 1))
    lines = _ai_lines(session, max_tokens=500, prompt=(
        f"No šiem numurētajiem tv3.lv nedēļas virsrakstiem uzraksti 5 īsus "
        f"kvīza jautājumus latviski. Katru jaunā rindā formātā "
        f"«numurs | jautājums», kur numurs ir tā virsraksta numurs, no kura "
        f"jautājums nāk (atbilde ir tajā rakstā). Bez atbildēm. "
        f"KATRS jautājums līdz 90 rakstzīmēm — garāks kartītē neietilpst. "
        f"Nevainojama pareizrakstība un galotnes. "
        f"Datumus raksti absolūti (piem., «26. augustā»), nekad "
        f"relatīvi. NEKAD neveido jautājumu par cietušajiem, "
        f"bojāgājušajiem, noziegumiem, karu vai katastrofām — kvīzs ir "
        f"izklaide, un cilvēku ciešanas nav spēle. "
        f"Jautā TIKAI par jau notikušu, noslēgtu faktu («kas notika», "
        f"«kurš uzvarēja», «cik»), nekad par to, kas vēl varētu notikt, "
        f"par izredzēm, kvalifikāciju vai situāciju, kas dažās dienās "
        f"var mainīties — ieraksts plūsmā dzīvo ilgāk nekā ziņa.\n{facts}"))
    # prasām piecus, lai pēc filtriem paliktu trīs: kvīzs ar diviem
    # jautājumiem izskatās pēc pusfabrikāta
    pairs = [(q, art) for q, art in _parse_quiz_lines(lines, articles)
             if q.endswith("?") and len(q) <= 130
             and not has_relative_words(q) and not grim_words(q)
             and not open_ended(q)][:3]
    if len(pairs) < 3:
        return None
    questions = [q for q, _ in pairs]
    # formāta nosaukums paliek angliskais «QUIZ» — tā to sauc
    # redakcija; jautājumi un viss pārējais teksts ir latviski
    title = "Nedēļas QUIZ: vai sekoji notikumiem?"
    image = next((i for i in (_clean_image(a) for a in articles) if i), "")
    blur = "" if image else next(
        (i for i in (_any_image(a) for a in articles) if i), "")
    try:
        # vāks ar foto; jautājumu kartītes paliek bez attēla apzināti —
        # raksta bilde blakus jautājumam nodotu atbildi
        media = cards.render_cards(title, "news", "#QUIZ", questions,
                                   image, "Atbildes — tv3.lv",
                                   cover_blur=blur,
                                   date_txt=day.strftime("%d.%m.%Y"))
    except Exception as e:  # noqa: BLE001
        log.warning("quiz render failed: %s", e)
        return None
    portal = "https://tv3.lv"
    # media = [vāks, jautājumi..., CTA]. Katra jautājuma kartīte ved uz SAVU
    # rakstu, kur ir atbilde; virsrakstu joslā liekam neitrālu aicinājumu —
    # raksta virsraksts blakus jautājumam nodotu atbildi.
    card_links = ([portal] + [(a.canonical_url or a.url) if a else portal
                              for _, a in pairs] + [portal])[:len(media)]
    card_titles = ([title] + ["Atbilde — tv3.lv"] * len(pairs)
                   + ["Atbildes — tv3.lv"])[:len(media)]
    copy = ("Nedēļas QUIZ — trīs jautājumi par notikumiem, par kuriem "
            "rakstīja tv3.lv. Atbildes atradīsi portālā.")
    return _schedule(session, _digest_article(
        session, f"digest-quiz-{day.isoformat()}", title, "news",
        portal), "card_carousel", copy, media, portal,
        "quiz", _local_slot(day, 19),
        card_links=card_links, card_titles=card_titles,
        recipe={"kind": "quiz", "title": title, "section": "news",
                "tag": "#QUIZ", "questions": questions,
                "question": "Atbildes — tv3.lv",
                "articles": [a.id for a in articles],
                "answer_articles": [a.id if a else 0 for _, a in pairs],
                "date": day.strftime("%d.%m.%Y"),
                "photos": {"total": 1, "clean": 1 if image else 0,
                           "blurred": 1 if blur else 0}})


def build_evergreen(session, day) -> Post | None:
    """Arhīva raksts (vecāks par 14 dienām), ko GA4 joprojām redz lasām."""
    old_enough = utcnow() - timedelta(days=14)
    recent_metrics = utcnow() - timedelta(days=7)
    rows = session.execute(
        select(Article, func.sum(PostMetrics.ga_sessions).label("s"))
        .join(Post, Post.article_id == Article.id)
        .join(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Article.published_at <= old_enough,
               PostMetrics.collected_at >= recent_metrics,
               Article.sensitivity == [])
        .group_by(Article.id)
        .order_by(func.sum(PostMetrics.ga_sessions).desc())
    ).all()
    reposted = {p.article_id for p in session.execute(
        select(Post).where(Post.hook_type.in_(("icymi", "evergreen")))
    ).scalars().all()}
    pick = next((r.Article for r in rows
                 if r.Article.id not in reposted and (r.s or 0) > 0
                 and not (r.Article.raw_json or {}).get("_digest")), None)
    if pick is None:
        return None
    dt = pick.published_at or pick.first_seen_at
    copy = (f"Joprojām aktuāli: {pick.title.rstrip('.')}. "
            f"Publicēts {lv_date(dt)}, un lasītāji pie tā atgriežas "
            f"vēl šobrīd.")
    return _schedule(session, pick, "link", copy, [],
                     pick.canonical_url or pick.url, "evergreen",
                     _local_slot(day, 9))


def build_weekend_guide(session, day) -> Post | None:
    """«Nedēļas nogales gids» (Pk 17:00): izklaides izlase brīvdienām —
    vienīgā franšīze ar tiešu TV3 ētera un Go3 sinerģiju, un vienīgā, kas
    apzināti izceļ izklaides sadaļu, kas darba dienās paliek ziņu ēnā."""
    from app import cards

    articles = week_top(session, section="entertainment")
    if len(articles) < 3 or not cards.renderer_available():
        return None
    title = "Nedēļas nogales gids"
    link = "https://tv3.lv/izklaide"
    copy = ("Ko skatīties, ko lasīt un ko nepalaist garām brīvdienās — "
            "izklaides izlase no tv3.lv. Pilnie stāsti portālā.")
    return _carousel_digest(session, day, articles, title, "entertainment",
                            link, copy, f"digest-guide-{day.isoformat()}",
                            "guide", 17, tag="#BRĪVDIENĀM",
                            ribbon="NOGALES GIDS")


def build_question(session, day) -> Post | None:
    """«Trešdienas jautājums» (19:00): viens jautājums no nedēļas lasītākā
    raksta. Vienīgā franšīze, kuras mērķis ir komentāri — jēgpilnas sarunas
    signāls Facebook ranžēšanā sver vairāk nekā reakcijas. Atbilde ir rakstā,
    saite iet gan tekstā, gan pirmajā komentārā (kā visiem foto ierakstiem)."""
    from app import cards

    # sarunas jautājums par traģēdiju ir necieņa pret cietušajiem un tiešs
    # zīmola risks — tādus rakstus šis formāts neaiztiek
    articles = [a for a in week_top(session, limit=6) if playful_safe(a)]
    if not articles or not cards.renderer_available():
        return None
    art = articles[0]
    dt = art.published_at or art.first_seen_at
    lines = _ai_lines(session, max_tokens=200, prompt=(
        f"tv3.lv raksta virsraksts: «{art.title}» "
        f"(publicēts {lv_date(dt) if dt else 'nesen'}).\n"
        f"Uzraksti VIENU īsu jautājumu latviski (līdz 90 rakstzīmēm), ko "
        f"uzdot Facebook lasītājiem, lai viņi komentāros pastāsta savu "
        f"viedokli par šo tēmu. Nevainojama pareizrakstība un galotnes. "
        f"Bez emocijzīmēm, bez hashtagiem, bez relatīviem laika vārdiem "
        f"(«vakar», «šonedēļ»). Atbildi tikai ar jautājumu."))
    question = next((q for q in lines if q.endswith("?")
                     and len(q) <= 110 and not has_relative_words(q)
                     and not grim_words(q)), "")
    if not question:
        return None
    try:
        media = [cards.render_share_image(
            question, art.section or "news", _clean_image(art),
            kicker="JAUTĀJUMS", width=1080, height=1350,
            date_txt=day.strftime("%d.%m.%Y"),
            blur_image=_any_image(art))]
    except Exception as e:  # noqa: BLE001
        log.warning("question render failed: %s", e)
        return None
    copy = (f"{question}\n\nPastāsti komentāros, ko domā tu — bet vispirms "
            f"izlasi, kas notika: {art.title.rstrip('.')}.")
    return _schedule(session, art, "photo", copy, media,
                     art.canonical_url or art.url, "question",
                     _local_slot(day, 19),
                     recipe={"kind": "share", "title": question,
                             "kicker": "JAUTĀJUMS", "article": art.id,
                             "section": art.section or "news",
                             "date": day.strftime("%d.%m.%Y")})


def build_year_ago(session, day, now: datetime | None = None) -> Post | None:
    """«Šajā dienā pirms gada» (Ce 15:00): arhīva raksts ar gadadienas āķi.
    Logs ir ±3 dienas ap gadu atpakaļ; jutīgas tēmas (traģēdijas, noziegumi)
    nekad neatgriežas kā nostalģija."""
    now = now or utcnow()
    lo, hi = now - timedelta(days=368), now - timedelta(days=362)
    rows = session.execute(
        select(Article, func.coalesce(func.sum(PostMetrics.ga_sessions), 0)
               .label("s"))
        .join(Post, Post.article_id == Article.id)
        .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Article.published_at >= lo, Article.published_at <= hi,
               Article.sensitivity == [])
        .group_by(Article.id)
        .order_by(func.coalesce(func.sum(PostMetrics.ga_sessions), 0).desc())
    ).all()
    reposted = {p.article_id for p in session.execute(
        select(Post).where(Post.hook_type.in_(("icymi", "evergreen", "yearago")))
    ).scalars().all()}
    pick = next((r.Article for r in rows
                 if r.Article.id not in reposted
                 and not (r.Article.raw_json or {}).get("_digest")), None)
    if pick is None:
        return None
    dt = pick.published_at or pick.first_seen_at
    copy = (f"Šajā dienā pirms gada: {pick.title.rstrip('.')}. "
            f"Publicēts {lv_date_full(dt)} — atskaties, kā toreiz bija.")
    return _schedule(session, pick, "link", copy, [],
                     pick.canonical_url or pick.url, "yearago",
                     _local_slot(day, 15))


def build_number(session, day) -> Post | None:
    """«Nedēļas skaitlis» (Ot 12:00): viens pārsteidzošs skaitlis no nedēļas
    TOP raksta uz brendētas kartes, konteksts — rakstā. Ja AI pārliecinošu
    skaitli neatrod, diena paliek tukša: labāk nekas nekā vājš ieraksts."""
    from app import cards

    # bojāgājušo skaits nekad nav «nedēļas skaitlis»
    articles = [a for a in week_top(session, limit=6) if playful_safe(a)][:3]
    if not articles or not cards.renderer_available():
        return None
    for art in articles:
        lines = _ai_lines(session, max_tokens=200, prompt=(
            f"tv3.lv raksta virsraksts: «{art.title}».\n"
            f"Ja virsrakstā ir konkrēts, pārsteidzošs skaitlis (summa, "
            f"procenti, daudzums, gads nav skaitlis šajā nozīmē), atbildi "
            f"ar DIVĀM rindām:\n"
            f"1. rinda — pats skaitlis ar mērvienību, īsi (piem., «47%» vai "
            f"«1,2 milj. €»).\n"
            f"2. rinda — viena konteksta rindiņa latviski līdz 90 "
            f"rakstzīmēm, nevainojama pareizrakstība un galotnes, bez "
            f"relatīviem laika vārdiem.\n"
            f"Ja pārliecinoša skaitļa nav, atbildi ar vienu vārdu: NAV."))
        if len(lines) < 2 or lines[0].upper().startswith("NAV"):
            continue
        number, context = lines[0][:12].strip(), lines[1][:90].strip()
        if (not any(c.isdigit() for c in number) or has_relative_words(context)
                or grim_words(context)):
            continue
        try:
            media = [cards.render_number_card(
                number, context, art.section or "news", _clean_image(art),
                date_txt=day.strftime("%d.%m.%Y"),
                # tīra foto nav -> photopost grafika kā izpludināta faktūra;
                # plakana krāsas karte plūsmā zaudē
                blur_image=_any_image(art))]
        except Exception as e:  # noqa: BLE001
            log.warning("number card render failed: %s", e)
            return None
        copy = (f"{number} — {context} Pilnais stāsts portālā: "
                f"{art.title.rstrip('.')}.")
        return _schedule(session, art, "photo", copy, media,
                         art.canonical_url or art.url, "number",
                         _local_slot(day, 12),
                         recipe={"kind": "number", "number": number,
                                 "context": context, "article": art.id,
                                 "section": art.section or "news",
                                 "date": day.strftime("%d.%m.%Y")})
    return None


# --- orķestris ------------------------------------------------------------

def plan_for(session, day, weekday: int, now: datetime) -> dict:
    """Dienas franšīžu plāns: nosaukums -> (slēdzis, agrākā būvēšanas stunda,
    celtnieks). Būvēšanas stunda nav publicēšanas laiks — vakara formātus
    būvējam tikai vakarā, citādi «dienas TOP» taptu no rīta datiem."""
    plan: dict[str, tuple[str, int, object]] = {}
    if weekday == 0:      # pirmdiena — nedēļas nogales apkopojums rīta pīķim
        plan["monday_top5"] = ("monday", 7,
                               lambda: build_monday_top5(session, day, now))
        plan["monday_story"] = ("monday", 7,
                                lambda: build_monday_story(session, day, now))
    elif weekday == 1:    # otrdiena
        plan["number"] = ("number", 10, lambda: build_number(session, day))
    elif weekday == 2:    # trešdiena
        plan["question"] = ("question", 16, lambda: build_question(session, day))
    elif weekday == 3:    # ceturtdiena
        plan["yearago"] = ("yearago", 10,
                           lambda: build_year_ago(session, day, now))
    elif weekday == 4:    # piektdiena
        plan["guide"] = ("guide", 12, lambda: build_weekend_guide(session, day))
    elif weekday == 5:    # sestdiena
        plan["top5_sport"] = ("top5", 7,
                              lambda: build_top5(session, day, "sport"))
        plan["reel"] = ("reel", 7, lambda: build_reel_digest(session, day))
        plan["icymi"] = ("icymi", 7, lambda: build_icymi(session, day))
    else:                 # svētdiena
        plan["top5_all"] = ("top5", 7, lambda: build_top5(session, day, None))
        plan["quiz"] = ("quiz", 7, lambda: build_quiz(session, day))
        plan["evergreen"] = ("evergreen", 7,
                             lambda: build_evergreen(session, day))
    if weekday <= 4:      # vakara stāsts ārpus plūsmas, darba dienās
        plan["daily_story"] = ("daily_story", 19,
                               lambda: build_daily_story(session, day, now))
    return plan


def run(session, now: datetime | None = None) -> int:
    """Stundas solis: izpilda dienas ieslēgtos formātus, katru vienreiz savā
    dienā. Formāts, kas atgriež None (nepietiek datu, nav renderētāja, AI
    neatrada skaitli), dienu tomēr atzīmē — franšīze nemēģina katru stundu."""
    now = now or utcnow()
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    if local.hour < 7:    # naktī neko nebūvējam
        return 0
    day = local.date()
    toggles = settings(session)
    created = 0
    for name, (feature, min_hour, builder) in plan_for(
            session, day, local.weekday(), now).items():
        if local.hour < min_hour or not toggles.get(feature, False):
            continue
        if get_setting(session, _ran_key(name, day)):
            continue
        try:
            post = builder()
        except Exception as e:  # noqa: BLE001 — franšīze negāž pārējo grafiku
            log.warning("franchise %s failed: %s", name, e)
            continue
        set_setting(session, _ran_key(name, day), "done")
        if post is not None:
            created += 1
            log.info("franchise %s scheduled as post %s", name, post.id)
    session.commit()
    return created
