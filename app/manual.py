"""Redaktora rokas vadība: uztaisi ŠIM rakstam ŠO formātu tagad.

Kāpēc tas vajadzīgs. Reelus un karuseļus automātika piedāvā reti, un tas nav
kļūda, bet trīs noteikumu summa: diversitātes dzinējs tos nekad neizvēlas
(`formats.suitable_formats` tos izlaiž), tos ierosina TIKAI AI, un promptā
tai ir teikts taupīt — ikdienas ziņai labāks ir saites ieraksts. Rezultātā
redaktors, kurš zina, ka šis konkrētais raksts ir lentes vērts, gaida, kad
AI tam nejauši piekritīs.

Šeit viņš to var pateikt pats. Rokas ieraksts iet caur tieši to pašu ceļu,
ko automātiskais — tā pati grafika, tā pati ieruna, tas pats laika plānotājs
un tie paši kanāla ierobežojumi — atšķiras tikai tas, KURŠ izvēlējās formātu.
Kanāla atstarpes un klusās stundas paliek spēkā: tās sargā kontu, nevis
ierobežo redaktoru.
"""
from __future__ import annotations

import logging

from app import config, disclosure, pagemeta, runtime
from app.best_practices import sanitize_copy
from app.models import Evaluation, Post, utcnow
from app.pipeline import format_media, resolve_format, upgrade_pending_stories
from app.rules_engine import evaluate_all
from app.slots import plan_slot

log = logging.getLogger(__name__)

# Ko redaktors drīkst pieprasīt. text_only nav sarakstā apzināti: ieraksts
# bez attēla un bez saites nav nekā, ko rokas režīmā gribētos.
REQUESTABLE = ("reel", "card_carousel", "photo", "photo_album", "story", "link")

# Cik sadaļu prasām kartītēm/lentei
POINTS = {"reel": 3, "card_carousel": 4}


def options(channel_cfg: dict) -> list[str]:
    """Formāti, ko šim kanālam drīkst pieprasīt ar roku."""
    allowed = channel_cfg.get("formats") or []
    return [f for f in REQUESTABLE if f in allowed]


def channel_options(session=None) -> dict[str, list[str]]:
    return {name: options(cfg) for name, cfg in config.load_channels().items()}


def unavailable(session=None) -> list[str]:
    """Formāti, ko neviens aktīvs kanāls nepieņem.

    Tieši šeit pazūd reeli: ja `channels.yaml` kanāla `formats` sarakstā
    «reel» nav, AI to var ierosināt cik grib — `resolve_format` to klusi
    nomet un atgriežas pie saites ieraksta. Rediģējamā konfigurācija mēdz
    būt vecāka par kodu, tāpēc to ir vērts pateikt skaļi.
    """
    accepted = {f for cfg in config.load_channels().values()
                for f in (cfg.get("formats") or [])}
    return [f for f in REQUESTABLE if f not in accepted]


def _sections(session, article, n: int) -> list[dict]:
    """Stāsta sadaļas šim rakstam ([{title, body}]; tukšs, ja AI nav).

    Sadaļas nāk no raksta TEKSTA, nevis virsraksta — tieši tāpēc raksta
    rindkopas tiek vilktas no lapas. Katra kartīte ir trekns virsraksts un
    2-4 teikumi ar faktiem, kā to dara labākie ziņu konti.
    """
    from app.pipeline import clean_sections
    from app.weekend import _ai_lines

    body = pagemeta.article_body(article) or article.lead or ""
    if not body.strip():
        return []
    prompt = (
        f"Raksts:\nVirsraksts: {article.title}\n\n{body[:1800]}\n\n"
        f"Sadali ŠO rakstu {n} kartīšu sadaļās latviski. Katra sadaļa vienā "
        "rindā formātā:\nVirsraksts | Teksts\n"
        "- Virsraksts: trekns apgalvojums līdz 60 zīmēm, bez punkta beigās;\n"
        "- Teksts: 2-4 pilni teikumi ar KONKRĒTIEM faktiem no raksta "
        "(skaitļi, vārdi, ieteikumi), 70-300 zīmes;\n"
        "- ja rakstā ir praktiskā daļa (kur zvanīt, ko darīt), tā ir laba "
        "pēdējā sadaļa.\n"
        "Ja rakstā tik daudz satura nav, uzraksti mazāk sadaļu — uzpildītas "
        "ir sliktākas par divām trāpīgām. Atbildē TIKAI sadaļu rindas."
    )
    out = []
    for ln in _ai_lines(session, prompt, max_tokens=800):
        head, sep, text = ln.partition("|")
        if sep:
            out.append({"title": head.strip(" -•*0123456789."),
                        "body": text.strip()})
    return clean_sections(out)[:n]


def _voice(session, article) -> str:
    """Ierunas teksts lentei ('' ja balss nav ieslēgta vai teksta nav)."""
    from app import tts
    from app.weekend import _ai_lines

    if not tts.enabled(session=session):
        return ""
    body = pagemeta.article_body(article)
    if not body.strip():
        return ""
    prompt = (
        f"Raksts:\nVirsraksts: {article.title}\n\n{body[:1500]}\n\n"
        "Uzraksti ierunas tekstu (voice-over) vertikālai lentei latviski, "
        "45-90 vārdi. Rakstīts RUNĀŠANAI: īsi teikumi, bez iekavām, bez "
        "saīsinājumiem, bez saitēm. Izstāsti, kas notika, ar konkrētiem "
        "faktiem no raksta, un beidz ar aicinājumu lasīt tv3.lv. "
        "Atbildi TIKAI ar pašu tekstu."
    )
    return " ".join(_ai_lines(session, prompt, max_tokens=400)).strip()


def _copy_for(session, article, channel: str) -> str:
    """Ieraksta teksts: ja AI šim rakstam un kanālam jau ko uzrakstīja,
    lietojam to — rokas režīms maina formātu, nevis toni."""
    for post in sorted(article.posts, key=lambda p: p.id, reverse=True):
        if post.channel == channel and (post.copy or "").strip():
            return post.copy
    lead = (article.lead or "").strip()
    if lead and not lead.lower().startswith(article.title.lower()[:40]):
        return f"{article.title} — {lead[:150]}"
    return article.title


def build(session, article, channel: str, fmt: str) -> tuple[Post | None, str]:
    """Uztaisa vienu ierakstu pēc redaktora pieprasījuma.

    Atgriež (ieraksts, ziņojums). Ieraksts ir None, kad neizdodas, un
    ziņojums tad pasaka, kāpēc — redaktoram ir jāsaprot, vai vainīgs raksts,
    kanāls vai renderētājs.
    """
    now = utcnow()
    cfg = (config.load_channels() or {}).get(channel)
    if cfg is None:
        return None, f"Kanāls «{channel}» nav aktīvs."
    if fmt not in options(cfg):
        return None, f"Kanāls «{channel}» formātu «{fmt}» nepieņem."
    if article.editor_status == "dont":
        return None, "Rakstam ir redaktora statuss «dont» — ieraksts netiek veidots."

    verdict = evaluate_all(article, now).get(channel)
    if verdict is None:
        return None, f"Kanālam «{channel}» nav vērtējuma."

    ch_dec: dict = {"channel": channel, "format": fmt,
                    "copy": _copy_for(session, article, channel)}
    if fmt in POINTS:
        sections = _sections(session, article, POINTS[fmt])
        if len(sections) < 2:
            return None, ("Nesanāca sagatavot vismaz 2 kartīšu sadaļas. "
                          "Vajag AI atslēgu un raksta tekstu — pārbaudi, vai "
                          "rakstam Rakstu sarakstā ir «teksts: N zīmes».")
        ch_dec["card_sections"] = sections
        ch_dec["card_end_question"] = "Uzzini visu stāstu tv3.lv"
        if fmt == "reel":
            ch_dec["voice_script"] = _voice(session, article)

    recipe: dict = {}
    if fmt in ("reel", "card_carousel"):
        built, media, recipe = resolve_format(session, channel, cfg, article, ch_dec)
        if built != fmt or not media:
            return None, (f"«{fmt}» šim rakstam uzbūvēt neizdevās — renderētājs "
                          "vai ffmpeg nav pieejams (sk. Pārskatu).")
    else:
        media = format_media(article, cfg, fmt)
        if media is None:
            return None, "Šim formātam vajag attēlu, un rakstam tāda nav."

    platform = cfg.get("platform", "")
    copy, hashtags, _fixes = sanitize_copy(
        ch_dec["copy"], pagemeta.hashtags(article), platform,
        article.sensitivity, reserve_link_chars=True,
        reserve_chars=len(disclosure.caption_line(platform)) + 2)

    # Kanāla atstarpes un klusās stundas paliek spēkā arī rokas režīmā: tās
    # pasargā kontu no pārblīvēšanas, un «tūlīt» šeit nozīmē «nākamajā
    # derīgajā logā», nevis «pāri visiem noteikumiem».
    slot, why = plan_slot(session, channel, cfg, verdict, article.section, fmt,
                          article.title, now, None,
                          score=float(article.ai_score or 0), allow_similar=True)
    if slot is None:
        import dataclasses

        slot, why = plan_slot(session, channel, cfg,
                              dataclasses.replace(verdict, latest=None),
                              article.section, fmt, article.title, now, None,
                              score=float(article.ai_score or 0),
                              allow_similar=True)
    if slot is None:
        return None, f"Nav derīga laika: {why}"

    from app import cards as cards_mod

    post = Post(
        article_id=article.id, channel=channel, format=fmt,
        copy=copy, hashtags=hashtags, media=media,
        hook_type=str(ch_dec.get("hook_type") or ""),
        link_url=article.canonical_url or article.url,
        scheduled_at=slot, state="scheduled",
        dry_run=runtime.is_dry_run(session),
        extra=(({"render_version": cards_mod.RENDER_VERSION}
                | ({"recipe": recipe} if recipe else {})
                | {"manual": True}) if media else {"manual": True}),
    )
    session.add(post)
    session.flush()
    session.add(Evaluation(
        article_id=article.id, channel=channel, outcome="posted",
        reason=f"redaktora pieprasīts {fmt}, ieplānots {slot:%Y-%m-%d %H:%M} UTC"))
    # rokas lente ir biežākais gadījums, kad lente top PĒC stāsta: jau
    # ieplānotais stāsts to pārņem uzreiz, ne tikai publicēšanas brīdī
    taken = upgrade_pending_stories(session, article) if fmt == "reel" else 0
    session.commit()
    log.info("manual %s for article %s on %s -> post %s",
             fmt, article.id, channel, post.id)
    note = f" Raksta ieplānotais stāsts pārņēma šo lenti ({taken})." if taken else ""
    return post, f"«{fmt}» ieplānots {slot:%d.%m. %H:%M} UTC kanālā {channel}.{note}"
