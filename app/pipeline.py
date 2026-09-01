"""Pipeline steps wired together by the scheduler:

  ingest -> evaluate (rules) + decide (AI) -> create posts -> publish due
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from adapters import get_adapter
from adapters.base import PublishError
from app import config, disclosure, pagemeta, shortlinks, tts
from app.best_practices import (add_utm, alt_text, assemble_post_text,
                                sanitize_copy)
from app.decide import decide
from app.formats import choose_format, mix_deficit, recent_format_shares
from app.models import Article, Evaluation, Post, get_setting, utcnow
from app.rules_engine import evaluate_all
from app.slots import plan_slot
from app import runtime

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def paused(session, channel: str | None = None) -> bool:
    if get_setting(session, "kill_switch") == "on":
        return True
    if channel and get_setting(session, f"pause:{channel}") == "on":
        return True
    return False


def run_decisions(session, limit: int = 20) -> int:
    """Evaluate + decide undecided articles, create scheduled posts."""
    now = utcnow()
    articles = session.execute(
        select(Article)
        .where(Article.decided_at.is_(None), Article.editor_status != "dont")
        .order_by(Article.first_seen_at)
        .limit(limit)
    ).scalars().all()

    channels_cfg = config.load_channels()
    created = 0
    # Raksta lapa nes to, kā feed'ā nav (autors, redakcijas tagi, video/
    # galerija, apjoms, "Tikai tv3.lv"), un tas maina AI lēmumu — tāpēc to
    # velkam tieši šeit. Budžets uz ciklu tur ciklu īsu arī tad, kad portāls
    # atbild lēni; pārējos pēc tam papildina ielases backfill.
    meta_budget = 8

    for article in articles:
        if retry_pending(article, now):
            continue  # queue was full: waiting out the backoff before retrying
        if meta_budget > 0 and not pagemeta.meta(article):
            pagemeta.enrich(article)
            meta_budget -= 1
        verdicts = evaluate_all(article, now)
        for channel, verdict in verdicts.items():
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome=verdict.outcome, reason=verdict.reason))
        if all(v.outcome == "blocked" for v in verdicts.values()):
            article.decided_at = now
            session.commit()
            continue

        decision = decide(article, verdicts, session)
        maybe_correct_section(article, decision)
        scheduled_here = 0

        if not decision.get("publish"):
            for channel, verdict in verdicts.items():
                if verdict.outcome != "blocked":
                    session.add(Evaluation(article_id=article.id, channel=channel,
                                           outcome="ai_skip",
                                           reason=decision.get("reason", "")))
            session.commit()
            continue

        for ch_dec in order_channels(decision.get("channels") or []):
            channel = ch_dec.get("channel", "")
            verdict = verdicts.get(channel)
            cfg = channels_cfg.get(channel)
            if verdict is None or cfg is None or verdict.outcome == "blocked":
                continue

            # One post per article and channel — except the deliberate second
            # wave: a strong story may go out twice in DIFFERENT formats (the
            # photo carries the visual, the link post an hour later carries the
            # clickable card that paid campaigns can amplify).
            existing = session.execute(
                select(Post).where(Post.article_id == article.id, Post.channel == channel,
                                   Post.state.in_(("proposed", "scheduled", "publishing",
                                                   "published")))
            ).scalars().all()
            repost_at = repost_offset(article, cfg, existing)
            if existing and repost_at is None:
                continue

            fmt, card_media, recipe = resolve_format(session, channel, cfg,
                                                     article, ch_dec)
            if any(p.format == fmt for p in existing):
                # the second wave only earns its place as a different format
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason=f"atkārtojums tajā pašā formātā ({fmt})"))
                continue

            platform = cfg.get("platform", "")
            copy, hashtags, fixes = sanitize_copy(
                ch_dec.get("copy") or article.title,
                # kad AI hashtagus nedod, ņemam redakcijas pašas birkas
                ch_dec.get("hashtags") or pagemeta.hashtags(article),
                platform, article.sensitivity, reserve_link_chars=True,
                # MI atrunai vieta jāatvēl PIRMS apgriešanas, citādi tvīts
                # ar to pārsniedz limitu tieši tad, kad teksts ir garš
                reserve_chars=len(disclosure.caption_line(platform)) + 2,
            )

            preferred = repost_at
            # asap režīmā AI ieteiktā stunda NEDRĪKST aizkavēt saturu (tā
            # pārcēla postus uz nākamās dienas pusdienlaiku); to izmanto
            # tikai optimize režīms
            asap_mode = str(config.load_rules().get(
                "scheduling_mode", "asap")).lower() != "optimize"
            if not asap_mode and isinstance(ch_dec.get("preferred_hour"), int):
                ph = ch_dec["preferred_hour"]
                candidate = now.replace(minute=0, second=0, microsecond=0)
                for _ in range(30):
                    from zoneinfo import ZoneInfo
                    local = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                        ZoneInfo(config.TIMEZONE))
                    if local.hour == ph and candidate >= now:
                        preferred = candidate
                        break
                    candidate += timedelta(hours=1)

            score = float(article.ai_score or 0)
            slot, why = plan_slot(session, channel, cfg, verdict,
                                  article.section, fmt, article.title, now, preferred,
                                  score=score, allow_similar=bool(existing))
            late = False
            if slot is None and verdict.latest is not None:
                # queue was full inside the status window — a later slot still
                # beats dropping content the AI decided to publish
                import dataclasses

                slot, why = plan_slot(session, channel, cfg,
                                      dataclasses.replace(verdict, latest=None),
                                      article.section, fmt, article.title, now,
                                      preferred, score=score,
                                      allow_similar=bool(existing))
                late = slot is not None
            if slot is None:
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason=f"nav derīga laika: {why}"))
                continue

            media = format_media(article, cfg, fmt,
                                 ch_dec.get("image_index") or 0, card_media)
            if media is None:
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason="story needs an image / renderer"))
                continue
            from app import cards as cards_mod

            post = Post(
                article_id=article.id, channel=channel, format=fmt,
                copy=copy, hashtags=hashtags, media=media,
                hook_type=str(ch_dec.get("hook_type") or ""),
                link_url=article.canonical_url or article.url,
                scheduled_at=slot, state="scheduled", dry_run=runtime.is_dry_run(session),
                # ar kādu grafiku izkārtojumu šis attēls uzzīmēts: ieraksts var
                # nostāvēt rindā stundas, un dizaina labojums citādi to vairs
                # neskartu (sk. refresh_missing_media)
                extra=(({"render_version": cards_mod.RENDER_VERSION}
                        | ({"recipe": recipe} if recipe else {}))
                       if media else {}),
            )
            session.add(post)
            session.flush()
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome="posted",
                                   reason=f"scheduled {slot:%Y-%m-%d %H:%M} UTC as {fmt}"
                                          + (" (otrais vilnis)" if existing else "")
                                          + (" (vēlāk — rinda bija pilna)" if late else "")
                                          + (f" (fixes: {', '.join(fixes)})" if fixes else "")))
            created += 1
            scheduled_here += 1
        if scheduled_here == 0:
            requeue_for_retry(article, now)
        session.commit()
    return created


# The queue can be genuinely full when an article is decided. Dropping it
# there wastes editorial work, so such articles are re-decided on later
# cycles until they land — bounded, because freshness rules eventually
# block them anyway.
MAX_DECISION_RETRIES = 8
RETRY_BACKOFF_MINUTES = 20


def retry_pending(article, now) -> bool:
    """True while an article is waiting out its retry backoff."""
    stamp = (article.raw_json or {}).get("_decide_retry_after")
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(str(stamp)) > now
    except ValueError:
        return False


def requeue_for_retry(article, now) -> None:
    raw = dict(article.raw_json or {})
    tries = int(raw.get("_decide_retries") or 0) + 1
    raw["_decide_retries"] = tries
    raw["_decide_retry_after"] = (
        now + timedelta(minutes=RETRY_BACKOFF_MINUTES * tries)).isoformat()
    article.raw_json = raw
    if tries <= MAX_DECISION_RETRIES:
        article.decided_at = None  # picked up again by the next decision run
        log.info("article %s scheduled nowhere (attempt %d) — will retry",
                 article.id, tries)
    else:
        article.decided_at = now
        log.info("article %s scheduled nowhere after %d attempts — giving up",
                 article.id, tries)


def maybe_correct_section(article, decision: dict) -> None:
    """Feed hints mislabel sections (a 'must' feed tagging NATO news as
    entertainment); the AI classifies from content. Sections derived from
    the CMS (term-ID mapping or URL path) are authoritative."""
    sec = decision.get("section") or ""
    if (sec in ("news", "sport", "entertainment") and sec != article.section
            and (article.raw_json or {}).get("_section_src") not in ("terms", "url")):
        log.info("section corrected for article %s: %s -> %s",
                 article.id, article.section, sec)
        article.section = sec


# Best-practice photo sizes: FB feed shows 4:5 uncropped and it takes the
# most screen space; X/Threads are safest at 1:1.
PHOTO_SIZES = {"facebook_page": (1080, 1350), "instagram": (1080, 1350)}


def photo_base_image(article, idx: int = 0) -> str:
    """Base image for branded renders. When the chosen image is portrait —
    on tv3.lv usually a 'photopost' graphic with its own baked-in headline —
    and the feed also carries a horizontal photo, use the horizontal one:
    the title plate then sits on a clean photo instead of doubling text.
    Toggle: rules.yaml photo_prefer_landscape."""
    images = article.images or []
    if not images:
        return ""
    chosen = images[min(idx, len(images) - 1)]
    if not config.load_rules().get("photo_prefer_landscape", True):
        return chosen
    from app import imageinfo

    if imageinfo.is_portrait(article, chosen):
        alt = imageinfo.landscape_image(article)
        if alt:
            return alt
    return chosen


def unbranded_image(article, idx: int = 0) -> str:
    """Raksta attēls BEZ iecepta virsraksta ('' ja tāda nav).

    Vākiem, kas zīmē savu virsrakstu (lentes, karuseļa vāks), gatava
    photopost grafika neder — teksts uz teksta. Bet atmest attēlu pavisam
    nozīmē plakanu krāsas laukumu, tāpēc vispirms pārmeklējam pārējos
    raksta attēlus.
    """
    base = photo_base_image(article, idx)
    if base and not prebranded(base):
        return base
    for img in article.images or []:
        if img and not prebranded(img):
            return img
    # Plūsmā bieži ir TIKAI photopost grafika, bet lapas metadatos
    # (dr:say:img / twitter:image) mēdz būt īstais foto — sk. pagemeta
    return pagemeta.clean_image(article)


def prebranded(image_url: str) -> bool:
    """True for images that already carry a baked-in headline (photopost
    graphics) — never put the title plate on top of those."""
    patterns = config.load_rules().get("prebranded_image_patterns")
    if patterns is None:
        patterns = ["photopost"]
    return any(p and p in (image_url or "") for p in patterns)


def article_date(article) -> str:
    """dd.mm.yyyy no raksta publicēšanas laika — grafiku datuma čipam."""
    dt = article.published_at or article.first_seen_at
    return dt.strftime("%d.%m.%Y") if dt else ""


def branded_photo(article, image_url: str, platform: str = "") -> str:
    """Photo posts carry the article image with the tv3.lv title plate
    burned in (rules.yaml photo_title_overlay). Falls back to the raw
    image when the renderer is unavailable or fails."""
    from app import cards

    rules = config.load_rules()
    if prebranded(image_url):
        return image_url  # the graphic already has its headline
    if not rules.get("photo_title_overlay", True) or not cards.renderer_available():
        return image_url
    width, height = PHOTO_SIZES.get(platform, (1080, 1080))
    try:
        return cards.render_share_image(article.title, article.section, image_url,
                                        width=width, height=height,
                                        date_txt=article_date(article))
    except Exception as e:  # noqa: BLE001
        log.warning("share image render failed for article %s: %s", article.id, e)
        cards.record_render_failure("photo", e)
        return image_url


def order_channels(channel_decisions: list[dict]) -> list[dict]:
    """Vispirms tie kanāli, kuros top video vai karuselis.

    Stāsts pārizmanto jau uzbūvēto lenti (`article_reel_file`), bet atrast to
    var tikai tad, ja tā jau eksistē. Bez šīs kārtības video stāsts sanāktu
    pēc veiksmes — atkarībā no tā, kādā secībā AI kanālus uzskaitīja.
    """
    return sorted(channel_decisions,
                  key=lambda d: 0 if d.get("format") in ("reel", "card_carousel")
                  else 1)


def article_reel_file(article, rules: dict | None = None) -> str:
    """Šī raksta jau uzbūvētā lente, ko var likt arī stāstā ('' ja nav).

    Stāsts un reels ir viens un tas pats 9:16 formāts, tāpēc otrreiz to
    renderēt nav jēgas — viens fails, divas vietas. Un tieši stāstos ieruna
    nostrādā vislabāk: stāstus skatās ar skaņu, plūsmā lentes bieži sākas
    klusas.

    Priekšroka ierunātai lentei: ja ir abas, stāstā liekam to, kas runā.
    """
    from adapters.base import is_video

    rules = config.load_rules() if rules is None else rules
    if not (rules or {}).get("story_reuses_reel", True):
        return ""

    from app import reels

    candidates = []
    for post in sorted(article.posts or [], key=lambda p: p.id, reverse=True):
        if post.format != "reel" or not post.media:
            continue
        first = str(post.media[0])
        if not is_video(first):
            continue
        if not first.startswith("http") and not Path(first).exists():
            continue    # fails no vecāka ieraksta jau nodzēsts
        candidates.append((reels.has_voice(post), first))
    if not candidates:
        return ""
    # ierunātā priekšā; sarakstā jau ir jaunākais pirmais
    candidates.sort(key=lambda c: not c[0])
    chosen = candidates[0][1]
    if reels.available():
        seconds = reels.media_duration(chosen)
        # Facebook video stāsta griesti ir 60 s; garāku nemēģinām sūtīt
        if seconds > reels.STORY_API_MAX_SECONDS:
            log.info("reel %s too long for a story (%.0fs)", chosen, seconds)
            return ""
    return chosen


def story_media(article, image_url: str) -> list[str]:
    """Vertical story media. An article with a real 9:16 clip becomes a
    VIDEO story (clip + CTA end card); failing that the article's own reel is
    reused (same 9:16 file, and in a story the voice-over is actually heard);
    otherwise the branded story image; falls back to the raw article image;
    empty when nothing visual exists."""
    from app import cards, reels

    video = reels.article_video(article)
    if video and reels.available():
        try:
            return [reels.build_video_reel(video,
                                           max_seconds=reels.STORY_MAX_SECONDS)]
        except Exception as e:  # noqa: BLE001
            log.warning("video story failed for article %s: %s", article.id, e)
            cards.record_render_failure("story", e)
    reused = article_reel_file(article)
    if reused:
        return [reused]
    if cards.renderer_available():
        try:
            # a pre-branded source keeps its own headline; we add only the
            # CTA layer (brand chip + poga + tv3.lv) around it
            return [cards.render_story(article.title, article.section, image_url,
                                       with_title=not prebranded(image_url),
                                       date_txt=article_date(article))]
        except Exception as e:  # noqa: BLE001
            log.warning("story render failed for article %s: %s", article.id, e)
            cards.record_render_failure("story", e)
    return [image_url] if image_url else []


def format_media(article, cfg: dict, fmt: str, idx: int = 0,
                 card_media: list | None = None) -> list[str] | None:
    """Media faili šim formātam. None = šo formātu šim rakstam uzzīmēt nevar.

    Karuselim un reelam grafika jau ir uzbūvēta (resolve_format), pārējiem
    to zīmējam te. Atsevišķa funkcija tāpēc, ka to pašu vajag arī redaktora
    rokas vadībai — tur formātu izvēlas cilvēks, nevis AI.
    """
    images = article.images or []
    if fmt in ("card_carousel", "reel"):
        return list(card_media or [])
    if fmt == "photo" and images:
        return [branded_photo(article, photo_base_image(article, idx),
                              cfg.get("platform", ""))]
    if fmt == "story":
        return story_media(article, images[idx] if idx < len(images)
                           else (images[0] if images else "")) or None
    if fmt == "photo_album":
        return images[:10]
    return []


def clean_sections(raw) -> list[dict]:
    """AI card_sections, novalidētas: [{title, body}, ...] (līdz 5).

    Miglains virsraksts bez teksta vai teksts bez virsraksta kartīti tikai
    bojā — tādas sadaļas izkrīt, un ja pāri paliek mazāk par divām,
    izsaucējs krīt atpakaļ uz punktiem vai citu formātu.
    """
    out = []
    for sec in (raw or [])[:5]:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip().rstrip(".")
        body = str(sec.get("body") or "").strip()
        if not (4 <= len(title) <= 80 and 30 <= len(body) <= 340):
            continue
        out.append({"title": title, "body": body})
    return out


def section_backgrounds(article) -> tuple[list[str], str]:
    """(tīro foto saraksts sadaļu kartītēm, blur rezerves attēls).

    Katra kartīte dabū savu foto no raksta galerijas — kā to dara labākie
    ziņu konti. Photopost grafikas ar iecepto virsrakstu zem baltā paneļa
    neder (teksts zem teksta); tās der tikai kā izpludināta faktūra, kad
    nekā cita nav.
    """
    clean = [img for img in (article.images or []) if img and not prebranded(img)]
    if not clean:
        # plūsmā tīra foto nav — pamēģinām lapas metadatus
        from_meta = pagemeta.clean_image(article)
        if from_meta:
            clean = [from_meta]
    blur = next((img for img in (article.images or [])
                 if img and prebranded(img)), "")
    return clean, ("" if clean else blur)


def sections_voice_text(sections: list[dict]) -> str:
    """Visas nodaļas vienā runas tekstā.

    Lentēs to vairs nelieto — tur katram kadram ir sava ieruna (sk.
    `reels.narration`). Paliek priekšskatījumam un vecajām receptēm, kur
    ieruna bija viens fails pār visu video. Virsraksts te ir iekšā apzināti:
    bez kadra, kas to parāda, teksts bez nodaļu nosaukumiem zaudē dalījumu.
    """
    return " ".join(f"{sec['title']}. {sec['body']}" for sec in sections)


def resolve_format(session, channel: str, cfg: dict, article, ch_dec: dict):
    """(format, media, recipe) for this post. A carousel happens only when the
    AI proposed it AND provided usable card points AND the renderer works;
    otherwise the diversity-aware chooser decides and media is derived from
    the article. The recipe records what the graphic was built from, so an
    editor can redraw it later without cancelling the post."""
    from app import cards

    ai_fmt = ch_dec.get("format")
    if ai_fmt == "card_carousel" and "card_carousel" in (cfg.get("formats") or []):
        sections = clean_sections(ch_dec.get("card_sections"))
        if len(sections) >= 2 and cards.renderer_available():
            tag = "#" + (article.labels[0].upper().replace(" ", "")
                         if article.labels else article.section.upper())
            image = photo_base_image(article)
            cover_title = not prebranded(image)
            bgs, blur = section_backgrounds(article)
            question = (ch_dec.get("card_end_question")
                        or "Uzzini visu stāstu tv3.lv").strip()
            try:
                media = cards.render_section_cards(
                    article.title, article.section, tag, sections, bgs,
                    question, cover_image=image, cover_title=cover_title,
                    blur_image=blur, date_txt=article_date(article))
                return "card_carousel", media, {
                    "kind": "article_cards", "article": article.id,
                    "tag": tag, "sections": sections, "question": question,
                    "section": article.section, "date": article_date(article)}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("section cards failed for article %s: %s",
                            article.id, e)
                cards.record_render_failure("card_carousel", e)
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:4]
        if len(points) >= 2 and cards.renderer_available():
            tag = "#" + (article.labels[0].upper().replace(" ", "")
                         if article.labels else article.section.upper())
            image = photo_base_image(article)
            # a pre-branded graphic becomes the cover as-is (its headline IS
            # the cover); a clean photo gets our title plate on top
            cover_title = not prebranded(image)
            point_bg = next((img for img in (article.images or [])
                             if img and not prebranded(img)), "")
            question = (ch_dec.get("card_end_question")
                        or "Uzzini visu stāstu tv3.lv").strip()
            try:
                media = cards.render_cards(article.title, article.section, tag,
                                           points, image, question,
                                           cover_title=cover_title,
                                           point_bg=point_bg,
                                           date_txt=article_date(article))
                # recepte, lai redaktors grafiku var pārzīmēt vēlāk: AI
                # kartīšu punkti pēc lēmuma citur nekur nepaliek
                return "card_carousel", media, {
                    "kind": "article_cards", "article": article.id,
                    "tag": tag, "points": points, "question": question,
                    "section": article.section, "date": article_date(article)}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("card render failed for article %s: %s", article.id, e)
                cards.record_render_failure("card_carousel", e)
        ai_fmt = None  # fall back to a normal format
    if ai_fmt == "reel" and "reel" in (cfg.get("formats") or []):
        from app import reels

        # Real article clip beats a slideshow every time; the tv3.lv/video
        # 9:16 clips come through the feed as a video URL on the item.
        video = reels.article_video(article)
        if video and reels.available():
            try:
                return "reel", [reels.build_video_reel(video)], {}
            except Exception as e:  # noqa: BLE001
                log.warning("video reel failed for article %s: %s", article.id, e)
                from app import cards as _cards

                _cards.record_render_failure("video_reel", e)
        sections = clean_sections(ch_dec.get("card_sections"))[:3]
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:3]
        # Lentes vāks zīmē savu virsrakstu, tāpēc gatava photopost grafika
        # tam neder — bet tas nenozīmē, ka jāpaliek BEZ foto. Meklējam raksta
        # tīro attēlu; tukšu vāku ar plakanu krāsu atstājam tikai tad, ja
        # neviena cita attēla nav.
        image = unbranded_image(article)
        if (len(sections) >= 2 or len(points) >= 2) and reels.available():
            # Ieruna vairs nav viens gabals pār visu lenti: katram kadram ir
            # sava rinda, un kadrs ir tieši tik garš, cik tā runa. Vāks saka
            # virsrakstu, nodaļas — savu tekstu (NE virsrakstu, tas jau ir
            # ekrānā), beigu kadrs īsu aicinājumu uz portālu.
            # Vāka ieruna ir TIKAI virsraksts. AI rakstītais āķis te bija
            # gan garš, gan saturiski tas pats, ko pirmā nodaļa — divas
            # reizes viena doma, pirms stāsts vispār sācies.
            cover_voice = reels.spoken_line(article.title)
            end_voice = reels.end_voice_text()
            bgs, blur = section_backgrounds(article)
            report: dict = {}
            try:
                media = reels.build_reel(article.title, article.section,
                                         image, points, sections=sections,
                                         point_images=bgs, blur_image=blur,
                                         cover_voice=cover_voice,
                                         end_voice=end_voice, report=report)
                return "reel", [media], {
                    "kind": "article_reel", "article": article.id,
                    "points": points, "sections": sections, "image": image,
                    "blur_image": blur,
                    "voice_script": " ".join(report.get("narration") or []),
                    "cover_voice": cover_voice, "end_voice": end_voice,
                    # vai lentē TIEŠĀM ir balss: scenārijs receptē var būt arī
                    # tad, kad sintēze neizdevās, un statistikā tie ir divi
                    # dažādi ieraksti
                    "voiced": bool(report.get("voiced")),
                    "seconds": report.get("seconds"),
                    "section": article.section, "date": article_date(article)}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("reel build failed for article %s: %s", article.id, e)
        ai_fmt = None
    fmt = choose_format(session, channel, cfg, article, ai_fmt)
    # Saites ierakstā attēlu izvēlas NEVIS mēs: Facebook to paņem no raksta
    # og:image un apgriež savas kartītes 1.91:1 rāmī. Jo attēls šaurāks, jo
    # vairāk augstuma pazūd (3:2 -> 21%, 4:3 -> 30%, kvadrāts -> 48%), un
    # ziņu kadrā galvas ir augšējā trešdaļā — tāpēc tieši tās nogriež.
    #
    # Pārslēdzot uz photo, attēlu zīmējam mēs: mūsu rāmji ir šaurāki par foto,
    # tāpēc `cover` griež SĀNUS un augstums paliek vesels. Cena ir saites
    # kartīte, tāpēc slieksnis ir maināms un grīda `format_mix` paliek spēkā —
    # tieši šī pārslēgšana kādreiz klusi pārvērta visu plūsmu par foto
    # ierakstiem.
    if fmt == "link" and link_card_hurts(session, channel, cfg, article)[0]:
        fmt = "photo"
    return fmt, [], {}


def link_card_hurts(session, channel: str, cfg: dict, article,
                    rules: dict | None = None) -> tuple[bool, float]:
    """(vai pārslēgt uz photo, cik daudz kartīte nogrieztu).

    Saites ierakstā attēlu izvēlas NEVIS mēs: Facebook to paņem no raksta
    og:image un ieliek savas kartītes 1.91:1 rāmī. Jo attēls šaurāks, jo
    vairāk augstuma pazūd (3:2 -> 21%, 4:3 -> 30%, kvadrāts -> 48%), un ziņu
    kadrā galvas ir augšējā trešdaļā — tāpēc tieši tās nogriež.

    Pārslēdzot uz photo, attēlu zīmējam mēs: mūsu rāmji ir šaurāki par foto,
    tāpēc `cover` griež SĀNUS un augstums paliek vesels. Cena ir saites
    kartīte, tāpēc slieksnis ir maināms un `format_mix` grīda paliek spēkā —
    tieši šī pārslēgšana kādreiz klusi pārvērta visu plūsmu par foto
    ierakstiem.
    """
    rules = config.load_rules() if rules is None else rules
    if not (article is not None and (article.images or [])
            and "photo" in (cfg.get("formats") or [])
            and rules.get("portrait_link_to_photo", True)):
        return False, 0.0
    from app import imageinfo

    loss = imageinfo.link_card_crop(article, photo_base_image(article))
    portrait = imageinfo.orientation(article) == "portrait"
    if not (portrait or loss > rules.get("link_card_max_crop", 0.20)):
        return False, loss
    # Saites postiem ir sava grīda (`format_mix`), un parasti tā ir svarīgāka
    # par vienu apgriezumu. BET grīdas jēga ir turēt plūsmā strādājošus saites
    # ierakstus, un šis tāds nav: pie portreta attēla vai puses nogrieztā
    # augstuma kartīte ir sabojāta neatkarīgi no kvotas. Piespiest to tur
    # nozīmē uztaisīt sliktu ierakstu UN iemācīt svariem, ka saites posti
    # nestrādā. Trūkstošo kvotu aizpildīs nākamais raksts ar derīgu attēlu.
    if portrait or loss >= rules.get("link_card_force_crop", 0.40):
        return True, loss
    if mix_deficit(recent_format_shares(session, channel),
                   cfg.get("format_mix") or {}, ["link"]):
        return False, loss
    return True, loss


def retarget_queued_link_post(session, post, cfg: dict) -> bool:
    """Pārslēdz jau ieplānotu saites ierakstu uz photo, ja kartīte to sabojātu.

    Ieraksts rindā var nostāvēt stundas. Bez šī labojums aizsniegtu tikai tos
    rakstus, par kuriem lēmums pieņemts PĒC izvietošanas, un redaktoram tie,
    kas jau gaida, būtu jāatceļ ar roku — to pašu problēmu `refresh_missing_media`
    jau risina grafikām.
    """
    if post.format != "link" or post.article is None:
        return False
    hurts, loss = link_card_hurts(session, post.channel, cfg, post.article)
    if not hurts:
        return False
    post.format = "photo"
    post.extra = {**(post.extra or {}),
                  "retargeted": {"from": "link", "link_card_crop": round(loss, 3)}}
    session.commit()
    log.info("post %s link -> photo rindā: FB kartīte nogrieztu %.0f%% augstuma",
             post.id, loss * 100)
    return True


def repost_offset(article, cfg: dict, existing: list) -> datetime | None:
    """When a second post for this article may go out on the channel, or None.

    Deliberate duplication (the competitor pattern: photo first, link post an
    hour later) is reserved for content the AI rated strongly, capped at one
    extra post, and only where the channel configures repost_after_minutes.
    """
    minutes = int(cfg.get("repost_after_minutes") or 0)
    if not minutes or len(existing) != 1:
        return None
    rules = config.load_rules()
    if float(article.ai_score or 0) < float(rules.get("repost_min_score", 0.75)):
        return None
    first = existing[0].scheduled_at
    return first + timedelta(minutes=minutes) if first else None


def compose_text(post, platform: str, shown_link: str,
                 rules: dict | None = None) -> tuple[str, bool]:
    """(post text, whether the link also goes out as the first comment).

    On FB/IG image posts the link goes into the first comment — the
    SocialFlow tactic. It ALSO stays in the caption (rules.yaml
    link_in_caption): one tap for the reader either way, and a caption that
    carries the destination is what Facebook can amplify as a traffic ad.
    Instagram drops it from the caption on its own (links aren't clickable
    there), so only the comment carries it.
    """
    rules = config.load_rules() if rules is None else rules
    in_comment = bool(
        shown_link and platform in ("facebook_page", "instagram")
        and post.format in ("photo", "photo_album", "card_carousel", "reel")
        and rules.get("link_in_first_comment", True))
    in_caption = rules.get("link_in_caption", True) or not in_comment
    # ES MI akta 50. panta atruna: parakstu, birkas un sadaļu tekstus raksta
    # mākslīgais intelekts, tāpēc katrs ieraksts to pasaka. Ja AI teksts to
    # jau ir pateicis pats, otrreiz nepieliekam.
    note = disclosure.caption_line(platform, rules)
    if note and disclosure.in_caption(post.copy or "", rules):
        note = ""
    text = assemble_post_text(post.copy, post.hashtags or [],
                              shown_link if in_caption else "", platform,
                              disclosure=note)
    return text, in_comment


def refresh_missing_media(session, post, platform: str) -> None:
    """Re-render photo/story media just before publishing when needed:
    the rendered file was wiped (deploy without the volume), the stored
    media is the raw article URL because rendering failed at decision time,
    or the graphic was drawn with an older layout version — a post can sit
    in the queue for hours, and without this a design fix would reach only
    the posts decided after it, not the ones already waiting."""
    from pathlib import Path

    from app import cards

    if post.article is None or post.format not in ("photo", "story"):
        # card_carousel and reel can't be regenerated here (the AI's card
        # points aren't stored on the post) — the adapter fails with a clear
        # message and the editor can cancel or let the article be re-decided
        return
    article = post.article
    if (article.raw_json or {}).get("_digest"):
        # franšīžu grafikas (foto mozaīka u.c.) būvē app.weekend no vairākiem
        # rakstiem — šeit tās pārzīmēt nevar, un parasts stāsta renders tās
        # aizstātu ar tukšu krāsas laukumu
        return
    media = post.media or []
    current = str(media[0]) if media else ""
    local_gone = bool(current) and not current.startswith("http") and not Path(current).exists()
    raw_fallback = current.startswith("http")
    stale_layout = (post.extra or {}).get("render_version") != cards.RENDER_VERSION
    if not (local_gone or raw_fallback or not media or stale_layout):
        return
    if post.format == "photo":
        image = photo_base_image(article)
        new = [branded_photo(article, image, platform)] if image else []
    else:
        image = (article.images or [""])[0]
        new = story_media(article, image)
    if not new:
        return
    if new != media:
        post.media = new
    post.extra = {**(post.extra or {}), "render_version": cards.RENDER_VERSION}
    session.commit()


def publish_due(session) -> int:
    """Publish posts whose time has come. Cancels posts whose article turned 'dont'."""
    now = utcnow()
    due = session.execute(
        select(Post).where(Post.state == "scheduled", Post.scheduled_at <= now)
        .order_by(Post.scheduled_at)
    ).scalars().all()

    published = 0
    channels_cfg = config.load_channels()
    for post in due:
        if post.article and post.article.editor_status == "dont":
            post.state = "cancelled"
            post.error = "editor set status to dont"
            session.commit()
            continue
        if paused(session, post.channel):
            continue  # stays scheduled; resumes when unpaused

        cfg = channels_cfg.get(post.channel) or {}
        platform = cfg.get("platform", "")
        # rindā gaidošs saites ieraksts, kura attēlu kartīte sagrieztu, vēl
        # paspēj kļūt par foto ierakstu — grafiku uzzīmē refresh_missing_media
        retarget_queued_link_post(session, post, cfg)
        refresh_missing_media(session, post, platform)
        post.state = "publishing"
        post.attempts += 1
        session.commit()
        try:
            rules = config.load_rules()
            link = (add_utm(post.link_url, platform, post.id, hook=post.hook_type or "")
                    if post.link_url else "")
            # what a reader sees: the tv3.lv short link when one is configured
            # (the full tracked URL still goes to the API as the link target,
            # where only the domain is ever displayed)
            shown = shortlinks.display_link(post.id, link, rules, post.article)
            text, first_comment_link = compose_text(post, platform, shown, rules)
            adapter = get_adapter(platform)
            raw_card_links = (post.extra or {}).get("card_links") or []
            if not raw_card_links and post.format == "card_carousel":
                # viena raksta karuselim visas kartītes ved uz to pašu rakstu,
                # bet KATRA ar savu utm_term — tikai tā var pateikt, kura
                # kartīte īsti nopelnīja klikšķi
                raw_card_links = [post.link_url] * len(post.media or [])
            # utm_term nes gan franšīzi, gan kartītes numuru («quiz-karte2»),
            # citādi GA4 visu karuseļu otrās kartītes saplūst vienā rindā
            hook_base = (post.hook_type or "").strip()
            card_links = [
                add_utm(u, platform, post.id,
                        hook=f"{hook_base}-karte{i + 1}" if hook_base
                        else f"karte{i + 1}")
                if u else "" for i, u in enumerate(raw_card_links)]
            card_titles = (post.extra or {}).get("card_titles") or []
            if (not card_titles and post.format == "card_carousel"
                    and post.article is not None):
                # parastam karuselim FB teksta josla zem katras kartītes rāda
                # raksta virsrakstu — tukša josla izskatās pēc kļūdas; pēdējā
                # (CTA) kartīte tā vietā sauc uz rakstu
                n = len(post.media or [])
                if n >= 2:
                    card_titles = ([post.article.title] * (n - 1)
                                   + ["Lasi visu rakstā — tv3.lv"])
                else:
                    card_titles = [post.article.title] * n
            extra_kwargs = {}
            if card_links:
                extra_kwargs["card_links"] = card_links
            if card_titles:
                extra_kwargs["card_titles"] = card_titles
            # attēla apraksts ekrānlasītājiem: mūsu grafikas nes virsrakstu,
            # tāpēc godīgākais apraksts ir tas, kas uz tās tiešām rakstīts
            if post.article is not None and post.media:
                extra_kwargs["alt_text"] = alt_text(
                    post.article.title, post.article.section,
                    pagemeta.author(post.article))
            post.platform_post_id = adapter.publish(
                text=text, link=link, images=post.media or [], fmt=post.format,
                **extra_kwargs)
            post.state = "published"
            post.published_at = utcnow()
            post.error = ""
            published += 1
            if first_comment_link and post.platform_post_id:
                try:
                    adapter.comment(post.platform_post_id, shown)
                except Exception as e:  # noqa: BLE001 — post stands even if it fails
                    log.warning("first-comment link failed for post %s: %s", post.id, e)
                    post.error = f"comment failed: {e}"
        except PublishError as e:
            if e.retryable and post.attempts < MAX_ATTEMPTS:
                post.state = "scheduled"
                post.scheduled_at = utcnow() + timedelta(minutes=5 * post.attempts)
                post.error = f"retry {post.attempts}: {e}"
            else:
                post.state = "failed"
                post.error = str(e)
                alert(f"Post {post.id} -> {post.channel} failed: {e}")
        except Exception as e:  # noqa: BLE001
            post.state = "failed"
            post.error = f"unexpected: {e}"
            alert(f"Post {post.id} -> {post.channel} crashed: {e}")
        session.commit()
    return published


def alert(message: str) -> None:
    log.error("ALERT: %s", message)
    if config.SLACK_WEBHOOK_URL:
        try:
            import httpx

            httpx.post(config.SLACK_WEBHOOK_URL,
                       json={"text": f"TV3 Autopilot: {message}"}, timeout=10)
        except Exception:  # noqa: BLE001
            log.exception("slack alert failed")


def collect_metrics(session) -> int:
    """Pull platform insights for recently published posts (1h..72h old)."""
    now = utcnow()
    rows = session.execute(
        select(Post).where(Post.state == "published",
                           Post.published_at >= now - timedelta(hours=72),
                           Post.dry_run.is_(False))
    ).scalars().all()
    channels_cfg = config.load_channels()
    collected = 0
    for post in rows:
        platform = (channels_cfg.get(post.channel) or {}).get("platform", "")
        adapter = get_adapter(platform)
        data = adapter.fetch_insights(post.platform_post_id)
        if data:
            from app.models import PostMetrics

            session.add(PostMetrics(post_id=post.id, **data))
            collected += 1
    session.commit()

    from app import ga4

    collected += ga4.collect(session)
    return collected


def weekly_report(session) -> str:
    """Human summary of the last 30 days, sent to Slack/log weekly."""
    from app import priors

    lines = ["TV3 Autopilot — nedēļas kopsavilkums (pēdējās 30 dienas):"]
    for channel in config.load_channels():
        s = priors.channel_summary(session, channel)
        if not s["posts"]:
            continue
        fmt = ", ".join(f"{f['format']}: {f['avg']:.0f} (n={f['n']})"
                        for f in s["formats"][:4])
        hours = (", stiprākās stundas " + ", ".join(f"{h}:00" for h in s["best_hours"])
                 if s["best_hours"] else "")
        lines.append(f"• {channel}: {s['posts']} ieraksti, {s['sessions']} GA sesijas, "
                     f"{s['clicks']} klikšķi. Formāti: {fmt}{hours}")
    top = priors.top_posts(session, 5)
    if top:
        lines.append("Top ieraksti:")
        for r in top:
            title = r["post"].article.title[:70] if r["post"].article else ""
            lines.append(f"  {r['score']:.0f} — [{r['channel']}/{r['format']}] {title}")
    report = "\n".join(lines)
    alert(report) if len(lines) > 1 else None
    return report
