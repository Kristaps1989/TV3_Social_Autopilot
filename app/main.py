"""Admin UI + healthcheck. Deliberately boring: server-rendered pages,
plain HTML forms, zero JavaScript build step — easy to support."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os
import tempfile
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from app import (auth, config, credentials, ga4, manual, pagemeta, reels,
                 runtime, shortlinks, tts)
from app.db import get_session, init_db
from app.models import Article, Evaluation, Post, get_setting, set_setting, utcnow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_editable_dirs()
    init_db()
    import os

    scheduler = None
    if os.environ.get("DISABLE_SCHEDULER", "").lower() != "true":
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
        log.info("scheduler started (dry_run=%s)", runtime.is_dry_run())
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="TV3 Social Autopilot", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Pages reachable without a session: healthcheck, the auth pages, rendered
# card images (unguessable names; platforms must be able to fetch them) and
# the /r/<code> short links, which every reader on Facebook follows.
PUBLIC_PATHS = {"/health", "/login", "/setup"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """The admin UI holds the kill switch and platform tokens — everything
    except /health requires a logged-in session. With no password configured
    yet, every page redirects to the one-time /setup screen."""
    if (request.url.path in PUBLIC_PATHS
            or request.url.path.startswith("/media/")
            or request.url.path.startswith("/r/")):
        return await call_next(request)
    session = get_session()
    try:
        if not auth.password_configured(session):
            return RedirectResponse("/setup", status_code=303)
        if auth.valid_token(session, request.cookies.get(auth.SESSION_COOKIE, "")):
            return await call_next(request)
    finally:
        session.close()
    return RedirectResponse("/login", status_code=303)


def _login_response(request: Request, session) -> RedirectResponse:
    resp = RedirectResponse("/", status_code=303)
    secure = (request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_token(session),
                    max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=secure)
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    session = get_session()
    try:
        if auth.password_configured(session):
            return RedirectResponse("/login", status_code=303)
    finally:
        session.close()
    return templates.TemplateResponse(request, "auth.html",
                                      {"mode": "setup", "error": error})


@app.post("/setup")
def setup_submit(request: Request, password: str = Form(...),
                 password2: str = Form(...)):
    from urllib.parse import quote

    session = get_session()
    try:
        if auth.password_configured(session):
            return RedirectResponse("/login", status_code=303)
        if password != password2:
            return RedirectResponse("/setup?error=Paroles+nesakrīt", status_code=303)
        err = auth.set_password(session, password)
        if err:
            return RedirectResponse(f"/setup?error={quote(err)}", status_code=303)
        return _login_response(request, session)
    finally:
        session.close()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    session = get_session()
    try:
        if not auth.password_configured(session):
            return RedirectResponse("/setup", status_code=303)
    finally:
        session.close()
    return templates.TemplateResponse(request, "auth.html",
                                      {"mode": "login", "error": error})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    session = get_session()
    try:
        if auth.check_password(session, password):
            return _login_response(request, session)
    finally:
        session.close()
    time.sleep(1)  # slow down password guessing
    return RedirectResponse("/login?error=Nepareiza+parole", status_code=303)


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


def public_base(request: Request) -> str:
    env = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.url.netloc}"


def to_local(dt):
    if dt is None:
        return ""
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE)).strftime("%d.%m. %H:%M")


def fmt_num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return v
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:.0f}"


def fmt_dur(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    return f"{s // 60}:{s % 60:02d}"


templates.env.filters["local"] = to_local
templates.env.filters["basename"] = lambda p: Path(str(p)).name
templates.env.filters["num"] = fmt_num
templates.env.filters["dur"] = fmt_dur


@app.get("/health")
def health():
    session = get_session()
    try:
        session.execute(select(Post.id).limit(1))
        from app import runtime

        return {"status": "ok", "dry_run": runtime.is_dry_run(session)}
    finally:
        session.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    session = get_session()
    try:
        now = utcnow()
        channels = config.load_channels()
        channel_data = []
        for name, cfg in channels.items():
            upcoming = session.execute(
                select(Post).where(Post.channel == name, Post.state == "scheduled")
                .order_by(Post.scheduled_at).limit(8)
            ).scalars().all()
            recent = session.execute(
                select(Post).where(Post.channel == name,
                                   Post.state.in_(("published", "failed", "cancelled")))
                .order_by(desc(Post.published_at), desc(Post.created_at)).limit(6)
            ).scalars().all()
            published_today = session.execute(
                select(Post).where(Post.channel == name, Post.state == "published",
                                   Post.published_at >= now - timedelta(hours=24))
            ).scalars().all()
            channel_data.append({
                "name": name,
                "display_name": (cfg or {}).get("display_name", name),
                "paused": get_setting(session, f"pause:{name}") == "on",
                "upcoming": upcoming,
                "recent": recent,
                "today_count": len(published_today),
                "daily_cap": ((cfg or {}).get("daily_cap") or "∞"),
            })
        return templates.TemplateResponse(request, "dashboard.html", {
            "channels": channel_data,
            "kill_switch": get_setting(session, "kill_switch") == "on",
            "dry_run": runtime.is_dry_run(session),
            "data_persistent": runtime.data_dir_persistent(),
            "ai_active": bool(credentials.get("anthropic_api_key", session)),
        })
    finally:
        session.close()


@app.post("/toggle/live")
def toggle_live():
    session = get_session()
    try:
        runtime.set_live(session, runtime.is_dry_run(session))  # flip the mode
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/kill")
def toggle_kill():
    session = get_session()
    try:
        current = get_setting(session, "kill_switch")
        set_setting(session, "kill_switch", "" if current == "on" else "on")
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/pause/{channel}")
def toggle_pause(channel: str):
    session = get_session()
    try:
        key = f"pause:{channel}"
        current = get_setting(session, key)
        set_setting(session, key, "" if current == "on" else "on")
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/post/{post_id}/cancel")
def cancel_post(post_id: int):
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post and post.state in ("proposed", "scheduled"):
            post.state = "cancelled"
            post.error = "cancelled manually in admin"
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/post/{post_id}/regenerate")
def regenerate_post(post_id: int):
    """Uzzīmē ieraksta grafiku no jauna ar pašreizējo izkārtojumu — pēc
    dizaina labojuma, pēc renderētāja kļūmes vai kad attēls neielādējās."""
    from urllib.parse import quote

    from app import regenerate as regen

    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post is None:
            return RedirectResponse("/", status_code=303)
        ok, message = regen.regenerate(session, post)
    finally:
        session.close()
    return RedirectResponse(
        f"/post/{post_id}/preview?msg={quote(message)}&ok={'1' if ok else '0'}",
        status_code=303)


@app.post("/post/{post_id}/publish-now")
def publish_now(post_id: int):
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post and post.state == "scheduled":
            post.scheduled_at = utcnow()
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/post/{post_id}/republish")
def republish_post(post_id: int):
    """A dry-run 'published' post never went to the platform. This clones it
    into the queue for immediate real publishing (respects the current mode)."""
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post and post.state == "published" and post.dry_run:
            clone = Post(article_id=post.article_id, channel=post.channel,
                         format=post.format, copy=post.copy,
                         hashtags=post.hashtags or [], media=post.media or [],
                         link_url=post.link_url, scheduled_at=utcnow(),
                         state="scheduled", dry_run=runtime.is_dry_run(session))
            session.add(clone)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/post/{post_id}/copy")
def edit_copy(post_id: int, copy: str = Form(...)):
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post and post.state in ("proposed", "scheduled"):
            from app import disclosure
            from app.best_practices import sanitize_copy

            channels = config.load_channels()
            platform = (channels.get(post.channel) or {}).get("platform", "")
            sens = post.article.sensitivity if post.article else []
            post.copy, _, _ = sanitize_copy(
                copy, post.hashtags or [], platform, sens,
                reserve_link_chars=True,
                reserve_chars=len(disclosure.caption_line(platform)) + 2)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.get("/post/{post_id}/preview", response_class=HTMLResponse)
def post_preview(request: Request, post_id: int, msg: str = "", ok: str = ""):
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post is None:
            return RedirectResponse("/", status_code=303)
        channels = config.load_channels()
        cfg = channels.get(post.channel) or {}
        platform = cfg.get("platform", "")
        from app.best_practices import add_utm, assemble_post_text

        link = add_utm(post.link_url, platform, post.id,
                       hook=post.hook_type or "") if post.link_url else ""
        shown = shortlinks.display_link(post.id, link, None, post.article)
        # kura īsā saite tika izvēlēta: mūsu /r/ kods (skaita klikšķus) vai
        # CMS /p/<id> (īsa, bet klikšķus neskaita)
        cms_short = pagemeta.short_url(post.article) if post.article else ""
        short_kind = ("own" if shortlinks.short_url(post.id)
                      else ("cms" if cms_short and shown.startswith(cms_short) else ""))
        from app.pipeline import compose_text

        full_text, in_comment = compose_text(post, platform, shown)
        article = post.article
        img_portrait = False
        # Cik lielu daļu augstuma Facebook saites kartīte no attēla nogriež.
        # Saites ierakstā attēlu izvēlas Facebook, ne mēs, tāpēc redaktoram
        # tas citādi ir neredzams mehānisms — un tieši tas ziņu kadrā aizvāc
        # galvas.
        link_card_crop = 0.0
        if article and post.format == "link":
            from app import imageinfo

            try:
                img_portrait = imageinfo.orientation(article) == "portrait"
                link_card_crop = imageinfo.link_card_crop(
                    article, (article.images or [""])[0])
                session.commit()  # keep the probed size cached
            except Exception:  # noqa: BLE001
                img_portrait = False
        from app import regenerate as regen
        from app.pipeline import prebranded

        media_prebranded = bool((post.media or [""])[0]
                                and prebranded(str(post.media[0])))
        return templates.TemplateResponse(request, "preview.html", {
            "post": post, "article": article, "platform": platform,
            "media_prebranded": media_prebranded,
            "channel_name": cfg.get("display_name", post.channel),
            "full_text": full_text, "link": shown, "target_link": link,
            "link_in_comment": in_comment,
            "og_image": (article.images or [""])[0] if article else "",
            "img_portrait": img_portrait,
            "link_card_crop": link_card_crop,
            "can_regenerate": regen.can_regenerate(post),
            "cms_meta": pagemeta.meta(article) if article else {},
            "cms_short": cms_short, "short_kind": short_kind,
            "photos": ((post.extra or {}).get("recipe") or {}).get("photos"),
            "voice_script": ((post.extra or {}).get("recipe")
                             or {}).get("voice_script", ""),
            # cik ilgi lentē tiešām skan balss: no tā redzams ĪSTAIS temps
            # vārdos minūtē, nevis mans vērtējums par to, cik tas varētu būt
            "voice_seconds": ((post.extra or {}).get("recipe")
                              or {}).get("speech_seconds"),
            "tts_ready": tts.enabled(session=session),
            "card_targets": [
                {"n": i + 1, "url": u,
                 "term": (f"{post.hook_type}-karte{i + 1}" if post.hook_type
                          else f"karte{i + 1}")}
                for i, u in enumerate((post.extra or {}).get("card_links") or [])],
            "msg": msg, "msg_ok": ok == "1",
        })
    finally:
        session.close()


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, period: str = "7d", section: str = "",
          date_from: str = "", date_to: str = ""):
    """Merged analytics: site-wide GA4 explore (periods + filters) and the
    autopilot's own performance, one page."""
    from app import ga4, priors

    session = get_session()
    try:
        channels = config.load_channels()
        summaries = [priors.channel_summary(session, ch) for ch in channels]
        d = ga4.explore(session, period, section, date_from, date_to)
        sections_available = sorted(set(config.url_sections().values()))
        return templates.TemplateResponse(request, "stats.html", {
            "d": d,
            "spark": ga4.sparkline(d.get("timeseries") or []),
            "sections_available": sections_available,
            "sel_period": period, "sel_section": section,
            "sel_from": date_from, "sel_to": date_to,
            "autopilot": ga4.autopilot_contribution(session) if d.get("configured") else [],
            "summaries": summaries,
            "top": priors.top_posts(session, 10),
            "hooks": priors.hook_summary(session),
            "voice": priors.voice_summary(session),
            "story_ab": priors.story_summary(session),
            "ga4_on": ga4.configured(),
            "ga4_error": d.get("error", ""),
            "dry_run": runtime.is_dry_run(session),
        })
    finally:
        session.close()


@app.get("/stats/live", response_class=HTMLResponse)
def stats_live(request: Request):
    """Tiešraide: kas notiek portālā tieši tagad (GA4 Realtime API)."""
    from app import ga4

    session = get_session()
    try:
        d = ga4.realtime()
        return templates.TemplateResponse(request, "live.html", {
            "d": d, "spark": ga4.sparkline(d.get("series") or [], width=860, height=90),
            "dry_run": runtime.is_dry_run(session),
        })
    finally:
        session.close()


@app.get("/stats/page", response_class=HTMLResponse)
def stats_page(request: Request, path: str, period: str = "7d",
               section: str = "", date_from: str = "", date_to: str = ""):
    """Viena raksta auditorijas skats (no Lasītākā satura tabulas)."""
    from app import ga4

    d = ga4.page_insight(path, period, date_from, date_to)
    session = get_session()
    try:
        return templates.TemplateResponse(request, "page_insight.html", {
            "d": d, "path": path,
            "back": f"/stats?period={period}&section={section}"
                    f"&date_from={date_from}&date_to={date_to}",
            "dry_run": runtime.is_dry_run(session),
        })
    finally:
        session.close()


@app.get("/portal")
def portal_redirect():
    return RedirectResponse("/stats", status_code=308)


@app.get("/r/{code}")
def short_link(request: Request, code: str):
    """Public short link: /r/<code> -> the article with the same UTM tags the
    post would have carried. Fronted by tv3.lv (rules.yaml short_link_base),
    so readers only ever see a tv3.lv address."""
    from app.best_practices import add_utm
    from app.shortlinks import decode, is_bot

    home = "https://tv3.lv/"
    post_id = decode(code)
    if post_id is None:
        return RedirectResponse(home, status_code=302)
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post is None or not post.link_url:
            return RedirectResponse(home, status_code=302)
        platform = (config.load_channels().get(post.channel) or {}).get("platform", "")
        target = add_utm(post.link_url, platform, post.id, hook=post.hook_type or "")
        # carry through whatever the platform appended (fbclid & co)
        extra = str(request.url.query or "")
        if extra:
            target += ("&" if "?" in target else "?") + extra
        if not is_bot(request.headers.get("user-agent", "")):
            post.short_hits = (post.short_hits or 0) + 1
            session.commit()
        return RedirectResponse(target, status_code=302)
    finally:
        session.close()


@app.get("/media/{name}")
def media(name: str):
    """Rendered carousel cards (unguessable filenames)."""
    from fastapi.responses import FileResponse

    from app.cards import CARDS_DIR

    safe = Path(name).name
    path = CARDS_DIR / safe
    if not safe.endswith((".png", ".mp4")) or not path.exists():
        return Response(status_code=404)
    media_type = "video/mp4" if safe.endswith(".mp4") else "image/png"
    return FileResponse(path, media_type=media_type)


@app.get("/why", response_class=HTMLResponse)
def why(request: Request, url: str = "", msg: str = "", ok: str = ""):
    session = get_session()
    try:
        article = None
        evaluations = []
        posts = []
        if url:
            needle = url.strip().split("?")[0].rstrip("/")
            article = session.execute(
                select(Article).where(
                    (Article.canonical_url.like(f"%{needle}%"))
                    | (Article.url.like(f"%{needle}%"))
                    | (Article.title.like(f"%{url.strip()}%"))
                ).order_by(desc(Article.first_seen_at))
            ).scalars().first()
            if article:
                evaluations = session.execute(
                    select(Evaluation).where(Evaluation.article_id == article.id)
                    .order_by(desc(Evaluation.created_at)).limit(30)
                ).scalars().all()
                posts = article.posts
        return templates.TemplateResponse(request, "why.html", {
            "query": url, "article": article,
            "evaluations": evaluations, "posts": posts, "searched": bool(url),
            "manual_options": manual.channel_options() if article else {},
            "manual_unavailable": manual.unavailable() if article else [],
            "missing_channels": config.missing_channels() if article else [],
            "msg": msg, "msg_ok": ok == "1",
        })
    finally:
        session.close()


@app.post("/article/{article_id}/make")
def make_format(article_id: int, channel: str = Form(...), fmt: str = Form(...),
                back: str = Form("")):
    """Editor-requested format for one article (reel, carousel, photo …).

    The automation proposes reels and carousels rarely by design; this is
    how an editor says «this story is worth a reel» without waiting for the
    AI to agree."""
    from urllib.parse import quote

    session = get_session()
    try:
        article = session.get(Article, article_id)
        if article is None:
            return RedirectResponse("/articles", status_code=303)
        post, message = manual.build(session, article, channel, fmt)
        target = back or f"/why?url={quote(article.canonical_url or article.url)}"
        joiner = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{joiner}msg={quote(message)}&ok={'1' if post else '0'}",
            status_code=303)
    finally:
        session.close()


@app.get("/articles", response_class=HTMLResponse)
def articles(request: Request):
    session = get_session()
    try:
        rows = session.execute(
            select(Article).order_by(desc(Article.first_seen_at)).limit(60)
        ).scalars().all()
        feeds = config.load_feeds()
        mapped = {str(k) for k in (feeds.get("term_sections") or {})}
        unmapped: dict[str, int] = {}
        for a in rows:
            for tid in a.term_ids or []:
                if str(tid) not in mapped:
                    unmapped[str(tid)] = unmapped.get(str(tid), 0) + 1
        with_lead = sum(1 for a in rows if (a.lead or "").strip())
        with_image = sum(1 for a in rows if (a.images or []))
        # CMS metadati no raksta lapas — autors, redakcijas tagi, īsā saite
        cms = {a.id: {"author": pagemeta.author(a),
                      "tags": pagemeta.tags(a, 3),
                      "short": pagemeta.short_url(a),
                      "label": pagemeta.label(a),
                      "chars": pagemeta.content_chars(a),
                      "gallery": pagemeta.has_gallery(a),
                      "video": pagemeta.has_video(a),
                      "body": len(pagemeta.article_body(a))}
               for a in rows}
        return templates.TemplateResponse(request, "articles.html", {
            "articles": rows, "cms": cms,
            "with_meta": sum(1 for a in rows if pagemeta.meta(a)),
            "with_body": sum(1 for a in rows if pagemeta.has_body(a)),
            "with_lead": with_lead, "with_image": with_image,
            "unmapped_terms": sorted(unmapped.items(), key=lambda kv: -kv[1])[:20],
            "url_sections": feeds.get("url_sections") or {},
        })
    finally:
        session.close()


# --- Account connections --------------------------------------------------

@app.get("/connect", response_class=HTMLResponse)
def connect(request: Request, error: str = "", connected: str = ""):
    session = get_session()
    try:
        status = credentials.connection_status(session)
        fb_app_id, _ = credentials.fb_app()
        th_app_id, _ = credentials.threads_app()
        ai_key = credentials.get("anthropic_api_key", session)
        tts_key = credentials.get("azure_speech_key", session)
        el_key = credentials.get("elevenlabs_api_key", session)

        def _env(name: str, secret: bool = False) -> str:
            value = os.environ.get(name, "")
            if not value:
                return ""
            return "uzstādīts ✓" if secret else value

        ga4_sa = credentials.info(session, "ga4_service_account")

        # Meta reklāmu konts: saraksts izvēlei (best effort — ja lietotāja
        # tokens der) un gatavības pārbaude jau pieslēgtam kontam
        from adapters.meta_ads import MetaAdsClient

        ads_status = status.get("meta_ads") or {}
        ads_acct_row = credentials.info(session, "fb_ad_account_id")
        ad_accounts: list[dict] = []
        user_token = credentials.get("fb_user_token", session)
        if user_token:
            try:
                ad_accounts = credentials.fb_list_ad_accounts(user_token)
            except Exception as e:  # noqa: BLE001 — kartīte strādā arī bez saraksta
                log.warning("ad account listing failed: %s", e)
        ads_issues: list[str] = []
        ads_client = MetaAdsClient(session)
        if ads_client.configured():
            try:
                _, ads_issues = ads_client.readiness()
            except Exception as e:  # noqa: BLE001
                ads_issues = [f"pārbaude neizdevās: {e}"]
        vol = runtime.data_dir_persistent()
        from app import cards
        render_ok, render_err = cards.renderer_check()
        env_diag = {
            "Datu disks (Volume)": ("pastāvīgs ✓" if vol
                                    else "lokāla vide" if vol is None else ""),
            "Attēlu renderētājs (Chromium)": (
                "strādā ✓" if render_ok
                else f"NESTRĀDĀ — foto/story bez virsraksta plāksnes: {render_err}"),
            "Pēdējā renderēšanas kļūda": (cards.last_render_failure()
                                          or "nav reģistrēta ✓"),
            "Video (ffmpeg)": ("strādā ✓" if reels.ffmpeg_bin()
                               else "nav — reel formāts izslēgts"),
            "Īsās saites": (
                f"{shortlinks.base_url()}/<kods> ✓" if shortlinks.base_url()
                else "izslēgtas — tekstā iet pilnā saite ar UTM "
                     "(ieslēdz ar short_link_base sadaļā Noteikumi)"),
            "META_APP_ID": _env("META_APP_ID"),
            "META_APP_SECRET": _env("META_APP_SECRET", secret=True),
            "META_LOGIN_CONFIG_ID": _env("META_LOGIN_CONFIG_ID"),
            "FB_AD_ACCOUNT_ID": _env("FB_AD_ACCOUNT_ID"),
            "PUBLIC_BASE_URL": (_env("PUBLIC_BASE_URL")
                                or (f"auto: https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                                    if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")),
            "ANTHROPIC_API_KEY": _env("ANTHROPIC_API_KEY", secret=True),
            "DATABASE_URL": (os.environ.get("DATABASE_URL", "").split("://")[0] + "://…"
                             if os.environ.get("DATABASE_URL") else ""),
            "RULES_DIR": _env("RULES_DIR"),
            "PROMPTS_DIR": _env("PROMPTS_DIR"),
            "THREADS_APP_ID": _env("THREADS_APP_ID"),
            "THREADS_APP_SECRET": _env("THREADS_APP_SECRET", secret=True),
        }
        return templates.TemplateResponse(request, "connect.html", {
            "status": status,
            "env_diag": env_diag,
            "ai_key_masked": f"sk-ant-…{ai_key[-4:]}" if ai_key else "",
            "tts_key_masked": (f"…{tts_key[-4:]}" if tts_key else ""),
            "tts_region": credentials.get("azure_speech_region", session)
                          or "westeurope",
            "tts_voice": tts.voice_name(),
            "tts_provider": tts.provider(),
            "el_key_masked": (f"…{el_key[-4:]}" if el_key else ""),
            # ko konts TIEŠĀM drīkst lietot — balss ID iekodēt ir minēšana,
            # un tieši tur bezmaksas plāns atsitās ar 402
            "el_catalogue": (tts.elevenlabs_catalogue(session)
                             if el_key else {}),
            "el_voice": tts.voice_name({**config.load_rules(),
                                        "tts_provider": "elevenlabs"}),
            "meta_app_ready": bool(fb_app_id),
            "meta_app_id": fb_app_id,
            "meta_config_id": credentials.get("meta_login_config_id", session),
            "threads_app_ready": bool(th_app_id),
            "threads_app_id": th_app_id,
            "redirect_fb": f"{public_base(request)}/connect/facebook/callback",
            "redirect_th": f"{public_base(request)}/connect/threads/callback",
            "ga4_connected": ga4.configured(),
            "ga4_property": credentials.get("ga4_property_id", session),
            "ga4_sa_label": (ga4_sa.label if ga4_sa and ga4_sa.value else ""),
            "ads_status": ads_status, "ad_accounts": ad_accounts,
            "ad_account_id": credentials.get("fb_ad_account_id", session),
            "ad_account_label": (ads_acct_row.label if ads_acct_row
                                 and ads_acct_row.value else ""),
            "pixel_id": credentials.get("meta_pixel_id", session),
            "ads_issues": ads_issues,
            "x_ads_account_id": credentials.get("x_ads_account_id", session),
            "error": error, "connected": connected,
        })
    finally:
        session.close()


@app.post("/connect/meta")
def connect_meta(app_id: str = Form(""), app_secret: str = Form(""),
                 config_id: str = Form("")):
    """Save Meta app credentials from the UI (stored in DB, like the AI key).
    Empty fields keep their current value."""
    session = get_session()
    try:
        if app_id.strip():
            credentials.put(session, "meta_app_id", app_id.strip())
        if app_secret.strip():
            credentials.put(session, "meta_app_secret", app_secret.strip())
        if config_id.strip():
            credentials.put(session, "meta_login_config_id", config_id.strip())
        return RedirectResponse("/connect?connected=Meta+lietotnes+dati+saglabāti",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/threads-app")
def connect_threads_app(app_id: str = Form(""), app_secret: str = Form("")):
    session = get_session()
    try:
        if app_id.strip():
            credentials.put(session, "threads_app_id", app_id.strip())
        if app_secret.strip():
            credentials.put(session, "threads_app_secret", app_secret.strip())
        return RedirectResponse("/connect?connected=Threads+lietotnes+dati+saglabāti",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/instagram/link")
def connect_instagram():
    """Look up the IG Business account linked to the connected FB page and
    store its id — no separate OAuth needed, the page token authorizes IG."""
    from urllib.parse import quote

    session = get_session()
    try:
        try:
            ig_id, username = credentials.fb_page_instagram(session)
        except Exception as e:  # noqa: BLE001 — clear message to the UI
            return RedirectResponse(f"/connect?error={quote(str(e)[:250])}",
                                    status_code=303)
        credentials.put(session, "ig_user_id", ig_id, label=username)
        return RedirectResponse(
            f"/connect?connected={quote('Instagram @' + (username or ig_id))}",
            status_code=303)
    finally:
        session.close()


@app.post("/connect/instagram/disconnect")
def disconnect_instagram():
    session = get_session()
    try:
        credentials.put(session, "ig_user_id", "", label="")
        return RedirectResponse("/connect?connected=Instagram+savienojums+noņemts",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/x")
def connect_x(api_key: str = Form(""), api_secret: str = Form(""),
              access_token: str = Form(""), access_secret: str = Form("")):
    """Save X API keys from the UI. Empty fields keep their current value."""
    session = get_session()
    try:
        for key, value in (("x_api_key", api_key), ("x_api_secret", api_secret),
                           ("x_access_token", access_token),
                           ("x_access_secret", access_secret)):
            if value.strip():
                credentials.put(session, key, value.strip())
        return RedirectResponse("/connect?connected=X+atslēgas+saglabātas",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/x/disconnect")
def disconnect_x():
    session = get_session()
    try:
        for key in ("x_api_key", "x_api_secret", "x_access_token", "x_access_secret"):
            credentials.put(session, key, "", label="")
        return RedirectResponse("/connect?connected=X+atslēgas+noņemtas",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/ga4")
def connect_ga4(property_id: str = Form(""), service_account: str = Form(""),
                author_dimension: str = Form("")):
    """Save GA4 settings from the UI. Empty fields keep their current value."""
    import json as _json
    from urllib.parse import quote

    session = get_session()
    try:
        if property_id.strip():
            credentials.put(session, "ga4_property_id", property_id.strip())
        if author_dimension.strip():
            credentials.put(session, "ga4_author_dimension", author_dimension.strip())
        sa = service_account.strip()
        if sa:
            try:
                info = _json.loads(sa)
            except ValueError:
                return RedirectResponse(
                    "/connect?error=Service+account+lauks+nav+derīgs+JSON+—+"
                    "ielīmē+visu+lejupielādēto+failu", status_code=303)
            if not (info.get("client_email") and info.get("private_key")):
                return RedirectResponse(
                    "/connect?error=JSON+trūkst+client_email+/+private_key+—+"
                    "vajadzīga+service+account+atslēga,+ne+cits+fails",
                    status_code=303)
            credentials.put(session, "ga4_service_account", sa,
                            label=info.get("client_email", ""))
        return RedirectResponse("/connect?connected=GA4+iestatījumi+saglabāti",
                                status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/connect?error={quote(str(e)[:200])}", status_code=303)
    finally:
        session.close()


@app.post("/connect/ga4/disconnect")
def disconnect_ga4():
    session = get_session()
    try:
        for key in ("ga4_property_id", "ga4_service_account"):
            credentials.put(session, key, "", label="")
        return RedirectResponse("/connect?connected=GA4+iestatījumi+noņemti",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/anthropic")
def connect_anthropic(api_key: str = Form("")):
    """Save (or clear) the Anthropic API key. A saved key is verified with a
    minimal live call so a typo is caught immediately."""
    from urllib.parse import quote

    session = get_session()
    try:
        api_key = api_key.strip()
        if not api_key:
            credentials.put(session, "anthropic_api_key", "")
            return RedirectResponse("/connect?connected=AI+atslēga+noņemta", status_code=303)
        if not api_key.startswith("sk-ant-"):
            return RedirectResponse(
                "/connect?error=Atslēgai+jāsākas+ar+sk-ant-+—+pārbaudi+kopēto+vērtību",
                status_code=303)
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(model=config.AI_MODEL_FAST, max_tokens=1,
                                   messages=[{"role": "user", "content": "ok"}])
        except anthropic.AuthenticationError:
            return RedirectResponse("/connect?error=Atslēga+netika+pieņemta+(authentication+error)",
                                    status_code=303)
        except Exception as e:  # noqa: BLE001 — network etc.: save anyway, but say so
            credentials.put(session, "anthropic_api_key", api_key)
            return RedirectResponse(
                f"/connect?error={quote('Atslēga saglabāta, bet pārbaude neizdevās: ' + str(e)[:120])}",
                status_code=303)
        credentials.put(session, "anthropic_api_key", api_key)
        return RedirectResponse("/connect?connected=AI+(Claude)", status_code=303)
    finally:
        session.close()


@app.post("/connect/elevenlabs")
def connect_elevenlabs(api_key: str = Form("")):
    """Saglabā (vai noņem) ElevenLabs atslēgu reelu ierunai.

    Pārbaudām tāpat kā Azure: ierunājam paraugu ar TIEŠI šo pakalpojumu —
    citādi nepareiza atslēga parādītos tikai pēc nedēļas klusiem reeliem.
    Pārbaude iet ar elevenlabs pakalpojumu arī tad, ja Noteikumos vēl ir
    azure: atslēgu pārbauda tam pakalpojumam, kuram tā pieder.
    """
    from urllib.parse import quote

    session = get_session()
    try:
        api_key = api_key.strip()
        if not api_key:
            credentials.put(session, "elevenlabs_api_key", "")
            return RedirectResponse(
                "/connect?connected=ElevenLabs+atslēga+noņemta", status_code=303)
        credentials.put(session, "elevenlabs_api_key", api_key)
        el_rules = {**config.load_rules(), "tts_provider": "elevenlabs"}
        errors: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            sample = tts.synthesize("Pārbaudes ieraksts.", tmp, rules=el_rules,
                                    session=session, force=True, errors=errors)
        if not sample:
            detail = errors[0] if errors else "nezināms iemesls"
            return RedirectResponse(
                f"/connect?error={quote(f'Atslēga saglabāta, bet parauga ieruna neizdevās — {detail}')}",
                status_code=303)
        note = ("Balss+(ElevenLabs)" if tts.provider() == "elevenlabs" else
                "Balss+(ElevenLabs)+—+lai+to+lietotu,+Noteikumos+ieraksti+tts_provider:+elevenlabs")
        return RedirectResponse(f"/connect?connected={note}", status_code=303)
    finally:
        session.close()


@app.post("/connect/elevenlabs/voice")
def connect_elevenlabs_voice(voice_id: str = Form("")):
    """Ieraksta izvēlēto balsi Noteikumos un uzreiz to pamēģina.

    Bez šī redaktoram ID būtu jāpārkopē ar roku uz Noteikumu lapu, un vai
    balss vispār strādā, noskaidrotos tikai pēc tam — bezmaksas plānā
    bibliotēkas balsis caur API atbild ar 402.
    """
    from urllib.parse import quote

    voice_id = voice_id.strip()
    if not voice_id:
        return RedirectResponse("/connect?error=Balss+nav+izvēlēta",
                                status_code=303)
    session = get_session()
    try:
        config.set_rule("reel_voice_name", voice_id)
        el_rules = {**config.load_rules(), "tts_provider": "elevenlabs",
                    "reel_voice_name": voice_id}
        errors: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            sample = tts.synthesize("Pārbaudes ieraksts.", tmp, rules=el_rules,
                                    session=session, force=True, errors=errors)
        if not sample:
            detail = errors[0] if errors else "nezināms iemesls"
            return RedirectResponse(
                f"/connect?error={quote(f'Balss saglabāta, bet ieruna ar to neizdevās — {detail}')}",
                status_code=303)
        note = f"Balss saglabāta un pārbaudīta: {voice_id}"
        if tts.provider() != "elevenlabs":
            note += " — lai to lietotu, Noteikumos ieraksti tts_provider: elevenlabs"
        return RedirectResponse(f"/connect?connected={quote(note)}",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/azure-speech")
def connect_azure_speech(api_key: str = Form(""), region: str = Form("")):
    """Save (or clear) the Azure Speech key used for reel voice-overs.

    A saved key is verified by synthesizing one short line: a wrong key or
    region otherwise only shows up as silent reels days later."""
    from urllib.parse import quote

    session = get_session()
    try:
        api_key, typed = api_key.strip(), region.strip()
        if not api_key:
            credentials.put(session, "azure_speech_key", "")
            return RedirectResponse("/connect?connected=Balss+atslēga+noņemta",
                                    status_code=303)
        # Atslēgas ir piesaistītas reģionam, un Foundry lapa rāda galapunktus,
        # nevis reģionu — ielīmētu galapunktu tāpēc noraidām ar paskaidrojumu,
        # nevis klusi krītam atpakaļ uz noklusējumu un pēc tam uz 401
        region = tts.normalize_region(typed) if typed else "westeurope"
        if not region:
            return RedirectResponse(
                f"/connect?error={quote('No «' + typed[:60] + '» reģionu nolasīt nevar. Ieraksti reģiona nosaukumu (piem. westeurope) — to atrod Azure portālā pie resursa, laukā Location. Foundry «Project endpoint» un «Azure OpenAI endpoint» šeit neder.')}",
                status_code=303)
        credentials.put(session, "azure_speech_region", region)
        credentials.put(session, "azure_speech_key", api_key)
        # paraugu ierunājam pagaidu mapē un apejot kešu: pārbaudei jāaiziet
        # līdz Azure ar TIEŠI šo atslēgu, nevis jāatbild no vecā faila
        errors: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            sample = tts.synthesize("Pārbaudes ieraksts.", tmp,
                                    session=session, force=True, errors=errors)
        if not sample:
            detail = errors[0] if errors else "nezināms iemesls"
            return RedirectResponse(
                f"/connect?error={quote(f'Atslēga saglabāta, bet parauga ieruna reģionā «{region}» neizdevās — {detail}')}",
                status_code=303)
        return RedirectResponse("/connect?connected=Balss+(Azure+Speech)",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/password")
def change_password(current: str = Form(...), password: str = Form(...),
                    password2: str = Form(...)):
    from urllib.parse import quote

    session = get_session()
    try:
        if not auth.check_password(session, current):
            return RedirectResponse("/connect?error=Pašreizējā+parole+nepareiza", status_code=303)
        if password != password2:
            return RedirectResponse("/connect?error=Jaunās+paroles+nesakrīt", status_code=303)
        err = auth.set_password(session, password)
        if err:
            return RedirectResponse(f"/connect?error={quote(err)}", status_code=303)
        return RedirectResponse("/connect?connected=parole+nomainīta", status_code=303)
    finally:
        session.close()


@app.get("/connect/facebook")
def connect_facebook(request: Request):
    from urllib.parse import quote

    session = get_session()
    try:
        state = credentials.new_state(session)
        url = credentials.fb_auth_url(
            f"{public_base(request)}/connect/facebook/callback", state)
        return RedirectResponse(url, status_code=302)
    except Exception as e:  # noqa: BLE001 — surface the reason in the UI
        log.exception("facebook connect start failed")
        return RedirectResponse(
            f"/connect?error={quote(f'{type(e).__name__}: {str(e)[:180]}')}",
            status_code=303)
    finally:
        session.close()


@app.get("/connect/facebook/callback", response_class=HTMLResponse)
def connect_facebook_callback(request: Request, code: str = "", state: str = "",
                              error_description: str = ""):
    from urllib.parse import quote

    session = get_session()
    try:
        if error_description or not code:
            return RedirectResponse(
                f"/connect?error={quote(error_description or 'Meta atgrieza kļūdu')}",
                status_code=303)
        if not credentials.check_state(session, state):
            return RedirectResponse("/connect?error=OAuth+state+nesakrīt+—+mēģini+vēlreiz",
                                    status_code=303)
        try:
            user_token = credentials.fb_exchange_code(
                code, f"{public_base(request)}/connect/facebook/callback")
            pages = credentials.fb_list_pages(user_token)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(f"/connect?error={quote(str(e)[:200])}", status_code=303)
        if not pages:
            return RedirectResponse(
                "/connect?error=Šim+lietotājam+nav+pārvaldāmu+lapu", status_code=303)
        if len(pages) == 1:
            p = pages[0]
            credentials.put(session, "fb_page_id", p["id"], label=p.get("name", ""))
            credentials.put(session, "fb_page_token", p["access_token"],
                            label=p.get("name", ""))
            credentials.put(session, "fb_user_token", user_token,
                            label="reklāmu kontam",
                            expires_at=utcnow() + timedelta(days=60))
            return RedirectResponse("/connect?connected=facebook", status_code=303)
        # several pages: keep the user token (also needed later for ads)
        credentials.put(session, "fb_user_token", user_token,
                        label="reklāmu kontam",
                        expires_at=utcnow() + timedelta(days=60))
        return templates.TemplateResponse(request, "connect_pick_page.html",
                                          {"pages": pages})
    finally:
        session.close()


@app.post("/connect/facebook/token")
def connect_facebook_token(request: Request, user_token: str = Form(...)):
    """Fallback when Meta's OAuth dialog is broken: paste a user token from
    Graph API Explorer; it is extended to long-lived and the page connection
    is derived exactly like in the OAuth callback."""
    from urllib.parse import quote

    session = get_session()
    try:
        token = user_token.strip()
        if not token:
            return RedirectResponse("/connect?error=Ievadi+user+token", status_code=303)
        try:
            token = credentials.fb_extend_user_token(token)
            pages = credentials.fb_list_pages(token)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(f"/connect?error={quote(str(e)[:200])}",
                                    status_code=303)
        if not pages:
            return RedirectResponse(
                "/connect?error=Token+nedod+piekļuvi+nevienai+lapai+—+"
                "pārbaudi+pages_show_list+atļauju", status_code=303)
        if len(pages) == 1:
            p = pages[0]
            credentials.put(session, "fb_page_id", p["id"], label=p.get("name", ""))
            credentials.put(session, "fb_page_token", p["access_token"],
                            label=p.get("name", ""))
            credentials.put(session, "fb_user_token", token,
                            label="reklāmu kontam",
                            expires_at=utcnow() + timedelta(days=60))
            return RedirectResponse("/connect?connected=facebook", status_code=303)
        credentials.put(session, "fb_user_token", token,
                        label="reklāmu kontam",
                        expires_at=utcnow() + timedelta(days=60))
        return templates.TemplateResponse(request, "connect_pick_page.html",
                                          {"pages": pages})
    finally:
        session.close()


@app.post("/connect/facebook/select")
def connect_facebook_select(page_id: str = Form(...)):
    from urllib.parse import quote

    session = get_session()
    try:
        row = credentials.info(session, "fb_user_token")
        if not (row and row.value and (row.expires_at is None
                                       or row.expires_at > utcnow())):
            return RedirectResponse("/connect?error=Sesija+beigusies+—+savieno+vēlreiz",
                                    status_code=303)
        pages = credentials.fb_list_pages(row.value)
        match = next((p for p in pages if p["id"] == page_id), None)
        if match is None:
            return RedirectResponse("/connect?error=Lapa+nav+atrasta", status_code=303)
        credentials.put(session, "fb_page_id", match["id"], label=match.get("name", ""))
        credentials.put(session, "fb_page_token", match["access_token"],
                        label=match.get("name", ""))
        # the USER token stays stored: reklāmu kontam (adapters/meta_ads)
        # vajag lietotāja, ne lapas tokenu; ilgtermiņa tokens dzīvo ~60 dienas
        return RedirectResponse("/connect?connected=facebook", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/connect?error={quote(str(e)[:200])}", status_code=303)
    finally:
        session.close()


@app.post("/connect/facebook/disconnect")
def disconnect_facebook():
    """Drop the stored page connection so the admin can connect cleanly
    from scratch (the app credentials stay — only tokens are removed)."""
    session = get_session()
    try:
        for key in ("fb_page_id", "fb_page_token", "fb_user_token"):
            credentials.put(session, key, "", label="")
        if credentials.get("fb_page_token", session):
            return RedirectResponse(
                "/connect?error=Savienojums+noņemts+no+DB,+bet+vides+mainīgie+"
                "joprojām+satur+FB+atslēgu+—+izdzēs+FB_PAGE_ACCESS_TOKEN",
                status_code=303)
        return RedirectResponse("/connect?connected=Facebook+savienojums+noņemts",
                                status_code=303)
    finally:
        session.close()


@app.post("/connect/threads/disconnect")
def disconnect_threads():
    session = get_session()
    try:
        for key in ("threads_user_id", "threads_token"):
            credentials.put(session, key, "", label="")
        if credentials.get("threads_token", session):
            return RedirectResponse(
                "/connect?error=Savienojums+noņemts+no+DB,+bet+vides+mainīgie+"
                "joprojām+satur+Threads+atslēgu", status_code=303)
        return RedirectResponse("/connect?connected=Threads+savienojums+noņemts",
                                status_code=303)
    finally:
        session.close()


@app.get("/connect/threads")
def connect_threads(request: Request):
    from urllib.parse import quote

    session = get_session()
    try:
        state = credentials.new_state(session)
        url = credentials.threads_auth_url(
            f"{public_base(request)}/connect/threads/callback", state)
        return RedirectResponse(url, status_code=302)
    except Exception as e:  # noqa: BLE001
        log.exception("threads connect start failed")
        return RedirectResponse(
            f"/connect?error={quote(f'{type(e).__name__}: {str(e)[:180]}')}",
            status_code=303)
    finally:
        session.close()


@app.get("/connect/threads/callback")
def connect_threads_callback(request: Request, code: str = "", state: str = "",
                             error_description: str = ""):
    from urllib.parse import quote

    session = get_session()
    try:
        if error_description or not code:
            return RedirectResponse(
                f"/connect?error={quote(error_description or 'Threads atgrieza kļūdu')}",
                status_code=303)
        if not credentials.check_state(session, state):
            return RedirectResponse("/connect?error=OAuth+state+nesakrīt+—+mēģini+vēlreiz",
                                    status_code=303)
        try:
            user_id, token, expires = credentials.threads_exchange_code(
                code, f"{public_base(request)}/connect/threads/callback")
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(f"/connect?error={quote(str(e)[:200])}", status_code=303)
        credentials.put(session, "threads_user_id", user_id)
        credentials.put(session, "threads_token", token, expires_at=expires)
        return RedirectResponse("/connect?connected=threads", status_code=303)
    finally:
        session.close()


@app.get("/overview", response_class=HTMLResponse)
def overview_page(request: Request, saved: str = ""):
    from app import overview

    session = get_session()
    try:
        from app import weekend

        return templates.TemplateResponse(request, "overview.html", {
            "d": overview.build(session), "saved": saved,
            "weekend": weekend.settings(session),
        })
    finally:
        session.close()


@app.post("/overview/spend")
def overview_spend(monthly_eur: float = Form(0.0)):
    from app import overview

    session = get_session()
    try:
        overview.save_external_spend(session, monthly_eur)
        return RedirectResponse("/overview?saved=1", status_code=303)
    finally:
        session.close()


@app.post("/overview/weekend")
async def overview_weekend(request: Request):
    from app import weekend

    form = await request.form()
    session = get_session()
    try:
        weekend.save_settings(session, {f: form.get(f) == "on"
                                        for f in weekend.FEATURES})
        return RedirectResponse("/overview?saved=1", status_code=303)
    finally:
        session.close()


@app.post("/overview/ai-report")
def overview_ai_report():
    from app import overview

    session = get_session()
    try:
        overview.ai_report(session)
        return RedirectResponse("/overview", status_code=303)
    finally:
        session.close()


@app.get("/ads", response_class=HTMLResponse)
def ads_page(request: Request, saved: str = "", error: str = ""):
    from adapters.meta_ads import MetaAdsClient
    from app import ads

    from app.models import AdEntry

    session = get_session()
    try:
        plan = ads.build_plan(session)
        client = MetaAdsClient(session)
        if client.configured():
            ads_ready, ads_issues = client.readiness()
        else:
            ads_ready, ads_issues = False, [
                "reklāmu konts nav pieslēgts (Konti → Meta reklāmas)"]
        live = session.execute(
            select(AdEntry).where(AdEntry.status.in_(
                ("awaiting_approval", "active", "paused")))
            .order_by(AdEntry.updated_at.desc())
        ).scalars().all()
        return templates.TemplateResponse(request, "ads.html", {
            "s": plan["settings"], "plan": plan, "saved": saved,
            "error": error, "live": live,
            "ads_ready": ads_ready, "ads_issues": ads_issues,
        })
    finally:
        session.close()


@app.post("/ads/settings")
def ads_settings_save(mode: str = Form("off"), daily_budget: float = Form(0.0),
                      brand_share: int = Form(20)):
    from app import ads

    from adapters.meta_ads import MetaAdsClient

    session = get_session()
    try:
        # live režīmi prasa pieslēgtu reklāmu kontu; bez tā paliekam dry
        if mode in ("approve", "auto") and not MetaAdsClient(session).configured():
            mode = "dry"
        ads.save_settings(session, mode, daily_budget, brand_share)
        if mode != "off":
            ads.sync_entries(session)
        return RedirectResponse("/ads?saved=1", status_code=303)
    finally:
        session.close()


@app.post("/ads/{entry_id}/approve")
def ads_approve(entry_id: int):
    from urllib.parse import quote

    from adapters.meta_ads import MetaAdsClient
    from app import ads
    from app.models import AdEntry

    session = get_session()
    try:
        entry = session.get(AdEntry, entry_id)
        if entry is None or entry.status not in ("awaiting_approval", "planned"):
            return RedirectResponse("/ads", status_code=303)
        client = MetaAdsClient(session)
        if not client.configured():
            return RedirectResponse("/ads?error=konts+nav+pieslēgts", status_code=303)
        try:
            ads.launch_entry(session, client, entry)
        except Exception as e:  # noqa: BLE001
            entry.status = "rejected"
            entry.reason = f"palaišana neizdevās: {e}"
            session.commit()
            return RedirectResponse(f"/ads?error={quote(str(e)[:200])}", status_code=303)
        return RedirectResponse("/ads?saved=1", status_code=303)
    finally:
        session.close()


@app.post("/ads/{entry_id}/pause")
def ads_pause(entry_id: int):
    from adapters.meta_ads import MetaAdsClient
    from app.models import AdEntry

    session = get_session()
    try:
        entry = session.get(AdEntry, entry_id)
        if entry and entry.status in ("active", "awaiting_approval"):
            if entry.adset_id:
                try:
                    MetaAdsClient(session).set_status(entry.adset_id, "PAUSED")
                except Exception as e:  # noqa: BLE001
                    log.warning("manual pause failed: %s", e)
            entry.status = "paused" if entry.adset_id else "rejected"
            entry.reason = "manuāli apturēts"
            session.commit()
        return RedirectResponse("/ads", status_code=303)
    finally:
        session.close()


@app.post("/ads/{entry_id}/resume")
def ads_resume(entry_id: int):
    from adapters.meta_ads import MetaAdsClient
    from app.models import AdEntry

    session = get_session()
    try:
        entry = session.get(AdEntry, entry_id)
        if entry and entry.status == "paused" and entry.adset_id:
            try:
                MetaAdsClient(session).set_status(entry.adset_id, "ACTIVE")
                entry.status = "active"
                entry.reason = "manuāli atsākts"
                session.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("resume failed: %s", e)
        return RedirectResponse("/ads", status_code=303)
    finally:
        session.close()


@app.post("/ads/creative")
async def ads_upload_creative(request: Request):
    """Plānotāja kreatīvs: attēls, ko piesaistīt rakstam reklāmu variantiem."""
    from urllib.parse import quote

    from app.cards import CARDS_DIR
    from app.models import Article, CreativeAsset

    form = await request.form()
    url = str(form.get("article_url") or "").strip()
    upload = form.get("image")
    session = get_session()
    try:
        art = session.execute(
            select(Article).where((Article.canonical_url == url) | (Article.url == url))
        ).scalars().first()
        if art is None:
            return RedirectResponse("/ads?error=rakstu+ar+šādu+URL+neatradu",
                                    status_code=303)
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse("/ads?error=pievieno+attēla+failu", status_code=303)
        data = await upload.read()
        if not data:
            return RedirectResponse("/ads?error=tukšs+fails", status_code=303)
        import secrets as _secrets

        CARDS_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(upload.filename).suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return RedirectResponse("/ads?error=atbalstīti+png/jpg/webp",
                                    status_code=303)
        path = CARDS_DIR / f"creative_{_secrets.token_hex(6)}{ext}"
        path.write_bytes(data)
        session.add(CreativeAsset(article_id=art.id, path=str(path),
                                  note=upload.filename))
        session.commit()
        return RedirectResponse("/ads?saved=1", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/ads?error={quote(str(e)[:200])}", status_code=303)
    finally:
        session.close()


@app.post("/connect/meta-ads")
def connect_meta_ads(ad_account_id: str = Form(""), pixel_id: str = Form("")):
    from urllib.parse import quote

    session = get_session()
    try:
        acct = ad_account_id.strip().replace("act_", "")
        if acct:
            label = ""
            token = credentials.get("fb_user_token", session)
            if token:
                try:
                    accounts = credentials.fb_list_ad_accounts(token)
                    match = next((a for a in accounts
                                  if str(a.get("account_id")) == acct), None)
                    if match:
                        label = match.get("name", "")
                except Exception as e:  # noqa: BLE001 — saglabājam arī neverificētu
                    log.warning("ad account lookup failed: %s", e)
            credentials.put(session, "fb_ad_account_id", acct, label=label)
        if pixel_id.strip():
            credentials.put(session, "meta_pixel_id", pixel_id.strip())
        if not acct and not pixel_id.strip():
            return RedirectResponse("/connect?error=Norādi+reklāmu+konta+ID",
                                    status_code=303)
        return RedirectResponse("/connect?connected=meta_ads", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/connect?error={quote(str(e)[:200])}", status_code=303)
    finally:
        session.close()


@app.post("/connect/x-ads")
def connect_x_ads(ads_account_id: str = Form("")):
    session = get_session()
    try:
        credentials.put(session, "x_ads_account_id", ads_account_id.strip())
        return RedirectResponse("/connect?connected=x_ads", status_code=303)
    finally:
        session.close()


@app.post("/connect/meta-ads/disconnect")
def disconnect_meta_ads():
    session = get_session()
    try:
        for key in ("fb_ad_account_id", "meta_pixel_id"):
            credentials.put(session, key, "", label="")
        return RedirectResponse("/connect?connected=Reklāmu+konts+atvienots",
                                status_code=303)
    finally:
        session.close()


EDITABLE = {
    "rules": config.RULES_DIR / "rules.yaml",
    "channels": config.RULES_DIR / "channels.yaml",
    "feeds": config.RULES_DIR / "feeds.yaml",
    "prompt_base": config.PROMPTS_DIR / "system_base.md",
    "prompt_facebook": config.PROMPTS_DIR / "system_facebook.md",
    "prompt_x": config.PROMPTS_DIR / "system_x.md",
    "prompt_threads": config.PROMPTS_DIR / "system_threads.md",
}


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, saved: str = "", error: str = ""):
    files = {k: (p.read_text(encoding="utf-8") if p.exists() else "")
             for k, p in EDITABLE.items()}
    return templates.TemplateResponse(
        request, "settings.html",
        {"files": files, "saved": saved, "error": error,
         # jaunas atslēgas, kas kodā ir, bet šīs instances kopijā nav: bez
         # brīdinājuma redaktors par tām neuzzina nekad
         "missing_rules": config.missing_rules(),
         "missing_channels": config.missing_channels()})


@app.post("/settings/sync-rules")
def settings_sync_rules():
    """Pieliek noteikumus, kas ir kodā, bet ne šīs instances kopijā.

    Startējot tas notiek pats; poga ir tam gadījumam, kad brīdinājums
    tomēr redzams — lai to varētu nokārtot uz vietas, nevis kopējot ar roku.
    """
    from urllib.parse import quote

    added = config.sync_missing_rules()
    if not added:
        return RedirectResponse("/settings?saved=nekas+nebija+jāpievieno",
                                status_code=303)
    return RedirectResponse(
        f"/settings?saved={quote('pievienoti noteikumi: ' + ', '.join(added))}",
        status_code=303)


@app.post("/settings/{kind}")
def save_settings(kind: str, content: str = Form(...)):
    path = EDITABLE.get(kind)
    if path is None:
        return RedirectResponse("/settings?error=unknown+file", status_code=303)
    err = config.validate_editable(kind, content)
    if err:
        from urllib.parse import quote

        return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)
    path.write_text(content, encoding="utf-8")
    return RedirectResponse(f"/settings?saved={kind}", status_code=303)
