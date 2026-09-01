"""Balss sintēze reelu ierunai: teksts -> mp3.

Reela ierunas teksts (`voice_script`) top lēmumu solī un glabājas receptē;
šis modulis to pārvērš skaņā.

Pakalpojumu izvēlas noteikumos (`tts_provider`). Ieviesti divi:
**Azure Speech** (divas latviešu neironu balsis, SSML ar ziņu tempu un
pauzēm) un **ElevenLabs** (balsis pēc ID no balsu bibliotēkas; latviski runā
tikai v3 modelis, tāpēc tas ir noklusējums). Atslēgas glabājas Konti sadaļā,
katram pakalpojumam sava; izrunas vārdnīca un skaitļi vārdos strādā abos
vienādi, jo pārraksta tekstu, ne marķējumu.

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

from app import config, credentials, lvnum

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "azure"

# Latviešu balsis pa pakalpojumiem. Atslēgas ("female"/"male") ir mūsu, lai
# noteikumos nebūtu jāraksta pakalpojuma iekšējie nosaukumi.
VOICES = {
    "azure": {"female": "lv-LV-EveritaNeural", "male": "lv-LV-NilsNeural"},
    # ElevenLabs balsis ir ID, ne vārdi. Noklusējumā divas plaši zināmās
    # priekšdefinētās (Rachel / Adam) — daudzvalodu modelī tās runā arī
    # latviski. Labāku latviešu balsi izvēlas balsu bibliotēkā un tās ID
    # ieraksta Noteikumos: reel_voice_name: <voice_id>.
    "elevenlabs": {"female": "21m00Tcm4TlvDq8ikWAM",
                   "male": "pNInz6obpgDQGcFmaJgB"},
}
DEFAULT_VOICE = VOICES["azure"]["female"]
# Vienīgais ElevenLabs modelis, kura valodu sarakstā ir latviešu, ir v3;
# multilingual_v2 latviski nerunā. Maināms Noteikumos (elevenlabs_model).
ELEVENLABS_MODEL = "eleven_v3"
# Ziņu ierunai neitrāls temps; nedaudz lēnāk par noklusējumu, jo lentē
# skatītājs vienlaikus lasa arī kadra tekstu.
# Ziņu ierunai neitrāls temps; nedaudz lēnāk par pakalpojuma noklusējumu, jo
# lentē skatītājs vienlaikus lasa arī kadra tekstu. Procentos, lai to varētu
# pateikt abiem pakalpojumiem: Azure gaida "+6%", ElevenLabs — reizinātāju.
DEFAULT_RATE_PERCENT = -4
DEFAULT_RATE = f"{DEFAULT_RATE_PERCENT}%"
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


# pakalpojums -> atslēgas vārds krātuvē
_KEY_NAMES = {"azure": "azure_speech_key", "elevenlabs": "elevenlabs_api_key"}


def _key(session=None, rules: dict | None = None) -> str:
    name = _KEY_NAMES.get(provider(rules), "azure_speech_key")
    return credentials.get(name, session)


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
    return bool(_key(session, rules))


def voice_name(rules: dict | None = None, section: str = "") -> str:
    """Balss izvēlētajam pakalpojumam un sadaļai.

    Noteikumos raksta "female"/"male"; pilns pakalpojuma balss nosaukums vai
    ID arī iet cauri, lai varētu izmēģināt balsi, kas šeit vēl nav sarakstā.

    `reel_voice_by_section` ļauj sadaļai savu balsi: izklaides ziņas panes
    dzīvāku balsi nekā tā, ar kuru stāsta par pierobežu, un tā ir redakcijas
    izvēle, ne tehnisks jautājums.
    """
    rules = config.load_rules() if rules is None else rules
    per_section = (rules or {}).get("reel_voice_by_section") or {}
    choice = str(per_section.get(section)
                 or (rules or {}).get("reel_voice_name") or "").strip()
    catalogue = VOICES.get(provider(rules), {})
    fallback = catalogue.get("female", DEFAULT_VOICE)
    return catalogue.get(choice.lower(), choice or fallback)


def speech_rate(rules: dict | None = None, section: str = "") -> int:
    """Runas temps procentos pret pakalpojuma noklusējumu (0 = neaiztikts).

    Ziņu ierunai noklusējums ir nedaudz lēnāks (-4%), jo lentē skatītājs
    vienlaikus lasa arī kadra tekstu. Izklaidei der ātrāk, un tas ir tas
    pats sadaļas jautājums, kas balss.
    """
    rules = config.load_rules() if rules is None else rules
    per_section = (rules or {}).get("reel_voice_rate_by_section") or {}
    value = per_section.get(section, (rules or {}).get("reel_voice_rate"))
    if value is None:
        value = DEFAULT_RATE_PERCENT
    try:
        return max(-40, min(40, int(value)))
    except (TypeError, ValueError):
        log.warning("nederīgs runas temps %r — lietoju noklusējumu", value)
        return DEFAULT_RATE_PERCENT


def voice_choice(rules: dict | None = None, section: str = "") -> dict:
    """Kura balss un kurš temps sadaļai TIEŠĀM tiks lietots.

    Balsi un tempu izšķir divi noteikumi (globālais un sadaļas), un no
    Noteikumu faila to nevar salasīt: `reel_voice_by_section` piemērs tur
    ir komentārs, un izkomentēta rinda izskatās gluži kā iestatījums. Šis
    pasaka rezultātu, un priekšskatījums to parāda pie ierunas — citādi
    redaktors maina rindu, kas neko nedara, un secina, ka nestrādā rīks.
    """
    rules = config.load_rules() if rules is None else rules
    by_voice = (rules or {}).get("reel_voice_by_section") or {}
    by_rate = (rules or {}).get("reel_voice_rate_by_section") or {}
    return {"provider": provider(rules),
            "voice": voice_name(rules, section),
            "rate": speech_rate(rules, section),
            # vai tieši ŠAI sadaļai ir sava rinda, vai tā lieto kopīgo
            "voice_by_section": bool(section and by_voice.get(section)),
            "rate_by_section": bool(section
                                    and by_rate.get(section) is not None)}


def spoken_text(text: str, rules: dict | None = None) -> str:
    """Teksts tā, kā tas JĀIZRUNĀ (izrunas vārdnīca pielietota).

    Divi soļi. Pirmais — kārtas skaitļi vārdos pareizā locījumā: balss
    «59. minūtē» citādi nolasa kā «piecdesmit devītā minūtē» (sk. lvnum).
    Otrais — izrunas vārdnīca; aizstājam garākos ierakstus vispirms, lai
    «tv3.lv» netiktu sadalīts pa «tv3».

    Rakstiskais scenārijs paliek neskarts — priekšskatījumā redaktors grib
    redzēt «lasi tv3.lv» un «59. minūtē», nevis fonētisko pierakstu.
    """
    text = lvnum.speak_ordinals(text or "")
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
                 rules: dict | None = None, rate: int = 0) -> bytes:
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
            content=build_ssml(text, voice, rate=f"{rate:+d}%",
                               rules=rules).encode("utf-8"))
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


def _voice_row(v: dict) -> dict:
    """Viena balss no /v2/voices tā, kā to vajag redaktoram.

    ElevenLabs balsis NAV piesaistītas valodai — valodu nosaka modelis, un
    balss nes tembru un akcentu. Tāpēc izvēles ass te nav «vai prot latviski»,
    bet «kā tā skan latviski», un uz to atbild tikai klausīšanās. `preview`
    ir pašas ElevenLabs parauga ieraksts; bez tā saraksts ir vārdi bez skaņas.
    """
    labels = v.get("labels") or {}
    return {
        "id": v.get("voice_id", ""),
        "name": v.get("name", ""),
        "category": v.get("category", ""),
        "gender": str(labels.get("gender") or "").lower(),
        "accent": str(labels.get("accent") or ""),
        "description": str(labels.get("description") or ""),
        "preview": v.get("preview_url") or "",
    }


def elevenlabs_catalogue(session=None, rules: dict | None = None) -> dict:
    """Ko konts TIEŠĀM drīkst lietot: balsis un modeļi ({} pie kļūmes).

    Balss ID iekodēt ir minēšana: ElevenLabs bezmaksas plānā bibliotēkas
    balsis caur API ir liegtas (402 paid_plan_required), un tas, kura balss
    ir «premade» un kura «library», katram kontam atšķiras. Tāpat ar
    valodām — kurš modelis runā latviski, pasaka pats modeļu saraksts, ne
    dokumentācija. Tāpēc jautājam kontam un rādām atbildi redaktoram.
    """
    import httpx

    key = _key(session, rules or {"tts_provider": "elevenlabs"})
    if not key:
        return {}
    headers = {"xi-api-key": key, "User-Agent": "TV3-Social-Autopilot/1.0"}
    out: dict = {"voices": [], "models": []}
    try:
        r = httpx.get("https://api.elevenlabs.io/v2/voices?page_size=100",
                      headers=headers, timeout=TIMEOUT)
        if r.status_code == 200:
            out["voices"] = [_voice_row(v)
                              for v in (r.json().get("voices") or [])
                              if v.get("voice_id")]
        r = httpx.get("https://api.elevenlabs.io/v1/models",
                      headers=headers, timeout=TIMEOUT)
        if r.status_code == 200:
            for m in r.json() or []:
                langs = [str(l.get("language_id") or l.get("name") or "").lower()
                         for l in (m.get("languages") or [])]
                out["models"].append({
                    "id": m.get("model_id", ""),
                    "name": m.get("name", ""),
                    # vai modelis prot latviski — atbild pats saraksts
                    "latvian": any(l.startswith("lv") or "latvian" in l
                                   for l in langs)})
    except Exception as e:  # noqa: BLE001 — saraksts ir palīgs, ne ceļš
        log.warning("ElevenLabs catalogue failed: %s", e)
    return out


def _elevenlabs_audio(text: str, voice: str, session=None,
                      errors: list | None = None,
                      rules: dict | None = None, rate: int = 0) -> bytes:
    """ElevenLabs TTS atbilde (b"" pie jebkuras kļūdas).

    SSML te nav: modelis pats liek pauzes pēc pieturzīmēm, tāpēc pietiek ar
    `spoken_text` — izrunas vārdnīca un skaitļi vārdos (lvnum) strādā tieši
    tāpat kā Azure ceļā, jo tie pārraksta tekstu, ne marķējumu.
    """
    import httpx

    rules = config.load_rules() if rules is None else rules
    model = str((rules or {}).get("elevenlabs_model")
                or ELEVENLABS_MODEL).strip()
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
           "?output_format=mp3_44100_128")
    payload = {"text": spoken_text(text, rules), "model_id": model}
    if rate:
        # ElevenLabs temps ir reizinātājs (1.0 = normāli). Sūtām TIKAI tad,
        # kad temps tiešām mainīts: vecāki modeļi šo lauku nepieņem, un
        # nederīgs lauks nozīmētu klusu lenti visiem, ne tikai ātrākiem.
        payload["voice_settings"] = {
            "speed": round(max(0.7, min(1.2, 1 + rate / 100)), 2)}
    try:
        resp = httpx.post(
            url, timeout=TIMEOUT,
            headers={"xi-api-key": _key(session, rules),
                     "User-Agent": "TV3-Social-Autopilot/1.0"},
            json=payload)
        if resp.status_code != 200:
            log.warning("ElevenLabs TTS failed: HTTP %s %s", resp.status_code,
                        resp.text[:200])
            if errors is not None:
                detail = (resp.text or "").strip()[:160] or "bez ziņojuma"
                if resp.status_code == 402:
                    # visbiežākā klupšana: bezmaksas plānā bibliotēkas balsis
                    # caur API ir liegtas, un tas nav atslēgas vai koda jautājums
                    detail += (" — šī balss kontam caur API nav pieejama. "
                               "Zemāk ir konta balsu saraksts; ieraksti "
                               "Noteikumos (reel_voice_name) kādas no tām ID.")
                errors.append(f"HTTP {resp.status_code}: {detail}")
            return b""
        if not resp.content and errors is not None:
            errors.append("ElevenLabs atbildēja bez audio")
        return resp.content or b""
    except Exception as e:  # noqa: BLE001 — kluss reels ir labāks par nekādu
        log.warning("ElevenLabs TTS request failed: %s", e)
        if errors is not None:
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
        return b""


# pakalpojums -> funkcija, kas atgriež audio baitus. Jauna pakalpojuma
# pievienošana ir viens ieraksts šeit: kešs, teksta sagatavošana un kļūdu
# apstrāde ir kopīga.
_SYNTHS = {"azure": _azure_audio, "elevenlabs": _elevenlabs_audio}
# ko drīkst rakstīt `tts_provider` rindā (Noteikumu pārbaudei — nepazīstams
# nosaukums nozīmē klusas lentes, un tas jāpasaka saglabājot, ne pēc nedēļas)
SUPPORTED_PROVIDERS = frozenset(_SYNTHS)


def _cache_path(text: str, voice: str, out_dir: Path) -> Path:
    """Viens un tas pats teksts ar to pašu balsi = tas pats fails.

    Pārzīmējot reelu, teksts parasti nemainās; bez keša katrs mēģinājums
    būtu jauns Azure pieprasījums par to pašu skaņu.
    """
    digest = hashlib.sha256(f"{voice}\n{text}".encode()).hexdigest()[:16]
    return out_dir / f"voice_{digest}.mp3"


def synthesize(text: str, out_dir: Path | str | None = None,
               rules: dict | None = None, session=None,
               force: bool = False, errors: list | None = None,
               section: str = "") -> str:
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
    voice = voice_name(rules, section)
    rate = speech_rate(rules, section)
    # kešojam pēc tā, kas tiks IZRUNĀTS: pielabojot izrunas vārdnīcu vai
    # tempu, vecais ieraksts kļūst nederīgs, un pēc šīs atslēgas tas atkrīt
    # pats. Temps atslēgā ir obligāts — citādi ātrāka izklaides ieruna
    # atbildētu ar vecāko, lēnāko failu.
    cached = _cache_path(spoken_text(text, rules), f"{voice}@{rate:+d}", out_dir)
    if not force and cached.exists() and cached.stat().st_size > 0:
        return str(cached)

    audio = _SYNTHS[provider(rules)](text, voice, session, errors, rules, rate)
    if not audio:
        return ""

    # rakstām caur pagaidu vārdu: puse faila kešā izskatītos pēc gatava
    tmp = out_dir / f"voice_{secrets.token_hex(6)}.part"
    tmp.write_bytes(audio)
    tmp.replace(cached)
    log.info("voice synthesized: %s (%d bytes, %s, temps %+d%%%s)",
             cached.name, len(audio), voice, rate,
             f", sadaļa {section}" if section else "")
    return str(cached)


def reel_voice(recipe: dict | None, rules: dict | None = None,
               session=None) -> str:
    """Ierunas fails reela receptei ('' ja receptē teksta nav)."""
    return synthesize((recipe or {}).get("voice_script") or "",
                      rules=rules, session=session)
