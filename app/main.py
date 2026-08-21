"""Admin UI + healthcheck. Deliberately boring: server-rendered pages,
plain HTML forms, zero JavaScript build step — easy to support."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from app import config
from app.db import get_session, init_db
from app.models import Article, Evaluation, Post, get_setting, set_setting, utcnow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    import os

    scheduler = None
    if os.environ.get("DISABLE_SCHEDULER", "").lower() != "true":
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
        log.info("scheduler started (dry_run=%s)", config.DRY_RUN)
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="TV3 Social Autopilot", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def to_local(dt):
    if dt is None:
        return ""
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE)).strftime("%d.%m. %H:%M")


templates.env.filters["local"] = to_local


@app.get("/health")
def health():
    session = get_session()
    try:
        session.execute(select(Post.id).limit(1))
        return {"status": "ok", "dry_run": config.DRY_RUN}
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
            "dry_run": config.DRY_RUN,
        })
    finally:
        session.close()


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
