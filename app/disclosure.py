"""ES MI akta (Regula (ES) 2024/1689) 50. panta atruna.

50. panta 2. punkts prasa, lai mākslīgi ģenerēts audio, attēls, video un
teksts būtu marķēts kā mākslīgi radīts; 4. punkts prasa, lai izplatītājs to
atklāj arī tad, kad saturs ir par sabiedrībai nozīmīgiem jautājumiem.
Ziņu saturs ir tieši tas.

Mēs esam izplatītājs: MI uzraksta parakstu, sadaļu tekstus uz grafikām, un
lentes ieruna ir sintezēta balss. Tāpēc atruna te ir REDZAMA — uzdrukāta uz
katras grafikas un ierakstīta katrā parakstā — nevis paslēpta metadatos.
Regula prasa, lai marķējums būtu skaidrs un pamanāms; kaut kas, ko redz
tikai izstrādātājs, tāds nav.

Formulējumu var mainīt Noteikumos, bet ne izslēgt netīšām: `ai_disclosure`
noklusējums ir True, un tā izslēgšana ir apzināts redakcijas lēmums.
"""
from __future__ import annotations

import html

DEFAULT_TEXT = "Saturs sagatavots ar mākslīgā intelekta palīdzību"
DEFAULT_SHORT = "Veidots ar MI"
# Izrunātā atruna pēc noklusējuma ir IZSLĒGTA. Regula prasa skaidru un
# pamanāmu marķējumu — to dod zīmīte uz katra kadra, pilns teikums noslēguma
# kadrā un pēdējā rinda parakstā (parakstu ekrānlasītājs nolasa). Balsī tā
# nāca kā liekais teikums aiz aicinājuma un stāsta beigas padarīja gurdenas.
# Kam vajag, ieraksta `ai_disclosure_spoken` Noteikumos.
DEFAULT_SPOKEN = ""


def _rules(rules: dict | None) -> dict:
    if rules is None:
        from app import config

        rules = config.load_rules()
    return rules or {}


DEFAULT_SCOPE = "voiced_reels"


def enabled(rules: dict | None = None) -> bool:
    return bool(_rules(rules).get("ai_disclosure", True))


def scope(rules: dict | None = None) -> str:
    return str(_rules(rules).get("ai_disclosure_scope") or DEFAULT_SCOPE).strip()


def applies(fmt: str = "", voiced: bool = False,
            rules: dict | None = None) -> bool:
    """Vai ŠIM ierakstam liekams MI marķējums.

    Noklusējums ir `voiced_reels`: tikai lentes ar sintezēto balsi. Iemesls
    ir precizitāte, ne slinkums — rakstu ir uzrakstījis žurnālists, un
    atruna zem katra ieraksta lasās kā apgalvojums, ka MI ir uzrakstījis
    RAKSTU. Balss ir vienīgais, kas tiešām ir mākslīgi ģenerēts medijs
    50. panta 2. punkta izpratnē; parakstu un kartīšu tekstus MI palīdz
    formulēt no žurnālista raksta, un tos redakcija apstiprina.

    `all` atgriež marķējumu uz visiem formātiem — tā tas bija sākotnēji, un
    tā ir plašākā interpretācija. Izvēle ir redakcijas, ne tehniska.
    """
    if not enabled(rules):
        return False
    if scope(rules) == "all":
        return True
    return fmt == "reel" and bool(voiced)


def text(rules: dict | None = None) -> str:
    r = _rules(rules)
    if not r.get("ai_disclosure", True):
        return ""
    return str(r.get("ai_disclosure_text") or DEFAULT_TEXT).strip()


def short(rules: dict | None = None) -> str:
    r = _rules(rules)
    if not r.get("ai_disclosure", True):
        return ""
    return str(r.get("ai_disclosure_short") or DEFAULT_SHORT).strip()


def spoken(rules: dict | None = None) -> str:
    """Ko pateikt skaļi ('' = neko; sk. DEFAULT_SPOKEN)."""
    r = _rules(rules)
    if not r.get("ai_disclosure", True):
        return ""
    return str(r.get("ai_disclosure_spoken") or DEFAULT_SPOKEN).strip()


def caption_line(platform: str = "", rules: dict | None = None) -> str:
    """Atruna parakstam. X ir 280 zīmes, tāpēc tur īsā forma."""
    long_form, short_form = text(rules), short(rules)
    if not long_form:
        return ""
    return short_form if platform == "x" and short_form else long_form


def in_caption(caption: str, rules: dict | None = None) -> bool:
    """Vai atruna parakstā jau ir — lai to nepieliktu divreiz.

    Salīdzinām pēc burtiem bez reģistra un atstarpēm: AI paraksts pats mēdz
    ieminēties par mākslīgo intelektu, un divas atrunas pēc kārtas izskatās
    pēc kļūdas.
    """
    hay = " ".join((caption or "").lower().split())
    for needle in (text(rules), short(rules)):
        if needle and " ".join(needle.lower().split()) in hay:
            return True
    return False


CSS = """
.aibadge { position:absolute; left:%(left)dpx; bottom:%(bottom)dpx;
  display:flex; align-items:center; gap:12px;
  background:rgba(9,7,12,.62); color:rgba(255,255,255,.94);
  border:2px solid rgba(255,255,255,.30); border-radius:99px;
  padding:10px 24px 10px 18px; font-size:%(size)dpx; font-weight:bold;
  letter-spacing:.02em; }
.aibadge b { background:#fff; color:#0d0a12; border-radius:8px;
  padding:2px 9px; font-size:%(size)dpx; line-height:1.25; }
"""


def badge_css(left: int = 56, bottom: int = 168, size: int = 30) -> str:
    return CSS % {"left": left, "bottom": bottom, "size": size}


def badge_html(rules: dict | None = None) -> str:
    """Redzamais marķējums uz grafikas ('' ja atruna izslēgta)."""
    label = short(rules)
    if not label:
        return ""
    return f'<div class="aibadge"><b>MI</b>{html.escape(label)}</div>'
