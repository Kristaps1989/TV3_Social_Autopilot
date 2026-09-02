#!/usr/bin/env python3
"""Kāpēc plūsmā ir tieši šie formāti — viss pamatojums komandrindā.

Tie paši dati ir sadaļā «Diagnostika» (/logs) un lejupielādējami kā JSON
(/logs/export.json) — šis skripts noder, kad lapa nav pieejama vai gribi
izvadi terminālī.

    python scripts/format_report.py                 # visi kanāli
    python scripts/format_report.py --channel fb_tv3lv --simulate
    python scripts/format_report.py --json > diagnostika.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISABLE_SCHEDULER", "true")

from app import diagnostics  # noqa: E402
from app.db import get_session  # noqa: E402


def show(data: dict) -> None:
    print(f"Noteikumu mape: {data['rules_dir']}"
          + ("  (rediģējamā kopija)" if data["editable_rules_used"] else "  (koda noklusējumi)"))
    print(f"Renderētājs: {'ok' if data['renderer_available'] else 'NAV'} · "
          f"lentes: {'ok' if data['reels_available'] else 'NAV'} · "
          f"reklāmas: {data['ads_mode']}")
    print(f"Pēdējās 24 h publicēts: {data['published_24h'] or 'nekā'}")
    if data["last_render_error"]:
        print(f"Pēdējā zīmēšanas kļūda: {data['last_render_error']}")
    if data["ads"]:
        print("\nFormātu maksas rezultāti (rezultāti par eiro):")
        for r in data["ads"]:
            print(f"  {r['format']:<15} {r['ads']} reklāmas · {r['eur']:.2f} € · "
                  f"{r['per_eur'] if r['per_eur'] is not None else '-'}")

    for ch in data["channels"]:
        print("\n" + "=" * 78)
        print(f"KANĀLS {ch['channel']} — {ch['display_name']}")
        print("=" * 78)
        print(f"Pēdējie ieraksti: {', '.join(ch['window']) or '-'}")
        print(f"Plūsmas galā: {ch['run']['format'] or '-'} x{ch['run']['count']} "
              f"(limits {ch['row_limit']} pēc kārtas)")
        print(f"{'formāts':<15}{'daļa':>7}{'šodien':>8}{'kvota':>7}{'grīda':>7}"
              f"{'griesti':>9}  statuss")
        for s in ch["status"]:
            floor = f"{s['floor'] * 100:.0f}%" if s["floor"] else "-"
            ceiling = f"{s['ceiling'] * 100:.0f}%" if s["ceiling"] else "-"
            status = s["blocked"] or ("GRĪDA: priekšroka" if s["starved"] else "brīvs")
            print(f"{s['format']:<15}{s['share'] * 100:>6.0f}%{s['today']:>8}"
                  f"{str(s['cap'] or '-'):>7}{floor:>7}{ceiling:>9}  {status}")
        if ch["history"]:
            print("\nPēdējie ieraksti un iemesli:")
            for h in ch["history"]:
                print(f"  {h['at']:%d.%m %H:%M}  {h['format']:<14} {h['state']:<10} {h['title']}")
                if h["decision"]:
                    print(f"       izvēle: {h['decision']}")
                for note in h["notes"]:
                    print(f"       ↳ {note}")
                if h["retargeted"]:
                    print(f"       ↳ {h['retargeted']}")
                for fmt, why in (h["blocked"] or {}).items():
                    print(f"       ↳ bloķēts {fmt}: {why}")

    sim = data.get("simulation")
    if sim:
        print("\n" + "=" * 78)
        print(f"SIMULĀCIJA ar rakstu: {sim['article'][:60]}")
        for name, rows in sim["channels"].items():
            print(f"\n  {name}")
            for r in rows:
                print(f"    AI grib «{r['ai_choice']}» → {r['chosen']}  ({r['decision']})")
                if r.get("gate"):
                    print(f"       bagātā formāta vārti: {r['gate']}")
                for fmt, why in (r["blocked"] or {}).items():
                    print(f"       bloķēts {fmt}: {why}")
                for fmt, sc in (r["scores"] or {}).items():
                    parts = ", ".join(f"{k} {v}" for k, v in sc.items() if k != "total")
                    print(f"       {fmt:<14} {sc['total']:.2f}  ({parts})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="", help="tikai šis kanāls")
    ap.add_argument("--posts", type=int, default=15, help="cik ierakstu vēsturē")
    ap.add_argument("--simulate", action="store_true",
                    help="ko izvēlētos ŠOBRĪD ar jaunāko rakstu")
    ap.add_argument("--json", action="store_true", help="mašīnlasāms izvads")
    args = ap.parse_args()

    session = get_session()
    try:
        data = diagnostics.report(session, channel=args.channel, posts=args.posts,
                                  simulate_article=args.simulate or args.json)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        else:
            show(data)
    finally:
        session.close()


if __name__ == "__main__":
    main()
