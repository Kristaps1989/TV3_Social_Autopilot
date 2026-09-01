"""Configuration: environment variables + hot-reloaded YAML/markdown files.

YAML in rules/ and markdown in prompts/ are the editorial surface — they are
re-read on every access (cheap, small files) so Ģirts can edit them without
a deploy or restart.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
# Repo defaults (read-only templates); editable copies live under data/ so
# a persistent volume mounted at data/ keeps admin-UI edits across deploys.
DEFAULT_RULES_DIR = BASE_DIR / "rules"
DEFAULT_PROMPTS_DIR = BASE_DIR / "prompts"
RULES_DIR = Path(os.environ.get("RULES_DIR", BASE_DIR / "data" / "rules"))
PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", BASE_DIR / "data" / "prompts"))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///data/autopilot.db")
TIMEZONE = os.environ.get("TZ", "Europe/Riga")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL_FAST = os.environ.get("AI_MODEL_FAST", "claude-haiku-4-5-20251001")
AI_MODEL_STRONG = os.environ.get("AI_MODEL_STRONG", "claude-sonnet-5")

INGEST_INTERVAL_MINUTES = int(os.environ.get("INGEST_INTERVAL_MINUTES", "3"))

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


class ConfigError(Exception):
    pass


def ensure_editable_dirs() -> None:
    """When RULES_DIR/PROMPTS_DIR point at a persistent volume (outside the
    repo), seed them with the repo defaults on first start so editor changes
    made in the admin UI survive redeploys. Existing files are never touched."""
    import shutil

    for target, source in ((RULES_DIR, DEFAULT_RULES_DIR),
                           (PROMPTS_DIR, DEFAULT_PROMPTS_DIR)):
        if target.resolve() == source.resolve():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for f in source.glob("*"):
            if f.is_file() and not (target / f.name).exists():
                shutil.copy2(f, target / f.name)
    # Uzsēšana notiek vienu reizi, bet noteikumi kodā turpina rasties. Bez šī
    # katrs jauns noteikums uz strādājošas instances paliek neredzams, līdz
    # kāds to pārkopē ar roku — un tā pēc katra izlaiduma.
    try:
        sync_missing_rules()
    except Exception as e:  # noqa: BLE001 — konfigurācija nedrīkst neļaut startēt
        log.warning("rules.yaml papildināšana neizdevās: %s", e)


def _editable(name: str, editable_dir: Path, default_dir: Path) -> Path:
    """The editable copy when it exists, else the repo default."""
    candidate = editable_dir / name
    return candidate if candidate.exists() else default_dir / name


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a YAML mapping")
    return data


def load_rules() -> dict[str, Any]:
    return _load_yaml(_editable("rules.yaml", RULES_DIR, DEFAULT_RULES_DIR))


def missing_channels() -> list[str]:
    """Kanāli, kas ir repo noklusējumos, bet nav rediģējamajā kopijā.

    Rediģējamā kopija tiek uzsēta VIENU reizi un pēc tam vairs netiek
    aiztikta (`ensure_editable_dirs`) — tas pasargā redaktora labojumus, bet
    nozīmē arī, ka jauns kanāls vai formāts, kas parādās kodā, uz jau
    strādājošas instances neparādās nekad. Tas ir kluss, un kluss nozīmē
    nedēļas brīnīšanos, kāpēc kaut kas neiznāk.
    """
    editable = RULES_DIR / "channels.yaml"
    default = DEFAULT_RULES_DIR / "channels.yaml"
    if not editable.exists() or editable.resolve() == default.resolve():
        return []
    try:
        have = set(_load_yaml(editable))
        return sorted(set(_load_yaml(default)) - have)
    except ConfigError:
        return []


def _yaml_blocks(text: str) -> dict[str, str]:
    """Faila teksts sadalīts pa augšējā līmeņa atslēgām, KOPĀ ar komentāriem.

    Komentāri šajā failā redaktoram pasaka, ko katrs noteikums dara, tāpēc
    jaunu atslēgu bez tiem pievienot nozīmētu pievienot mīklu.
    """
    lines = text.splitlines()
    keys = [i for i, l in enumerate(lines) if re.match(r"^[a-z_]+:", l)]

    def comment_start(i: int) -> int:
        """Kur sākas komentāri, kas pieder ŠAI atslēgai.

        Ar atkāpi rakstīts komentārs («#  entertainment: 8») šajā failā ir
        iepriekšējās atslēgas izkomentēts piemērs, ne nākamās virsraksts.
        Bez šī izņēmuma piemērs aizceļo pie svešas atslēgas, un pievienotais
        bloks izskatās pēc kļūdas.
        """
        j = i
        while j > 0 and re.match(r"^#(?! {2,})", lines[j - 1]):
            j -= 1
        return j

    blocks: dict[str, str] = {}
    for n, i in enumerate(keys):
        start = comment_start(i)
        end = comment_start(keys[n + 1]) if n + 1 < len(keys) else len(lines)
        body = lines[start:end]
        while body and not body[-1].strip():
            body.pop()
        blocks[lines[i].split(":")[0]] = "\n".join(body)
    return blocks


def sync_missing_rules() -> list[str]:
    """Pieliek rediģējamajai kopijai noteikumus, kas ir kodā, bet ne tajā.

    Kopija tiek uzsēta VIENU reizi un pēc tam netiek aiztikta, lai nepazustu
    redaktora labojumi. Tas nozīmēja, ka katrs jauns noteikums uz strādājošas
    instances paliek neredzams, līdz kāds to pārkopē ar roku — un pēc katra
    izlaiduma tas bija jādara no jauna.

    Pielikt ir droši: esošās atslēgas neaiztiekam (tātad neviens labojums
    nepazūd), un jaunā atslēga nāk ar TO PAŠU vērtību, kas jau tāpat ir spēkā
    kā koda noklusējums. Uzvedība nemainās — mainās tikai tas, ka redaktors
    to beidzot redz un var mainīt.
    """
    editable = RULES_DIR / "rules.yaml"
    default = DEFAULT_RULES_DIR / "rules.yaml"
    if not editable.exists() or editable.resolve() == default.resolve():
        return []
    missing = missing_rules()
    if not missing:
        return []
    blocks = _yaml_blocks(default.read_text(encoding="utf-8"))
    added = [k for k in missing if k in blocks]
    if not added:
        return []
    text = editable.read_text(encoding="utf-8").rstrip("\n")
    text += "\n\n# --- Pievienots automātiski: jauni noteikumi no koda ---\n"
    text += "\n\n".join(blocks[k] for k in added) + "\n"
    editable.write_text(text, encoding="utf-8")
    log.info("rules.yaml papildināts ar %d jauniem noteikumiem: %s",
             len(added), ", ".join(added))
    return added


def missing_rules() -> list[str]:
    """Noteikumu atslēgas, kas ir repo noklusējumos, bet ne rediģējamajā kopijā.

    Tā pati klusā novirze, kas kanāliem: kopija tiek uzsēta vienu reizi, un
    jauns noteikums uz strādājošas instances redaktoram vairs neparādās.
    Koda noklusējums parasti darbojas arī bez atslēgas, bet redaktors par to
    nezina un tāpēc nevar to ne mainīt, ne izslēgt.
    """
    editable = RULES_DIR / "rules.yaml"
    default = DEFAULT_RULES_DIR / "rules.yaml"
    if not editable.exists() or editable.resolve() == default.resolve():
        return []
    try:
        have = set(_load_yaml(editable) or {})
        return sorted(set(_load_yaml(default) or {}) - have)
    except ConfigError:
        return []


def set_rule(key: str, value: str) -> None:
    """Nomaina VIENU noteikumu rediģējamajā rules.yaml, saglabājot pārējo.

    Rindas līmenī, nevis ielasot un izrakstot YAML: dump nogalinātu visus
    komentārus, un tieši tie šajā failā redaktoram pasaka, ko katrs noteikums
    dara. Ja atslēgas nav, pieliekam beigās.
    """
    path = RULES_DIR / "rules.yaml"
    if not path.exists():
        path = DEFAULT_RULES_DIR / "rules.yaml"
    text = path.read_text(encoding="utf-8")
    line = f'{key}: "{value}"'
    pattern = re.compile(rf"^{re.escape(key)}:[^\n]*$", re.M)
    text = (pattern.sub(line, text, count=1) if pattern.search(text)
            else text.rstrip("\n") + f"\n{line}\n")
    path.write_text(text, encoding="utf-8")


def load_channels() -> dict[str, Any]:
    """Channels with active: false are hidden everywhere (dashboard,
    scheduling, publishing) until the flag is flipped — used to ship
    channel configs ahead of their account connection."""
    channels = _load_yaml(_editable("channels.yaml", RULES_DIR, DEFAULT_RULES_DIR))
    out = {}
    for name, cfg in channels.items():
        if not isinstance(cfg, dict):
            # a mis-indented setting parses as a top-level key; skip it rather
            # than take down every page that loads the channel list
            log.warning("channels.yaml: «%s» nav kanāla bloks (%r) — izlaists",
                        name, cfg)
            continue
        if cfg.get("active", True):
            out[name] = cfg
    return out


def load_feeds() -> dict[str, Any]:
    return _load_yaml(_editable("feeds.yaml", RULES_DIR, DEFAULT_RULES_DIR))


# CMS sadaļas no raksta URL ceļa. feeds.yaml url_sections pārraksta šo;
# noklusējums nodrošina, ka sadaļu noteikšana un Statistikas filtrs strādā
# arī ar vecu (nepapildinātu) konfigurācijas kopiju uz servera diska.
DEFAULT_URL_SECTIONS = {"zinas": "news", "sports": "sport",
                        "izklaide": "entertainment",
                        "dzivesstils": "entertainment"}


def url_sections() -> dict:
    return load_feeds().get("url_sections") or DEFAULT_URL_SECTIONS


def load_prompt(name: str) -> str:
    path = _editable(f"{name}.md", PROMPTS_DIR, DEFAULT_PROMPTS_DIR)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def system_prompt_for(platform: str) -> str:
    """Base style guide + the platform-specific one."""
    per_platform = {
        "facebook_page": "system_facebook",
        "x": "system_x",
        "threads": "system_threads",
        "instagram": "system_instagram",
    }.get(platform, "")
    parts = [load_prompt("system_base")]
    if per_platform:
        parts.append(load_prompt(per_platform))
    return "\n\n".join(p for p in parts if p)


# Formāti, ko sistēma prot uzbūvēt un publicēt. Nepazīstams nosaukums
# `formats:` sarakstā klusi neko nedara — kanāls vienkārši to nekad
# neizvēlas, un redaktors nedēļu brīnās, kāpēc formāta nav.
KNOWN_FORMATS = {"link", "photo", "photo_album", "card_carousel", "reel",
                 "story", "text_only", "carousel", "video"}


def _channel_errors(data: dict) -> str | None:
    """Kanālu faila kļūdas, kas citādi paliktu klusas līdz pirmajam brīnumam."""
    from adapters import _REAL

    feeds = {k for k in (load_feeds() or {})
             if k not in ("term_sections", "url_sections")}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            return (f"«{name}» nav kanāla bloks, bet atsevišķa vērtība "
                    f"({cfg!r}) — visticamāk trūkst atkāpes: kanāla "
                    f"iestatījumiem jāsākas ar diviem tukšumiem")
        platform = cfg.get("platform")
        if platform and platform not in _REAL:
            return (f"«{name}»: nezināma platforma «{platform}». "
                    f"Pieejamās: {', '.join(sorted(_REAL))}")
        for fmt in cfg.get("formats") or []:
            if fmt not in KNOWN_FORMATS:
                return (f"«{name}»: nezināms formāts «{fmt}». "
                        f"Pieejamie: {', '.join(sorted(KNOWN_FORMATS))}")
        # `feeds:` kanāla blokā kods pagaidām nelasa (plūsmas tiek aptaujātas
        # visas, kas feeds.yaml ir aktīvas, un maršrutē `sections:`), bet
        # nepareizs ieraksts te ir brīdinājuma vērts: tieši šajā rindā mēdz
        # nokļūt formāta nosaukums, kura īstā vieta ir `formats:`.
        for feed in cfg.get("feeds") or []:
            if feed in KNOWN_FORMATS:
                return (f"«{name}»: «{feed}» ir formāts, nevis plūsma — "
                        f"tā vieta ir `formats:` rindā")
            if feeds and feed not in feeds:
                return (f"«{name}»: plūsma «{feed}» feeds.yaml neeksistē. "
                        f"Pieejamās: {', '.join(sorted(feeds))}")
    return None


# Noteikumi, kuru vērtība ir kartējums (sadaļa -> vērtība). Tukšs drīkst
# būt; atsevišķa vērtība nedrīkst — tā nozīmē, ka atkāpe ir pazudusi.
_MAPPING_RULES = ("reel_voice_by_section", "reel_voice_rate_by_section",
                  "term_blocklist", "term_allowlist")


def _rules_errors(data: dict) -> str | None:
    """Noteikumu kļūdas, kas citādi paliek klusas.

    Šeit nav gaumes jautājumu: katrs pārbaudītais gadījums ir tāds, kur
    fails ir derīgs YAML, kods to pieņem un vienkārši nedara neko — balss
    nemainās, temps nemainās, un iemesls no ekrāna nav redzams.
    """
    from app import tts

    for key in _MAPPING_RULES:
        value = data.get(key)
        if value is None or isinstance(value, dict):
            continue
        return (f"«{key}» jābūt sarakstam pa sadaļām vai tukšam, bet ir "
                f"{value!r} — visticamāk trūkst atkāpes: sadaļas rindai "
                f"jāsākas ar diviem tukšumiem nākamajā rindā")

    provider = str(data.get("tts_provider") or "").strip().lower()
    if provider and provider not in tts.SUPPORTED_PROVIDERS:
        return (f"nezināms tts_provider «{provider}» — lentes iznāks klusas. "
                f"Pieejamie: {', '.join(sorted(tts.SUPPORTED_PROVIDERS))}")

    rates = {"reel_voice_rate": data.get("reel_voice_rate")}
    rates.update({f"reel_voice_rate_by_section: {k}": v
                  for k, v in (data.get("reel_voice_rate_by_section")
                               or {}).items()})
    for where, value in rates.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return (f"«{where}» jābūt veselam skaitlim procentos "
                    f"(piem. -4 vai 12), bet ir {value!r}")
        if not -40 <= value <= 40:
            return (f"«{where}»: {value}% ir ārpus diapazona -40..40 — "
                    f"pakalpojums to tik un tā apgrieztu")
    return None


def validate_editable(kind: str, text: str) -> str | None:
    """Validate an edited config before saving. Returns error message or None."""
    if kind in ("rules", "channels", "feeds"):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return f"Invalid YAML: {e}"
        if not isinstance(data, dict):
            return "File must be a YAML mapping (key: value)"
        if kind == "channels":
            return _channel_errors(data)
        if kind == "rules":
            return _rules_errors(data)
    return None
