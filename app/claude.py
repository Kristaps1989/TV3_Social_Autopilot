"""Kopīgie Claude izsaukuma parametri — vienā vietā, lai visi izsaukumi
(lēmums, reklāmas teksti, nedēļas nogales plāns, kopsavilkums) runā ar API
vienādi un modeļa maiņa env mainīgajā nesalauž nevienu no tiem.

Divas lietas, kas te dzīvo:

* `effort` — jaunajiem modeļiem (Sonnet 5, Opus 4.6+, Opus 5, Fable)
  domāšana ir ieslēgta pēc noklusējuma, un šis regulators nosaka, cik dziļi
  tie domā. Rutīnas ziņai pietiek ar `medium`; īsam reklāmas tekstam ar
  `low`. Vecākie modeļi (Haiku 4.5, Sonnet 4.5) parametru noraida ar 400,
  tāpēc tiem to nesūtām.
* sistēmas prompta kešs — sistēmas instrukcija ir stabila un gara, un
  ingest iet ik pēc dažām minūtēm, tāpēc kešs paliek silts: atkārtotā
  ievade maksā ~10 % no pilnās cenas.
"""
from __future__ import annotations

# modeļu prefiksi, kas pieņem output_config.effort (un domā pēc noklusējuma)
EFFORT_MODELS = ("claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4",
                 "claude-opus-5", "claude-fable", "claude-mythos")


def supports_effort(model: str) -> bool:
    return str(model or "").startswith(EFFORT_MODELS)


def thinks(model: str) -> bool:
    """Vai modelis pirms atbildes domā (un domāšana skaitās max_tokens)."""
    return supports_effort(model)


def params(model: str, effort: str = "medium") -> dict:
    """Papildu argumenti `messages.create` — tukši vecajiem modeļiem."""
    if not supports_effort(model):
        return {}
    return {"output_config": {"effort": effort}}


def cached_system(text: str) -> list[dict]:
    """Sistēmas prompts kā kešojams bloks. Kešs ir prefiksa sakritība: viss,
    kas mainās no raksta uz rakstu, jāliek lietotāja ziņā, ne šeit."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def max_tokens_for(model: str, output: int) -> int:
    """Izvades griesti. Domājošam modelim domāšana skaitās tajā pašā limitā,
    tāpēc griestiem jābūt ar rezervi — nogriezta atbilde ir nederīgs lēmums
    un otrs izsaukums par pilnu cenu. Griesti nemaksā, ja tos nesasniedz."""
    return max(output * 3, 2000) if thinks(model) else output
