"""Skaitļi vārdos ierunai — latviešu kārtas skaitļa vārdi pareizā locījumā.

Balss lasa ciparus tā, kā tos redz: «59. minūtē» tai ir «piecdesmit devītā
minūtē», jo punkts aiz cipara nozīmē kārtas skaitli nominatīvā. Latviski tur
vajag lokatīvu — «piecdesmit devītajā minūtē». Neironu balss to pati
neizdomās: locījumu nosaka nākamais vārds, un runas sintēze teikumu neanalizē.

Tāpēc locījumu nosakām mēs, pēc nākamā vārda galotnes, un uzrakstām kārtas
skaitli vārdiem PIRMS teksts aiziet uz sintēzi. Ekrānā redzamais teksts
paliek neskarts — «59. minūtē» ir pareizi rakstīts, tikai nepareizi lasīts.

Otra puse tam pašam punktam: teikuma BEIGĀS punkts nav kārtas skaitļa zīme,
bet teikuma beigas. «Serbija uzvarēja ar rezultātu 76 pret 64.» balss nolasīja
kā «sešdesmit ceturtais», jo cipars un punkts izskatās vienādi abos gadījumos.
Tur skaitlis jāuzraksta kā pamata skaitlis — «sešdesmit četri».

APZINĀTA ROBEŽA: pārrakstām TIKAI tad, kad kontekstu var pateikt droši —
nākamais vārds ir ar mazo burtu (kārtas skaitlis) vai skaitlis ir teikuma
beigās (pamata skaitlis). Ja nevar (sveša galotne), atstājam ciparus, kā bija.
Uzminēts locījums skan sliktāk nekā tas, ko balss dara šodien.

ZINĀMA NEPILNĪBA: «1. Maija ielā» — nākamais vārds te ir ar lielo burtu, bet
tas nav jauns teikums, tāpēc iznāks «viens Maija ielā». Ielu nosaukumi ziņu
tekstā ir retāk nekā rezultāti un uzskaitījumi, tāpēc biežākais gadījums
uzvar; ja tāds parādīsies, to risina izrunas vārdnīca Noteikumos.
"""
from __future__ import annotations

import re

# Kārtas skaitļa celmi. Galotne nāk no locījuma (sk. _ENDINGS).
_STEM = {
    1: "pirm", 2: "otr", 3: "treš", 4: "ceturt", 5: "piekt", 6: "sest",
    7: "septīt", 8: "astot", 9: "devīt", 10: "desmit",
    11: "vienpadsmit", 12: "divpadsmit", 13: "trīspadsmit",
    14: "četrpadsmit", 15: "piecpadsmit", 16: "sešpadsmit",
    17: "septiņpadsmit", 18: "astoņpadsmit", 19: "deviņpadsmit",
    20: "divdesmit", 30: "trīsdesmit", 40: "četrdesmit", 50: "piecdesmit",
    60: "sešdesmit", 70: "septiņdesmit", 80: "astoņdesmit",
    90: "deviņdesmit", 100: "simt",
}

# Pamata skaitļa vārdi. Vajadzīgi divām lietām: daļām, kas kārtas skaitlī
# paliek priekšā (piecdesmit devītajā, nevis piecdesmitajā devītajā), un
# skaitļiem teikuma beigās, kur punkts nav kārtas skaitļa zīme.
_CARDINAL = {
    1: "viens", 2: "divi", 3: "trīs", 4: "četri", 5: "pieci", 6: "seši",
    7: "septiņi", 8: "astoņi", 9: "deviņi", 10: "desmit",
}
_TEENS = {
    11: "vienpadsmit", 12: "divpadsmit", 13: "trīspadsmit",
    14: "četrpadsmit", 15: "piecpadsmit", 16: "sešpadsmit",
    17: "septiņpadsmit", 18: "astoņpadsmit", 19: "deviņpadsmit",
}
_TENS = {
    20: "divdesmit", 30: "trīsdesmit", 40: "četrdesmit", 50: "piecdesmit",
    60: "sešdesmit", 70: "septiņdesmit", 80: "astoņdesmit",
    90: "deviņdesmit",
}

_ENDINGS = {
    "loc": "ajā",       # piecdesmit devītajā minūtē
    "loc_pl": "ajos",   # divdesmitajos gados
    "loc_pl_f": "ajās",
    "nom_m": "ais",     # pirmais puslaiks
    "nom_f": "ā",       # pirmā vieta; sakrīt ar ģenitīvu «pirmā maija»
    "nom_pl_m": "ie",   # deviņdesmitie gadi
    "gen_f": "ās",      # pirmās vietas
    "acc": "o",         # pakāpjas uz otro vietu (vīr. un siev. dz. sakrīt)
    "acc_pl_m": "os",   # pirmos gadus
    "dat_m": "ajam",
    "dat_f": "ajai",
}

# Nākamā vārda galotne -> locījums. Garumzīme te ir viss: «vietā» ir
# lokatīvs, «vieta» — nominatīvs, un balsij tie ir divi dažādi teikumi.
# Secība ir daļa no loģikas: garākā un šaurākā galotne pārbaudāma pirmā.
# «vietās» (lokatīvs dsk.) beidzas arī ar «s», un, ja «s» tiktu pārbaudīts
# agrāk, iznāktu «pirmais vietās».
_BY_SUFFIX: list[tuple[str, str]] = [
    ("ajās", "loc_pl_f"), ("ajos", "loc_pl"),
    ("ās", "loc_pl_f"), ("īs", "loc_pl"), ("ūs", "loc_pl"),
    ("as", "gen_f"),                       # pirmās vietas
    ("us", "acc_pl_m"),                    # pirmos gadus
    ("os", "loc_pl"),                      # pirmajos gados
    ("ai", "dat_f"), ("am", "dat_m"),
    ("ā", "loc"), ("ē", "loc"), ("ī", "loc"), ("ū", "loc"),
    ("u", "acc"),                          # uz otro vietu
    ("i", "nom_pl_m"),                     # deviņdesmitie gadi
    ("s", "nom_m"), ("š", "nom_m"),
    ("a", "nom_f"), ("e", "nom_f"),
]


def case_of(word: str) -> str:
    """Locījums, ko prasa nākamais vārds ('' ja nevar pateikt)."""
    w = (word or "").strip().lower()
    if not w or not w.isalpha():
        return ""
    for suffix, case in _BY_SUFFIX:
        if w.endswith(suffix):
            return case
    return ""


def ordinal(n: int, case: str = "loc") -> str:
    """Kārtas skaitlis vārdiem ('' ja skaitlis ārpus atbalstītā apjoma).

    Atbalstīts 1-999 un gadskaitļi 1000-2999, kuriem pēdējie divi cipari nav
    nulles. Pārējos neaiztiekam (sk. moduļa aprakstu).
    """
    end = _ENDINGS.get(case)
    if end is None or n < 1:
        return ""
    prefix: list[str] = []
    if 1000 <= n <= 2999:
        if n % 100 == 0:
            return ""            # «divtūkstošajā» — retums, labāk neminēt
        thousands = n // 1000
        prefix.append("tūkstoš" if thousands == 1 else "divi tūkstoši")
        n %= 1000
    if n >= 100:
        hundreds = n // 100
        if n % 100 == 0:
            return " ".join([*prefix, _STEM[100] + end]) if hundreds == 1 else ""
        prefix.append("simt" if hundreds == 1
                      else f"{_CARDINAL[hundreds]} simti")
        n %= 100
    if n == 0:
        return ""
    if n in _STEM:
        return " ".join([*prefix, _STEM[n] + end])
    tens, units = n // 10 * 10, n % 10
    return " ".join([*prefix, _STEM[tens], _STEM[units] + end])


# Kārtas skaitlis: aiz punkta seko vārds ar MAZO burtu (minūtē, vietā,
# septembrī). Lielais burts nozīmē jaunu teikumu, ne locījumu.
_ORDINAL_RE = re.compile(
    r"(?<![\d.,])(\d{1,4})\.(?=\s)\s+([a-zāčēģīķļņšūž]+)")
# Pamata skaitlis: punkts te beidz teikumu — aiz tā ir teksta beigas vai
# nākamais teikums ar lielo burtu.
_CARDINAL_RE = re.compile(
    r"(?<![\d.,])(\d{1,4})\.(?=\s*$|\s+[A-ZĀČĒĢĪĶĻŅŠŪŽ])")


def cardinal(n: int) -> str:
    """Pamata skaitlis vārdiem ('' ja ārpus atbalstītā apjoma 0-9999)."""
    if n < 0 or n > 9999:
        return ""
    if n == 0:
        return "nulle"
    parts: list[str] = []
    if n >= 1000:
        th = n // 1000
        parts.append("tūkstoš" if th == 1 else f"{_CARDINAL[th]} tūkstoši")
        n %= 1000
        if n == 0:
            return ("tūkstotis" if th == 1
                    else f"{_CARDINAL[th]} tūkstoši")
    if n >= 100:
        hu = n // 100
        parts.append("simt" if hu == 1 else f"{_CARDINAL[hu]} simti")
        n %= 100
        if n == 0 and len(parts) == 1 and hu == 1:
            return "simts"
    if n == 0:
        return " ".join(parts)
    if n in _CARDINAL:
        parts.append(_CARDINAL[n])
    elif n in _TEENS:
        parts.append(_TEENS[n])
    else:
        tens, units = n // 10 * 10, n % 10
        parts.append(_TENS[tens])
        if units:
            parts.append(_CARDINAL[units])
    return " ".join(parts)


def speak_ordinals(text: str) -> str:
    """«59. minūtē» -> «piecdesmit devītajā minūtē»,
    «...ar rezultātu 76 pret 64.» -> «...septiņdesmit seši pret sešdesmit četri.»

    Nākamo vārdu atstājam, kā bija — kontekstu nosakām pēc tā, nevis mainām.
    """
    def as_cardinal(m: re.Match) -> str:
        spelled = cardinal(int(m.group(1)))
        return f"{spelled}." if spelled else m.group(0)

    def as_ordinal(m: re.Match) -> str:
        number, word = m.group(1), m.group(2)
        spelled = ordinal(int(number), case_of(word))
        return f"{spelled} {word}" if spelled else m.group(0)

    # vispirms teikuma beigas: tur punkts nav kārtas skaitļa zīme
    text = _CARDINAL_RE.sub(as_cardinal, text)
    return _ORDINAL_RE.sub(as_ordinal, text)
