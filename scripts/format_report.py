#!/usr/bin/env python3
"""Kāpēc plūsmā ir tieši šie formāti — viss pamatojums vienā izdrukā.

Palaišana (projekta saknē vai Railway konsolē):

    python scripts/format_report.py                 # visi kanāli
    python scripts/format_report.py --channel fb_tv3lv
    python scripts/format_report.py --posts 30      # cik ierakstu vēsturē
    python scripts/format_report.py --simulate      # ko izvēlētos ŠOBRĪD

Ko rāda:
  1. kanāla konfigurācija — formāti, svari, grīdas, griesti, dienas kvotas;
  2. pēdējie ieraksti pa formātiem (daļas, cik pēc kārtas, šodienas skaits);
  3. katra formāta statuss TAGAD: vai drīkst, un ja nē — kurš sargs to tur;
  4. ierakstu vēsture ar piezīmi, kāpēc AI ieteiktais formāts netika izpildīts
     (`extra.format_notes`) un vai saites ieraksts kļuva par foto
     (`extra.retargeted`);
  5. ar --simulate: pilna izvēles pēda (svari × izmērītais × sadaļa ×
     reklāma × piesātinājums × AI bonuss) katram derīgajam formātam.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISABLE_SCHEDULER", "true")

from sqlalchemy import desc, select  # noqa: E402

from app import config, formats, pipeline  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Article, Post, utcnow  # noqa: E402

LIVE_STATES = ("scheduled", "publishing", "published")


def line(char: str = "-", width: int = 78) -> None:
    print(char * width)


def channel_report(session, name: str, cfg: dict, posts: int, simulate: bool) -> None:
    line("=")
    print(f"KANĀLS {name} — {cfg.get('display_name', name)}")
    line("=")
    allowed = cfg.get("formats") or []
    floors = {**formats.DEFAULT_FORMAT_MIX, **(cfg.get("format_mix") or {})}
    ceilings = {**formats.DEFAULT_FORMAT_MAX_SHARE, **(cfg.get("format_max_share") or {})}
    caps = {**pipeline.DEFAULT_FORMAT_DAILY_CAP, **(cfg.get("format_daily_cap") or {})}
    weights = {**formats.DEFAULT_FORMAT_WEIGHTS, **(cfg.get("format_weights") or {})}
    print(f"Formāti: {', '.join(allowed) or '-'}")
    print(f"Svari: {weights}")
    print(f"Grīdas (format_mix): {floors or '-'}")
    print(f"Griesti (format_max_share): {ceilings or '-'}")
    print(f"Dienas kvotas (format_daily_cap): {caps or '-'}")
    print(f"Pēc kārtas viena formāta max (max_same_format_in_row): "
          f"{formats.row_limit(cfg)}")

    window = formats.recent_formats(session, name)
    shares = formats.recent_format_shares(session, name)
    head, run = formats.format_run(session, name)
    print()
    print(f"Pēdējie {len(window)} ieraksti (jaunākais pirmais): "
          f"{', '.join(window) or '-'}")
    print("Daļas: " + (", ".join(f"{f} {v:.0%}" for f, v in sorted(shares.items()))
                       or "-"))
    print(f"Plūsmas galā: {head or '-'} ×{run}")

    today = {f: pipeline.posts_today(session, name, f) for f in allowed}
    print("Šodien (Rīgas datums): "
          + (", ".join(f"{f} {n}" for f, n in today.items() if n) or "nekā"))

    print()
    print("Formātu statuss TAGAD:")
    for fmt in allowed:
        bits = []
        penalty, why = formats.monotony_state(session, name, cfg, fmt)
        if why:
            bits.append(f"vienveidība ({penalty}): {why}")
        cap = caps.get(fmt)
        if cap is not None and today.get(fmt, 0) >= int(cap):
            bits.append(f"dienas kvota {today.get(fmt, 0)}/{cap}")
        floor = floors.get(fmt)
        if floor and shares.get(fmt, 0.0) < float(floor):
            bits.append(f"GRĪDA: daļa {shares.get(fmt, 0.0):.0%} zem {float(floor):.0%}"
                        " — šim formātam ir priekšroka")
        print(f"  {fmt:<15} " + ("; ".join(bits) if bits else "brīvs"))

    rows = session.execute(
        select(Post).where(Post.channel == name, Post.state.in_(LIVE_STATES + ("cancelled",)))
        .order_by(desc(Post.created_at)).limit(posts)
    ).scalars().all()
    print()
    print(f"Pēdējie {len(rows)} ieraksti un to iemesli:")
    for p in rows:
        when = p.published_at or p.scheduled_at or p.created_at
        title = (p.article.title if p.article else "")[:46]
        print(f"  {when:%d.%m %H:%M}  {p.format:<14} {p.state:<10} {title}")
        extra = p.extra or {}
        for note in extra.get("format_notes") or []:
            print(f"       ↳ {note}")
        if extra.get("retargeted"):
            r = extra["retargeted"]
            print(f"       ↳ {r.get('from')} → {p.format}: FB kartīte nogrieztu "
                  f"{float(r.get('link_card_crop') or 0) * 100:.0f}% augstuma")
        trace = extra.get("format_trace")
        if trace:
            print(f"       ↳ izvēle: {trace.get('decision', '')}"
                  + (f"; bloķēti: {trace.get('blocked')}" if trace.get("blocked") else ""))

    if simulate:
        art = session.execute(
            select(Article).where(Article.title != "")
            .order_by(desc(Article.first_seen_at)).limit(1)).scalars().first()
        if art is None:
            return
        print()
        print(f"SIMULĀCIJA ar jaunāko rakstu: {art.title[:60]}")
        for ai_choice in ("", "card_carousel", "reel", "link"):
            trace = formats.explain(session, name, cfg, art, ai_choice or None)
            print(f"  AI grib «{ai_choice or '—'}» → {trace['chosen']} "
                  f"({trace.get('decision', '')})")
            if trace.get("blocked"):
                for fmt, why in trace["blocked"].items():
                    print(f"       bloķēts {fmt}: {why}")
            for fmt, sc in (trace.get("scores") or {}).items():
                parts = ", ".join(f"{k} {v}" for k, v in sc.items() if k != "total")
                print(f"       {fmt:<14} {sc['total']:.2f}  ({parts})")
            if ai_choice in pipeline.RICH_FORMATS:
                gate = pipeline.rich_format_gate(session, name, cfg, art, ai_choice)
                print(f"       bagātā formāta vārti: {gate or 'atvērti'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="", help="tikai šis kanāls")
    ap.add_argument("--posts", type=int, default=20, help="cik ierakstu vēsturē")
    ap.add_argument("--simulate", action="store_true",
                    help="ko izvēlētos ŠOBRĪD ar jaunāko rakstu")
    ap.add_argument("--json", action="store_true", help="mašīnlasāms kopsavilkums")
    args = ap.parse_args()

    session = get_session()
    try:
        channels = config.load_channels()
        if args.channel:
            channels = {k: v for k, v in channels.items() if k == args.channel}
        if args.json:
            out = {}
            for name, cfg in channels.items():
                out[name] = {
                    "window": formats.recent_formats(session, name),
                    "shares": formats.recent_format_shares(session, name),
                    "run": dict(zip(("format", "count"), formats.format_run(session, name))),
                    "today": {f: pipeline.posts_today(session, name, f)
                              for f in (cfg.get("formats") or [])},
                    "blocked": {f: formats.monotony_reason(session, name, cfg, f)
                                for f in (cfg.get("formats") or [])
                                if formats.monotony_reason(session, name, cfg, f)},
                }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        since = utcnow() - timedelta(hours=24)
        published = session.execute(
            select(Post).where(Post.state == "published", Post.published_at >= since)
        ).scalars().all()
        print(f"Pēdējās 24 h publicēts: "
              f"{dict(Counter(p.format for p in published)) or 'nekā'}")
        for name, cfg in channels.items():
            channel_report(session, name, cfg or {}, args.posts, args.simulate)
    finally:
        session.close()


if __name__ == "__main__":
    main()
