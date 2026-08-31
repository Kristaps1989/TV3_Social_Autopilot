"""Balss sintēze reelu ierunai: teksts -> mp3 (Azure Speech).

Reela ierunas teksts (`voice_script`) top lēmumu solī un glabājas receptē;
šis modulis to pārvērš skaņā. Azure dod divas īstas latviešu neironu
balsis, un SSML ļauj pateikt, kā runāt: ziņu tempu, pauzi pirms aicinājuma
lasīt, un — svarīgākais — normalizētus skaitļus un datumus, ko citādi balss
nolasa pa ciparam.

Viss ir izslēdzams: bez atslēgas `synthesize()` atgriež tukšu virkni, un
reels sanāk kluss tieši tāpat kā līdz šim. Neviena kļūda te nedrīkst
apturēt lentes būvēšanu — labāk kluss reels nekā nekāds.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from pathlib import Path
from xml.sax.saxutils import escape

from app import config, credentials

log = logging.getLogger(__name__)

# Latviešu neironu balsis Azure Speech katalogā.
VOICES = {
    "female": "lv-LV-EveritaNeural",
    "male": "lv-LV-NilsNeural",
}
DEFAULT_VOICE = VOICES["female"]
# Ziņu ierunai neitrāls temps; nedaudz lēnāk par noklusējumu, jo lentē
# skatītājs vienlaikus lasa arī kadra tekstu.
DEFAULT_RATE = "-4%"
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
TIMEOUT = 30


def _key(session=None) -> str:
    return credentials.get("azure_speech_key", session)


def _region(session=None) -> str:
    return credentials.get("azure_speech_region", session) or "westeurope"


def enabled(rules: dict | None = None, session=None) -> bool:
    """Vai ierunu vispār drīkst un var uztaisīt."""
    rules = config.load_rules() if rules is None else rules
    return bool((rules or {}).get("reel_voice", True)) and bool(_key(session))


def voice_name(rules: dict | None = None) -> str:
    rules = config.load_rules() if rules is None else rules
    choice = str((rules or {}).get("reel_voice_name") or "").strip()
    return VOICES.get(choice.lower(), choice or DEFAULT_VOICE)


def build_ssml(text: str, voice: str = DEFAULT_VOICE,
               rate: str = DEFAULT_RATE) -> str:
    """SSML dokuments vienam ierunas tekstam.

    Teikumu robežas kļūst par īsām pauzēm: bez tām neironu balss ziņu
    tekstu izstāsta vienā elpas vilcienā, un klausītājam nesanāk saprast,
    kur beidzas viens fakts un sākas nākamais.
    """
    parts = [escape(p.strip()) for p in re.split(r"(?<=[.!?])\s+", text.strip())
             if p.strip()]
    body = '<break time="260ms"/>'.join(parts)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="lv-LV">'
        f'<voice name="{escape(voice, {chr(34): "&quot;"})}">'
        f'<prosody rate="{escape(rate, {chr(34): "&quot;"})}">{body}</prosody>'
        "</voice></speak>"
    )


def _cache_path(text: str, voice: str, out_dir: Path) -> Path:
    """Viens un tas pats teksts ar to pašu balsi = tas pats fails.

    Pārzīmējot reelu, teksts parasti nemainās; bez keša katrs mēģinājums
    būtu jauns Azure pieprasījums par to pašu skaņu.
    """
    digest = hashlib.sha256(f"{voice}\n{text}".encode()).hexdigest()[:16]
    return out_dir / f"voice_{digest}.mp3"


def synthesize(text: str, out_dir: Path | str | None = None,
               rules: dict | None = None, session=None,
               force: bool = False) -> str:
    """Ierunas mp3 ceļš ('' ja balss nav pieejama vai neizdodas).

    Nemet kļūdu: ja Azure neatbild, reels vienkārši iznāk kluss.

    force=True apiet kešu. To vajag atslēgas pārbaudei: citādi tas pats
    parauga teikums atbild no iepriekšējās atslēgas faila, un nederīga
    atslēga tiktu apstiprināta kā strādājoša.
    """
    text = (text or "").strip()
    if not text:
        return ""
    rules = config.load_rules() if rules is None else rules
    if not enabled(rules, session):
        return ""

    from app import cards

    out_dir = Path(out_dir or cards.CARDS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    voice = voice_name(rules)
    cached = _cache_path(text, voice, out_dir)
    if not force and cached.exists() and cached.stat().st_size > 0:
        return str(cached)

    import httpx

    url = (f"https://{_region(session)}.tts.speech.microsoft.com"
           "/cognitiveservices/v1")
    try:
        resp = httpx.post(
            url, timeout=TIMEOUT,
            headers={
                "Ocp-Apim-Subscription-Key": _key(session),
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
                "User-Agent": "TV3-Social-Autopilot/1.0",
            },
            content=build_ssml(text, voice).encode("utf-8"))
        if resp.status_code != 200:
            log.warning("TTS failed: HTTP %s %s", resp.status_code,
                        resp.text[:200])
            return ""
        if not resp.content:
            return ""
    except Exception as e:  # noqa: BLE001 — kluss reels ir labāks par nekādu
        log.warning("TTS request failed: %s", e)
        return ""

    # rakstām caur pagaidu vārdu: puse faila kešā izskatītos pēc gatava
    tmp = out_dir / f"voice_{secrets.token_hex(6)}.part"
    tmp.write_bytes(resp.content)
    tmp.replace(cached)
    log.info("voice synthesized: %s (%d bytes, %s)", cached.name,
             len(resp.content), voice)
    return str(cached)


def reel_voice(recipe: dict | None, rules: dict | None = None,
               session=None) -> str:
    """Ierunas fails reela receptei ('' ja receptē teksta nav)."""
    return synthesize((recipe or {}).get("voice_script") or "",
                      rules=rules, session=session)
