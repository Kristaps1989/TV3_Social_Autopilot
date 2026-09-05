"""AI decision layer: publish or not, where, in what format, with what copy.

Uses Claude via the Anthropic API when ANTHROPIC_API_KEY is set. Every call
is logged to decisions_log. If the API is unavailable or returns invalid
output, a deterministic fallback keeps the pipeline running: link post,
copy from the headline, next free slot.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import select

from app import claude, config, pagemeta
from app.best_practices import pick_format
from app.models import Article, DecisionLog, Post
from app.rules_engine import Verdict

log = logging.getLogger(__name__)

# Cik raksta teksta dodam modelim: pietiek faktiem un ierunai,
# bet netaisa promptu par rēķinu, ko maksā katrā lēmumā.
BODY_IN_PROMPT = 2500

DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record the publishing decision for this article.",
    "input_schema": {
        "type": "object",
        "properties": {
            "publish": {"type": "boolean"},
            "section": {
                "type": "string",
                "enum": ["news", "sport", "entertainment"],
                "description": "Raksta PATIESĀ sadaļa pēc satura (feed marķējums "
                               "mēdz būt kļūdains — NATO ziņas nav izklaide).",
            },
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "boostable": {
                "type": "boolean",
                "description": "Vai rakstu DRĪKST pastiprināt ar maksas reklāmu "
                               "ES: Meta nepieņem politiku, vēlēšanas un "
                               "sabiedriskos jautājumus (TTPA), un mēs nekad "
                               "nereklamējam traģēdijas/noziegumus.",
            },
            "boost_reason": {"type": "string",
                             "description": "Īss iemesls boostable lēmumam."},
            "reason": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {
                "type": "array",
                "items": {"type": "string",
                          "enum": ["none", "nudity", "party", "alcohol", "tragedy", "crime"]},
            },
            "channels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string"},
                        "format": {"type": "string",
                                   "enum": ["link", "photo", "photo_album", "text_only",
                                            "carousel", "card_carousel", "story",
                                            "reel", "video"]},
                        "copy": {"type": "string"},
                        "card_points": {
                            "type": "array", "items": {"type": "string"},
                            "maxItems": 5,
                            "description": "Rezerves variants, kad card_sections "
                                           "nesanāk (piem., nav raksta teksta): "
                                           "2-4 īsi fakti pa vienam kartītē.",
                        },
                        "card_sections": {
                            "type": "array", "maxItems": 5,
                            "description": "card_carousel un reel GALVENAIS saturs: "
                                           "raksts, sadalīts sadaļās. Katra kartīte = "
                                           "trekns virsraksts + 2-4 teikumi ar "
                                           "KONKRĒTIEM faktiem no raksta teksta "
                                           "(skaitļi, vārdi, ieteikumi). 3-4 sadaļas; "
                                           "labāk 3 spēcīgas nekā 5 uzpildītas. Ja "
                                           "rakstā ir praktiskā daļa (kur zvanīt, ko "
                                           "darīt), tā ir laba pēdējā sadaļa.",
                            "items": {"type": "object", "properties": {
                                "title": {"type": "string",
                                          "description": "līdz 60 zīmēm, bez punkta"},
                                "body": {"type": "string",
                                         "description": "2-4 pilni teikumi, "
                                                        "70-300 zīmes"}},
                                "required": ["title", "body"]},
                        },
                        "card_end_question": {
                            "type": "string",
                            "description": "Only for card_carousel: jautājums pēdējai "
                                           "kartītei, kas liek atvērt rakstu.",
                        },
                        "hook_type": {
                            "type": "string",
                            "enum": ["fact", "number", "question", "quote",
                                     "urgency", "curiosity"],
                            "description": "Copy āķa stils. Vienam rakstam uz "
                                           "dažādām platformām lieto ATŠĶIRĪGUS "
                                           "stilus — tas ir starpplatformu A/B "
                                           "tests, ko sistēma mēra.",
                        },
                        "hashtags": {"type": "array", "items": {"type": "string"}},
                        "image_index": {"type": "integer"},
                        "preferred_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    },
                    "required": ["channel", "format", "copy"],
                },
            },
        },
        "required": ["publish", "score", "reason", "channels"],
    },
}


def recent_posts_context(session, channels: list[str], limit: int = 10) -> str:
    lines = []
    for ch in channels:
        rows = session.execute(
            select(Post).where(Post.channel == ch,
                               Post.state.in_(("scheduled", "published")))
            .order_by(Post.created_at.desc()).limit(limit)
        ).scalars().all()
        for p in rows:
            title = p.article.title if p.article else ""
            lines.append(f"- [{ch}] {p.format}: {title}")
    return "\n".join(lines) if lines else "(nav nesenu ierakstu)"


def performance_context(session, channels: list[str]) -> str:
    from app import priors

    text = priors.prompt_context(session, channels)
    return text or "(vēl nav pietiekami daudz datu)"


def format_quota_context(session, channels: list[str], channels_cfg: dict) -> str:
    """Fakti par formātu kvotām ŠODIEN — modelis vienu rakstu redz atrauti
    un dienas kopsummu saskaitīt nevar, tāpēc to pasakām mēs."""
    from app import pipeline
    from app.formats import format_run, monotony_reason, recent_format_shares

    lines = []
    for ch in channels:
        cfg = channels_cfg.get(ch) or {}
        formats = cfg.get("formats") or []
        bits: list[str] = []
        closed: dict[str, str] = {}     # formāts -> iemesls (bez dublikātiem)
        prefer: list[str] = []          # ko tieši gaidām
        for fmt in pipeline.RICH_FORMATS:
            if fmt not in formats:
                continue
            cap = pipeline.format_daily_cap(cfg, fmt)
            used = pipeline.posts_today(session, ch, fmt)
            if cap is None:
                bits.append(f"{used} {fmt}")
                continue
            bits.append(f"{used}/{cap} {fmt}")
            if used >= cap:
                closed[fmt] = f"dienas kvota {used}/{cap}"
        floor = float((cfg.get("format_mix") or {}).get("link") or 0)
        if "link" in formats and floor:
            share = recent_format_shares(session, ch).get("link", 0.0)
            bits.append(f"saites daļa pēdējos ierakstos {share:.0%} (grīda {floor:.0%})")
            if share < floor:
                prefer.append("link (saites grīda nav izpildīta)")
        head, run = format_run(session, ch)
        if head and run >= 2:
            bits.append(f"pēdējie {run} ieraksti pēc kārtas ir {head}")
        for fmt in formats:
            why = monotony_reason(session, ch, cfg, fmt)
            if why and fmt not in closed:
                closed[fmt] = why
        if bits:
            tail = ""
            if closed:
                tail += (" → šodien vairs nepiedāvā: "
                         + ", ".join(f"{f} ({why})" for f, why in closed.items()))
            if prefer:
                tail += " → priekšroka: " + ", ".join(prefer)
            lines.append(f"- {ch}: šodien jau {', '.join(bits)}{tail}")
    return "\n".join(lines) if lines else "(kvotas brīvas)"


# Formātu rokasgrāmata ir VIENĀDA katram rakstam, tāpēc tā pieder sistēmas
# promptam, ne lietotāja ziņai: sistēmas prompts ir kešots un atkārtotā
# ievade maksā ~10 %, bet lietotāja ziņa katru reizi tiek apmaksāta pilnā
# cenā. Piecus kilobaitus reizināt ar katru lēmumu nav par ko.
# Platformu pamācības fiksētā kārtībā: sistēmas prompts nedrīkst mainīties no
# raksta uz rakstu, citādi kešs sadalās vairākos prefiksos.
ALL_PLATFORMS = ("facebook_page", "instagram", "threads", "x")


FORMAT_GUIDE = """Formātu izvēle. Noklusējums ikdienas ziņai ir link: saites kartīte ar
virsrakstu un CTA dod labāko klikšķu attiecību uz portālu, tā ir vienīgais
formāts, ko Facebook var pastiprināt kā traffic reklāmu, un tikai ar to
sistēma iemācās, ko saite ir vērta. Izvēlies link, ja raksts ir notikums,
rezultāts, paziņojums, viena fakta ziņa — vairums rakstu ir tādi.

Formāts card_carousel (ja kanāls to atbalsta un kvota brīva): svaipojams
kartīšu karuselis, kur KATRA kartīte ir klikšķināma saite uz rakstu —
tikai skaidrojumiem, sarakstiem un "X lietas, kas jāzina" stāstiem, kur
rakstam ir vismaz 2-3 patstāvīgas daļas. Saturu dod card_sections: katra
kartīte ir trekns virsraksts plus 2-4 pilni teikumi ar konkrētiem faktiem
no RAKSTA TEKSTA (skaitļi, vārdi, ieteikumi). Piemērs: title "Spēcīgas vēja
brāzmas", body "Vēja ātrums vietām var sasniegt 30 m/s. Vētras laikā
ieteicams neapmeklēt parkus un bērnu rotaļu laukumus." Praktiskā daļa (kur
zvanīt, ko darīt) ir laba pēdējā sadaļa; card_end_question ved uz pilno
rakstu ar neatbildētu niansi. Ja raksta teksta nav, card_points ar īsiem
faktiem ir rezerves variants. Karuselis vienas ziņas rakstam ir kļūda — tad
link vai photo.

Formāts reel (ja kanāls to atbalsta): vertikāls video ar CTA beigu kadru
"lasi tv3.lv". Ja rakstam IR videoklips, reel izmanto īsto video — dod tam
priekšroku vizuāliem stāstiem, tas ir spēcīgākais formāts; arī story
formāts tad automātiski iznāk kā video stāsts ar CTA beigu kadru. Ja video nav,
reel ir slideshow (vāks → 2-3 sadaļu kadri → CTA); tad aizpildi
card_sections (2-3 sadaļas — tie paši virsraksts + teikumi kā karuselī;
bez raksta teksta der card_points). Ne biežāk kā ~2x dienā kanālā.

Lentes ieruna nav atsevišķs teksts blakus kadriem — tā IR kadru teksts.
Balss katrā kadrā nolasa tieši tās sadaļas body, un kadrs ir tieši tik garš,
cik tā ieruna. Tāpēc:
  - sadaļu body raksti RUNĀŠANAI: īsi teikumi, bez iekavām, saīsinājumiem un
    URL, skaitļi vārdiem, kad tā runā;
  - sadaļas jālasa kā VIENS plūstošs stāsts pēc kārtas, nevis kā trīs
    atsevišķi paziņojumi — otrā turpina pirmo, nevis sāk no gala;
  - title ir nodaļas MARĶIERIS (2-4 vārdi), ne body pirmā teikuma atstāstījums:
    balss to nelasa, tas stāv uz ekrāna. Ja title un body saka to pašu,
    skatītājs vienu domu saņem divreiz.
Ievadu neraksti: pār vāka kadru balss nolasa raksta virsrakstu, un atsevišķs
āķis tur nāca kā tā paša atkārtojums pirms stāsta sākuma. Ja raksta teksta
nav, atstāj card_sections tukšu — labāk klusa lente nekā izdomāts saturs.

link pret photo: link posts ir galvenais klikšķu formāts — saites kartīte ar
virsrakstu un CTA pogu; izvēlies to ikdienas ziņām. photo lieto, kad attēls
pats ir stāsts (spēcīgs foto, gatava photopost grafika, emocionāls kadrs);
tad saite ir gan aprakstā, gan pirmajā komentārā. Maksas pastiprināšanai der
visi trīs formāti (karuseļa katra kartīte ir saite), un kurš no tiem par eiro
atved vairāk sesiju, sistēma mēra pati — tas nav arguments, kas tev jāmin.

Formātu līdzsvars dienā: vairums ierakstu ir link, ~1-2 card_carousel un
~1-2 reel tur, kur saturs tam tiešām der, photo — kad attēls ir stāsts.
Kvotas augstāk ir stingras; tās nevar "pataupīt" nākamajam rakstam.

Otrais vilnis: dienas spēcīgākajiem rakstiem (score >= 0.75) drīksti pieteikt
VIENU kanālu DIVAS reizes ar dažādiem formātiem — piem. photo tagad un link
posts stundu vēlāk. Otrajam ierakstam raksti CITU tekstu un citu hook_type
(cits leņķis, ne pārfrāzēts tas pats), citādi tas izskatās pēc kļūdas.
Sistēma ieplāno otro automātiski ar stundas nobīdi. ~1-2 raksti dienā, ne
vairāk — pārējiem pietiek ar vienu ierakstu.

Maksas pastiprināšana (boostable): atzīmē, vai šo rakstu drīkstētu
reklamēt ES. boostable=false, ja tēma Meta izpratnē ir politika, vēlēšanas
vai sabiedriskie jautājumi (valdība, Saeima, partijas, karš, migrācija,
veselības/izglītības politika, protesti u.tml.) — Meta ES tādas reklāmas
vairs nepieņem — vai ja saturs ir traģēdija/noziegums. Sports, izklaide,
dzīvesstils, patērētāju un servisa ziņas parasti ir boostable=true.
Šis NEIETEKMĒ organisko publicēšanu — tikai maksas kampaņas.

Satura izmantošana: lēmums vienmēr ir tavs (redaktora statuss ir signāls,
ne pavēle), bet noklusējums ir PUBLICĒT — nepublicēts raksts ir izniekots
redakcijas darbs. publish=false lieto tikai tiešām nederīgam saturam
(dublikāts, servisa paziņojums, tukša ziņa) un vienmēr ar konkrētu iemeslu.

Valoda: nevainojama latviešu pareizrakstība visos tekstos un card_points —
pareizas galotnes, locījumi un garumzīmes; pirms atbildes pārlasi katru
teikumu. Kļūdains teksts ziņu zīmolam nav pieļaujams.

Āķu A/B: katram kanālam norādi hook_type un vienam rakstam uz dažādām
platformām apzināti izmanto dažādus āķu stilus — sistēma mēra, kurš stils
kurā sadaļā atved vairāk lasītāju, un tu redzēsi rezultātus veiktspējas
sadaļā zemāk.

Pieņem lēmumu ar record_decision. Ja raksts nav pietiekami interesants
('can' statuss ļauj izlaist), atzīmē publish=false ar īsu iemeslu latviski."""


def build_user_prompt(article: Article, verdicts: dict[str, Verdict],
                      channels_cfg: dict, session) -> str:
    eligible = {n: v for n, v in verdicts.items() if v.outcome in ("eligible", "forced_now")}
    channel_desc = []
    for name, v in eligible.items():
        cfg = channels_cfg.get(name) or {}
        channel_desc.append(
            f"- {name} ({cfg.get('platform')}): formāti {cfg.get('formats')}, "
            f"statuss: {v.reason}"
        )
    # CMS metadati no raksta lapas (autors, redakcijas tagi, apjoms,
    # galerija, "Tikai tv3.lv") — tukšs, ja lapa nav ievilkta.
    cms = pagemeta.prompt_lines(article)
    # Raksta pašas rindkopas: bez tām punkti un ieruna top no virsraksta,
    # un tad tie ir pārstāsts, nevis fakti.
    body = pagemeta.article_body(article)
    body_block = (f"Raksta teksts (sākums):\n{body[:BODY_IN_PROMPT]}\n"
                  if body else "")
    return f"""Raksts:
Virsraksts: {article.title}
Ievads: {article.lead[:600]}
{body_block}
Sadaļa (no feed, var būt kļūdaina — klasificē pats laukā section): {article.section}
Attēli: {len(article.images or [])}
Video: {pagemeta.video_hint(article)}
{("TV3 Play: " + _play_hint(article)) if _play_hint(article) else ""}
Redaktora statuss: {article.editor_status}
Publicēts: {article.published_at}
{cms}

Pieejamie kanāli:
{chr(10).join(channel_desc)}

Nesenie ieraksti (neatkārto leņķus):
{recent_posts_context(session, list(eligible))}

Izmērītā veiktspēja (izmanto formāta un laika izvēlē):
{performance_context(session, list(eligible))}

Formātu kvotas šodien (sistēmas fakti; slēgtu formātu nepiedāvā — sistēma
to tik un tā pārvērstu par saiti):
{format_quota_context(session, list(eligible), channels_cfg)}
"""



def call_claude(article: Article, verdicts: dict[str, Verdict], session) -> dict | None:
    from app import credentials

    api_key = credentials.get("anthropic_api_key", session)
    if not api_key:
        return None
    import anthropic

    channels_cfg = config.load_channels()
    strong = article.editor_status in ("now", "must")
    model = config.AI_MODEL_STRONG if strong else config.AI_MODEL_FAST
    # Sistēmas prompts ir VIENĀDS katram rakstam — arī tad, kad daļa kanālu šim
    # rakstam nav derīgi. Kešs ir prefiksa sakritība: kamēr platformu pamācības
    # pielika pēc derīgajiem kanāliem, katra kanālu kombinācija bija SAVS
    # prefikss ar savu piecu minūšu kešu, un retāk sastopamās kombinācijas
    # nekad nepaspēja sasilt. Neizmantotā pamācība kešā maksā ~10 %, bet
    # netrāpīts kešs maksā visu prefiksu pilnā cenā — tāpēc liekam visas.
    system = config.system_prompt_for("")
    for p in ALL_PLATFORMS:
        system += "\n\n" + config.system_prompt_for(p)
    system += "\n\n" + FORMAT_GUIDE
    user = build_user_prompt(article, verdicts, channels_cfg, session)

    client = anthropic.Anthropic(api_key=api_key)
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model,
                # 4 kanālu copy + kartītes + ierunas teksts; domājošam
                # modelim griestos ietilpst arī domāšana
                max_tokens=claude.max_tokens_for(model, 1500),
                system=claude.cached_system(system),
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": user}],
                **claude.params(model, "medium"),
            )
            decision = next((b.input for b in resp.content if b.type == "tool_use"), None)
            log.info("claude %s: in=%d (cache read %d) out=%d", model,
                     resp.usage.input_tokens,
                     getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                     resp.usage.output_tokens)
            session.add(DecisionLog(
                article_id=article.id, model=model,
                prompt_hash=hashlib.sha256((system + user).encode()).hexdigest()[:16],
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                cached_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                raw_response=json.dumps(decision, ensure_ascii=False) if decision else "",
            ))
            if decision and validate_decision(decision):
                return decision
            log.warning("invalid AI decision for article %s (attempt %d)", article.id, attempt)
        except Exception as e:  # noqa: BLE001
            log.warning("Claude call failed for article %s: %s", article.id, e)
            break
    return None


def validate_decision(d: dict) -> bool:
    if not isinstance(d.get("publish"), bool):
        return False
    if d["publish"] and not d.get("channels"):
        return False
    for ch in d.get("channels") or []:
        if not ch.get("channel") or not isinstance(ch.get("copy"), str):
            return False
    return True


def fallback_decision(article: Article, verdicts: dict[str, Verdict]) -> dict:
    """Deterministic safe default: link post with headline copy on every
    eligible channel for must/now; skip weak 'can' items (no AI to judge them)."""
    channels_cfg = config.load_channels()
    eligible = [n for n, v in verdicts.items() if v.outcome in ("eligible", "forced_now")]
    publish = article.editor_status in ("now", "must") and bool(eligible)
    copy = article.title
    lead = (article.lead or "").strip()
    # only append the lead when it actually adds something — many feeds
    # repeat the headline as the first sentence of the lead
    if lead:
        from app.rules_engine import title_similarity

        head = lead[: len(article.title) + 30]
        if not lead.lower().startswith(article.title.lower()[:40]) \
                and title_similarity(article.title, head) < 0.6:
            copy = f"{article.title} — {lead[:150]}"
    return {
        "publish": publish,
        "score": {"now": 0.95, "must": 0.8}.get(article.editor_status, 0.4),
        "reason": ("fallback: publicēts pēc redaktora statusa" if publish
                   else "fallback: 'can' bez AI vērtējuma netiek publicēts"),
        "labels": [article.section],
        "sensitivity": [],
        "channels": [
            {
                "channel": name,
                "format": pick_format(article.section, article.images or [],
                                      article.editor_status,
                                      (channels_cfg.get(name) or {}).get("formats") or ["link"]),
                "copy": copy,
                "hashtags": [],
                "image_index": 0,
            }
            for name in eligible
        ] if publish else [],
    }


def _play_hint(article) -> str:
    from app import play

    return play.hint(article) if play.is_play_item(article) else ""


def reusable_decision(article: Article, verdicts: dict[str, Verdict]) -> dict | None:
    """Iepriekšējais lēmums šim pašam rakstam, ja tas vēl der.

    Raksts, kam rinda bija pilna, atgriežas cikla rindā vēl astoņas reizes.
    Katra no tām līdz šim bija PILNS jauns Claude izsaukums, kaut mainījās
    tikai pulkstenis: raksts tas pats, kanāli tie paši, atbilde tā pati.
    Astoņkārtīga cena par vienu lēmumu. Tāpēc lēmumu glabājam pie raksta un
    atkārtoti izmantojam, kamēr derīgo kanālu kopa nav mainījusies — ja kāds
    kanāls pa to laiku ir aizvēries vai atvēries, atbilde jāpārrēķina.
    """
    saved = (article.raw_json or {}).get("_decision")
    if not isinstance(saved, dict):
        return None
    kept = saved.get("decision")
    if not isinstance(kept, dict) or not validate_decision(kept):
        return None
    eligible = sorted(n for n, v in verdicts.items()
                      if v.outcome in ("eligible", "forced_now"))
    if list(saved.get("channels") or []) != eligible:
        return None
    return kept


def remember_decision(article: Article, verdicts: dict[str, Verdict],
                      decision: dict) -> None:
    raw = dict(article.raw_json or {})
    raw["_decision"] = {
        "channels": sorted(n for n, v in verdicts.items()
                           if v.outcome in ("eligible", "forced_now")),
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "decision": decision,
    }
    article.raw_json = raw


def decide(article: Article, verdicts: dict[str, Verdict], session) -> dict:
    decision = reusable_decision(article, verdicts)
    if decision is not None:
        session.add(DecisionLog(article_id=article.id, model="(atkārtoti)",
                                reused=1, raw_response=""))
        log.info("article %s: izmantots iepriekšējais lēmums, izsaukuma nav",
                 article.id)
    else:
        decision = call_claude(article, verdicts, session)
        if decision is None:
            decision = fallback_decision(article, verdicts)
        else:
            remember_decision(article, verdicts, decision)
    article.decided_at = datetime.utcnow()
    article.ai_score = float(decision.get("score") or 0)
    article.ai_reason = str(decision.get("reason") or "")
    article.labels = decision.get("labels") or []
    article.sensitivity = [s for s in (decision.get("sensitivity") or []) if s != "none"]
    if isinstance(decision.get("boostable"), bool):
        raw = dict(article.raw_json or {})
        raw["_boostable"] = decision["boostable"]
        raw["_boost_reason"] = str(decision.get("boost_reason") or "")
        article.raw_json = raw
    return decision
