"""Balss sintēze reelu ierunai: teksts -> mp3.

Reela ierunas teksts (`voice_script`) top lēmumu solī un glabājas receptē;
šis modulis to pārvērš skaņā.

Pakalpojumu izvēlas noteikumos (`tts_provider`). Šobrīd ieviests ir
**Azure Speech**: divas latviešu neironu balsis un SSML, ar ko var pateikt,
kā runāt — ziņu tempu un pauzes starp teikumiem.

Otrs nopietnais kandidāts ir **Tilde** (tilde.ai): latviešu balsis, kas
taisītas latviešu valodai, un — kas mums svarīgākais — pielāgojamas izrunas
vārdnīcas. Tieši tur Azure klūp: īpašvārdi lokatīvā ("Bauskas ielā") un
saīsinājumi. Tilde nav ieviesta, jo cena un API līgums nāk caur sarunu ar
pārdošanu, nevis no publiskas dokumentācijas. Kad tas ir, jauns pakalpojums
ir viena funkcija, kas atgriež audio baitus, un ieraksts `_SYNTHS` sarakstā —
kešs, SSML sagatavošana un kļūdu apstrāde ir kopīga.

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

DEFAULT_PROVIDER = "azure"

# Latviešu balsis pa pakalpojumiem. Atslēgas ("female"/"male") ir mūsu, lai
# noteikumos nebūtu jāraksta pakalpojuma iekšējie nosaukumi.
VOICES = {
    "azure": {"female": "lv-LV-EveritaNeural", "male": "lv-LV-NilsNeural"},
}
DEFAULT_VOICE = VOICES["azure"]["female"]
# Ziņu ierunai neitrāls temps; nedaudz lēnāk par noklusējumu, jo lentē
# skatītājs vienlaikus lasa arī kadra tekstu.
DEFAULT_RATE = "-4%"
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
TIMEOUT = 30

# Izrunas vārdnīca: ko balss nolasa nepareizi.
#
# Latviski punkts aiz cipara nozīmē kārtas skaitli, tāpēc Azure normalizētājs
# «tv3.lv» nolasa kā «tv TREŠAIS punkts lv». Domēnā tas ir tikai punkts. Šo
# nevar salabot ne ar promptu, ne ar balss izvēli — teksts modelim jāpasniedz
# tā, kā tas jāizrunā. Papildināt var Noteikumos (`tts_pronunciation`), bez
# deploy.
PRONUNCIATION = {
    "tv3.lv": "tv trīs punkts lv",
    "tv3 play": "tv trīs pleij",
    "tv3": "tv trīs",
}


def _key(session=None) -> str:
    return credentials.get("azure_speech_key", session)


# Hosti, kuru PIRMĀ etiķete ir reģions. Uzmanību: pie
# <resurss>.cognitiveservices.azure.com un <resurss>.services.ai.azure.com
# pirmā etiķete ir RESURSA nosaukums, nevis reģions — no tiem reģionu
# nolasīt nevar.
_REGION_HOSTS = ("tts.speech.microsoft.com", "stt.speech.microsoft.com",
                 "api.cognitive.microsoft.com")


def normalize_region(value: str) -> str:
    """Reģions no tā, ko cilvēks ielīmē ('' ja nolasīt nevar).

    Foundry lapa rāda galapunktus, nevis reģionu, tāpēc ielīmētu adresi
    pieņemam un reģionu paņemam no tās, kad tas tur tiešām ir. Klusi
    atkāpties uz noklusējumu būtu sliktāk nekā pateikt, ka nesanāca:
    atslēgas ir piesaistītas reģionam, un nepareizs reģions nozīmē 401.
    """
    value = (value or "").strip().strip("/")
    if not value:
        return ""
    if "." in value or "://" in value:
        from urllib.parse import urlparse

        host = urlparse(value if "://" in value else f"https://{value}").netloc
        host = (host or value).split(":")[0].lower()
        label, _, rest = host.partition(".")
        return label if rest in _REGION_HOSTS else ""
    return value.lower()


def _region(session=None) -> str:
    stored = credentials.get("azure_speech_region", session)
    return normalize_region(stored) or "westeurope"


def provider(rules: dict | None = None) -> str:
    rules = config.load_rules() if rules is None else rules
    name = str((rules or {}).get("tts_provider") or "").strip().lower()
    return name or DEFAULT_PROVIDER


def enabled(rules: dict | None = None, session=None) -> bool:
    """Vai ierunu vispār drīkst un var uztaisīt."""
    rules = config.load_rules() if rules is None else rules
    if not (rules or {}).get("reel_voice", True):
        return False
    if provider(rules) not in _SYNTHS:
        return False
    return bool(_key(session))


def voice_name(rules: dict | None = None) -> str:
    """Balss nosaukums izvēlētajam pakalpojumam.

    Noteikumos raksta "female"/"male"; pilns pakalpojuma nosaukums arī iet
    cauri, lai varētu izmēģināt balsi, kas šeit vēl nav sarakstā.
    """
    rules = config.load_rules() if rules is None else rules
    choice = str((rules or {}).get("reel_voice_name") or "").strip()
    catalogue = VOICES.get(provider(rules), {})
    return catalogue.get(choice.lower(), choice or DEFAULT_VOICE)


def spoken_text(text: str, rules: dict | None = None) -> str:
    """Teksts tā, kā tas JĀIZRUNĀ (izrunas vārdnīca pielietota).

    Aizstājam garākos ierakstus vispirms, lai «tv3.lv» netiktu sadalīts pa
    «tv3». Rakstiskais scenārijs paliek neskarts — priekšskatījumā redaktors
    grib redzēt «lasi tv3.lv», nevis fonētisko pierakstu.
    """
    table = {k.lower(): v for k, v in PRONUNCIATION.items()}
    extra = (rules or {}).get("tts_pronunciation") or {}
    if isinstance(extra, dict):
        table.update({str(k).lower(): str(v) for k, v in extra.items()})
    for src in sorted(table, key=len, reverse=True):
        if not src:
            continue
        text = re.sub(re.escape(src), table[src], text, flags=re.IGNORECASE)
    return text


def build_ssml(text: str, voice: str = DEFAULT_VOICE,
               rate: str = DEFAULT_RATE, rules: dict | None = None) -> str:
    """SSML dokuments vienam ierunas tekstam.

    Teikumu robežas kļūst par īsām pauzēm: bez tām neironu balss ziņu
    tekstu izstāsta vienā elpas vilcienā, un klausītājam nesanāk saprast,
    kur beidzas viens fakts un sākas nākamais.
    """
    text = spoken_text(text, rules)
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


def _azure_audio(text: str, voice: str, session=None,
                 errors: list | None = None,
                 rules: dict | None = None) -> bytes:
    """Azure Speech REST atbilde (b"" pie jebkuras kļūdas).

    errors: saraksts, kurā ielikt neizdošanās iemeslu. Azure atbilde pasaka,
    KAS nav kārtībā (401 nepareiza atslēga, 403 reģions, 400 balss), un
    lietotājam to ir vērts parādīt — citādi paliek "neizdevās".
    """
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
            content=build_ssml(text, voice, rules=rules).encode("utf-8"))
        if resp.status_code != 200:
            log.warning("TTS failed: HTTP %s %s", resp.status_code,
                        resp.text[:200])
            if errors is not None:
                errors.append(f"HTTP {resp.status_code}: "
                              f"{(resp.text or '').strip()[:160] or 'bez ziņojuma'}")
            return b""
        if not resp.content and errors is not None:
            errors.append("Azure atbildēja bez audio")
        return resp.content or b""
    except Exception as e:  # noqa: BLE001 — kluss reels ir labāks par nekādu
        log.warning("TTS request failed: %s", e)
        if errors is not None:
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
        return b""


# pakalpojums -> funkcija, kas atgriež audio baitus. Jauna pakalpojuma
# pievienošana ir viens ieraksts šeit: kešs, SSML un kļūdu apstrāde ir kopīga.
_SYNTHS = {"azure": _azure_audio}


def _cache_path(text: str, voice: str, out_dir: Path) -> Path:
    """Viens un tas pats teksts ar to pašu balsi = tas pats fails.

    Pārzīmējot reelu, teksts parasti nemainās; bez keša katrs mēģinājums
    būtu jauns Azure pieprasījums par to pašu skaņu.
    """
    digest = hashlib.sha256(f"{voice}\n{text}".encode()).hexdigest()[:16]
    return out_dir / f"voice_{digest}.mp3"


def synthesize(text: str, out_dir: Path | str | None = None,
               rules: dict | None = None, session=None,
               force: bool = False, errors: list | None = None) -> str:
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
    # kešojam pēc tā, kas tiks IZRUNĀTS: pielabojot izrunas vārdnīcu, vecais
    # ieraksts kļūst nederīgs, un pēc šī atslēgas tas atkrīt pats
    cached = _cache_path(spoken_text(text, rules), voice, out_dir)
    if not force and cached.exists() and cached.stat().st_size > 0:
        return str(cached)

    audio = _SYNTHS[provider(rules)](text, voice, session, errors, rules)
    if not audio:
        return ""

    # rakstām caur pagaidu vārdu: puse faila kešā izskatītos pēc gatava
    tmp = out_dir / f"voice_{secrets.token_hex(6)}.part"
    tmp.write_bytes(audio)
    tmp.replace(cached)
    log.info("voice synthesized: %s (%d bytes, %s)", cached.name,
             len(audio), voice)
    return str(cached)


def reel_voice(recipe: dict | None, rules: dict | None = None,
               session=None) -> str:
    """Ierunas fails reela receptei ('' ja receptē teksta nav)."""
    return synthesize((recipe or {}).get("voice_script") or "",
                      rules=rules, session=session)
