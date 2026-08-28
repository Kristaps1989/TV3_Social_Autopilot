"""Admin UI + healthcheck. Deliberately boring: server-rendered pages,
plain HTML forms, zero JavaScript build step — easy to support."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from app import auth, config, credentials, ga4, reels, runtime
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

# Pages reachable without a session: healthcheck, the auth pages, and
# rendered card images (unguessable names; platforms must be able to fetch them).
PUBLIC_PATHS = {"/health", "/login", "/setup"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """The admin UI holds the kill switch and platform tokens — everything
    except /health requires a logged-in session. With no password configured
    yet, every page redirects to the one-time /setup screen."""
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/media/"):
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


templates.env.filters["local"] = to_local
templates.env.filters["basename"] = lambda p: Path(str(p)).name


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
                "daily_cap": (cfg or {}).get("daily_cap", "—"),
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
            from app.best_practices import sanitize_copy

            channels = config.load_channels()
            platform = (channels.get(post.channel) or {}).get("platform", "")
            sens = post.article.sensitivity if post.article else []
            post.copy, _, _ = sanitize_copy(copy, post.hashtags or [], platform, sens,
                                            reserve_link_chars=True)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.get("/post/{post_id}/preview", response_class=HTMLResponse)
def post_preview(request: Request, post_id: int):
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post is None:
            return RedirectResponse("/", status_code=303)
        channels = config.load_channels()
        cfg = channels.get(post.channel) or {}
        platform = cfg.get("platform", "")
        from app.best_practices import add_utm, assemble_post_text

        link = add_utm(post.link_url, platform, post.id) if post.link_url else ""
        full_text = assemble_post_text(post.copy, post.hashtags or [], link, platform)
        article = post.article
        img_portrait = False
        if article and post.format == "link":
            from app import imageinfo

            try:
                img_portrait = imageinfo.orientation(article) == "portrait"
                session.commit()  # keep the probed size cached
            except Exception:  # noqa: BLE001
                img_portrait = False
        return templates.TemplateResponse(request, "preview.html", {
            "post": post, "article": article, "platform": platform,
            "channel_name": cfg.get("display_name", post.channel),
            "full_text": full_text, "link": link,
            "og_image": (article.images or [""])[0] if article else "",
            "img_portrait": img_portrait,
        })
    finally:
        session.close()


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    from app import ga4, priors

    session = get_session()
    try:
        channels = config.load_channels()
        summaries = [priors.channel_summary(session, ch) for ch in channels]
        return templates.TemplateResponse(request, "stats.html", {
            "summaries": summaries,
            "top": priors.top_posts(session, 10),
            "ga4_on": ga4.configured(),
            "dry_run": runtime.is_dry_run(session),
        })
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
def why(request: Request, url: str = ""):
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
        })
    finally:
        session.close()


@app.get("/articles", response_class=HTMLResponse)
def articles(request: Request):
    session = get_session()
    try:
        rows = session.execute(
            select(Article).order_by(desc(Article.first_seen_at)).limit(60)
        ).scalars().all()
        return templates.TemplateResponse(request, "articles.html", {"articles": rows})
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

        def _env(name: str, secret: bool = False) -> str:
            value = os.environ.get(name, "")
            if not value:
                return ""
            return "uzstādīts ✓" if secret else value

        ga4_sa = credentials.info(session, "ga4_service_account")
        vol = runtime.data_dir_persistent()
        from app import cards
        render_ok, render_err = cards.renderer_check()
        env_diag = {
            "Datu disks (Volume)": ("pastāvīgs ✓" if vol
                                    else "lokāla vide" if vol is None else ""),
            "Attēlu renderētājs (Chromium)": (
                "strādā ✓" if render_ok
                else f"NESTRĀDĀ — foto/story bez virsraksta plāksnes: {render_err}"),
            "Video (ffmpeg)": ("strādā ✓" if reels.ffmpeg_bin()
                               else "nav — reel formāts izslēgts"),
            "META_APP_ID": _env("META_APP_ID"),
            "META_APP_SECRET": _env("META_APP_SECRET", secret=True),
            "META_LOGIN_CONFIG_ID": _env("META_LOGIN_CONFIG_ID"),
            "PUBLIC_BASE_URL": _env("PUBLIC_BASE_URL"),
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
def connect_ga4(property_id: str = Form(""), service_account: str = Form("")):
    """Save GA4 settings from the UI. Empty fields keep their current value."""
    import json as _json
    from urllib.parse import quote

    session = get_session()
    try:
        if property_id.strip():
            credentials.put(session, "ga4_property_id", property_id.strip())
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
            return RedirectResponse("/connect?connected=facebook", status_code=303)
        # several pages: keep the user token briefly and let the admin pick
        credentials.put(session, "fb_user_token", user_token,
                        expires_at=utcnow() + timedelta(minutes=15))
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
            return RedirectResponse("/connect?connected=facebook", status_code=303)
        credentials.put(session, "fb_user_token", token,
                        expires_at=utcnow() + timedelta(minutes=15))
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
        if not (row and row.value and row.expires_at and row.expires_at > utcnow()):
            return RedirectResponse("/connect?error=Sesija+beigusies+—+savieno+vēlreiz",
                                    status_code=303)
        pages = credentials.fb_list_pages(row.value)
        match = next((p for p in pages if p["id"] == page_id), None)
        if match is None:
            return RedirectResponse("/connect?error=Lapa+nav+atrasta", status_code=303)
        credentials.put(session, "fb_page_id", match["id"], label=match.get("name", ""))
        credentials.put(session, "fb_page_token", match["access_token"],
                        label=match.get("name", ""))
        credentials.put(session, "fb_user_token", "")  # done with it
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
    return templates.TemplateResponse(request, "settings.html",
                                      {"files": files, "saved": saved, "error": error})


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
