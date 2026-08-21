"""AI decision layer: publish or not, where, in what format, with what copy.

Uses Claude via the Anthropic API when ANTHROPIC_API_KEY is set. Every call
is logged to decisions_log. If the API is unavailable or returns invalid
output, a deterministic fallback keeps the pipeline running: link post,
copy from the headline, next free slot.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import select

from app import config
from app.best_practices import pick_format
from app.models import Article, DecisionLog, Post
from app.rules_engine import Verdict

log = logging.getLogger(__name__)

DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record the publishing decision for this article.",
    "input_schema": {
        "type": "object",
        "properties": {
            "publish": {"type": "boolean"},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {
                "type": "array",
                "items": {"type": "string",
                          "enum": ["none", "nudity", "party", "alcohol", "tragedy", "crime"]},
            },
            "channels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string"},
                        "format": {"type": "string",
                                   "enum": ["link", "photo", "photo_album", "text_only",
                                            "carousel", "video"]},
                        "copy": {"type": "string"},
                        "hashtags": {"type": "array", "items": {"type": "string"}},
                        "image_index": {"type": "integer"},
                        "preferred_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    },
                    "required": ["channel", "format", "copy"],
                },
            },
        },
        "required": ["publish", "score", "reason", "channels"],
    },
}


def recent_posts_context(session, channels: list[str], limit: int = 10) -> str:
    lines = []
    for ch in channels:
        rows = session.execute(
            select(Post).where(Post.channel == ch,
                               Post.state.in_(("scheduled", "published")))
            .order_by(Post.created_at.desc()).limit(limit)
        ).scalars().all()
        for p in rows:
            title = p.article.title if p.article else ""
            lines.append(f"- [{ch}] {p.format}: {title}")
    return "\n".join(lines) if lines else "(nav nesenu ierakstu)"


def build_user_prompt(article: Article, verdicts: dict[str, Verdict],
                      channels_cfg: dict, session) -> str:
    eligible = {n: v for n, v in verdicts.items() if v.outcome in ("eligible", "forced_now")}
    channel_desc = []
    for name, v in eligible.items():
        cfg = channels_cfg.get(name) or {}
        channel_desc.append(
            f"- {name} ({cfg.get('platform')}): formāti {cfg.get('formats')}, "
            f"statuss: {v.reason}"
        )
    return f"""Raksts:
Virsraksts: {article.title}
Ievads: {article.lead[:600]}
Sadaļa: {article.section}
Attēli: {len(article.images or [])}
Redaktora statuss: {article.editor_status}
Publicēts: {article.published_at}

Pieejamie kanāli:
{chr(10).join(channel_desc)}

Nesenie ieraksti (neatkārto leņķus):
{recent_posts_context(session, list(eligible))}

Pieņem lēmumu ar record_decision. Ja raksts nav pietiekami interesants
('can' statuss ļauj izlaist), atzīmē publish=false ar īsu iemeslu latviski."""


def call_claude(article: Article, verdicts: dict[str, Verdict], session) -> dict | None:
    from app import credentials

    api_key = credentials.get("anthropic_api_key", session)
    if not api_key:
        return None
    import anthropic

    channels_cfg = config.load_channels()
    strong = article.editor_status in ("now", "must")
    model = config.AI_MODEL_STRONG if strong else config.AI_MODEL_FAST
    system = config.system_prompt_for("")  # base style guide; channel guides are appended below
    platforms = {(channels_cfg.get(n) or {}).get("platform", "")
                 for n, v in verdicts.items() if v.outcome != "blocked"}
    for p in sorted(platforms):
        system += "\n\n" + config.system_prompt_for(p)
    user = build_user_prompt(article, verdicts, channels_cfg, session)

    client = anthropic.Anthropic(api_key=api_key)
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model, max_tokens=1500, system=system,
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": user}],
            )
            decision = next((b.input for b in resp.content if b.type == "tool_use"), None)
            session.add(DecisionLog(
                article_id=article.id, model=model,
                prompt_hash=hashlib.sha256((system + user).encode()).hexdigest()[:16],
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                raw_response=json.dumps(decision, ensure_ascii=False) if decision else "",
            ))
            if decision and validate_decision(decision):
                return decision
            log.warning("invalid AI decision for article %s (attempt %d)", article.id, attempt)
        except Exception as e:  # noqa: BLE001
            log.warning("Claude call failed for article %s: %s", article.id, e)
            break
    return None


def validate_decision(d: dict) -> bool:
    if not isinstance(d.get("publish"), bool):
        return False
    if d["publish"] and not d.get("channels"):
        return False
    for ch in d.get("channels") or []:
        if not ch.get("channel") or not isinstance(ch.get("copy"), str):
            return False
    return True


def fallback_decision(article: Article, verdicts: dict[str, Verdict]) -> dict:
    """Deterministic safe default: link post with headline copy on every
    eligible channel for must/now; skip weak 'can' items (no AI to judge them)."""
    channels_cfg = config.load_channels()
    eligible = [n for n, v in verdicts.items() if v.outcome in ("eligible", "forced_now")]
    publish = article.editor_status in ("now", "must") and bool(eligible)
    copy = article.title
    if article.lead:
        copy = f"{article.title} — {article.lead[:150]}"
    return {
        "publish": publish,
        "score": {"now": 0.95, "must": 0.8}.get(article.editor_status, 0.4),
        "reason": ("fallback: publicēts pēc redaktora statusa" if publish
                   else "fallback: 'can' bez AI vērtējuma netiek publicēts"),
        "labels": [article.section],
        "sensitivity": [],
        "channels": [
            {
                "channel": name,
                "format": pick_format(article.section, article.images or [],
                                      article.editor_status,
                                      (channels_cfg.get(name) or {}).get("formats") or ["link"]),
                "copy": copy,
                "hashtags": [],
                "image_index": 0,
            }
            for name in eligible
        ] if publish else [],
    }


def decide(article: Article, verdicts: dict[str, Verdict], session) -> dict:
    decision = call_claude(article, verdicts, session)
    if decision is None:
        decision = fallback_decision(article, verdicts)
    article.decided_at = datetime.utcnow()
    article.ai_score = float(decision.get("score") or 0)
    article.ai_reason = str(decision.get("reason") or "")
    article.labels = decision.get("labels") or []
    article.sensitivity = [s for s in (decision.get("sensitivity") or []) if s != "none"]
    return decision
