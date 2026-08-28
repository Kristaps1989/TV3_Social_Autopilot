"""Configuration: environment variables + hot-reloaded YAML/markdown files.

YAML in rules/ and markdown in prompts/ are the editorial surface — they are
re-read on every access (cheap, small files) so Ģirts can edit them without
a deploy or restart.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

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


def load_channels() -> dict[str, Any]:
    """Channels with active: false are hidden everywhere (dashboard,
    scheduling, publishing) until the flag is flipped — used to ship
    channel configs ahead of their account connection."""
    channels = _load_yaml(_editable("channels.yaml", RULES_DIR, DEFAULT_RULES_DIR))
    return {name: cfg for name, cfg in channels.items()
            if (cfg or {}).get("active", True)}


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


def validate_editable(kind: str, text: str) -> str | None:
    """Validate an edited config before saving. Returns error message or None."""
    if kind in ("rules", "channels", "feeds"):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return f"Invalid YAML: {e}"
        if not isinstance(data, dict):
            return "File must be a YAML mapping (key: value)"
    return None
