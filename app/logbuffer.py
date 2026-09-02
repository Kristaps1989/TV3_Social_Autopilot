"""Pēdējie žurnāla ieraksti atmiņā, lai tos varētu skatīt pašā rīkā.

Railway logi ir pieejami tikai caur to konsoli, un tieši tad, kad kaut kas
ir greizi, tur rakāties ir vislielākā berze. Šis handleris tur pēdējos
ierakstus gredzena buferī — sadaļa «Diagnostika» tos parāda, un no turienes
tos var lejupielādēt kā JSON.

Buferis dzīvo procesā: konteinera pārstarts to notīra. Tas ir apzināti —
žurnāls nav datu bāze, un pastāvīgai vēsturei ir ieraksti un vērtējumi.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timezone

MAX_RECORDS = 800
_records: deque[dict] = deque(maxlen=MAX_RECORDS)

# Kas nedrīkst nonākt izdrukā, ko sūta tālāk: tokeni un atslēgas. Žurnālā tie
# nenonāk arī tagad, bet eksports iet ārā no sistēmas, tāpēc filtrs ir lēts.
_SECRETS = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]+|EAA[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._\-]{20,}"
    r"|access_token=[^&\s]+|[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{32,})")


def scrub(text: str) -> str:
    return _SECRETS.sub("«noslēpums»", str(text or ""))


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — žurnāls nedrīkst gāzt izsaucēju
            message = str(record.msg)
        _records.append({
            "at": datetime.fromtimestamp(record.created, tz=timezone.utc)
                          .strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(message)[:2000],
        })


def install(level: int = logging.INFO) -> None:
    """Pieliek buferi saknes žurnālam (vienreiz)."""
    root = logging.getLogger()
    if any(isinstance(h, RingHandler) for h in root.handlers):
        return
    handler = RingHandler()
    handler.setLevel(level)
    root.addHandler(handler)


def records(limit: int = 200, level: str = "", contains: str = "") -> list[dict]:
    """Jaunākie pirmie, ar vienkāršu filtru pēc līmeņa un teksta."""
    rows = list(_records)
    if level:
        wanted = {"WARNING": ("WARNING", "ERROR", "CRITICAL"),
                  "ERROR": ("ERROR", "CRITICAL")}.get(level.upper(), (level.upper(),))
        rows = [r for r in rows if r["level"] in wanted]
    if contains:
        needle = contains.lower()
        rows = [r for r in rows
                if needle in r["message"].lower() or needle in r["logger"].lower()]
    return list(reversed(rows))[:limit]


def clear() -> None:
    _records.clear()
