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
from app import config, credentials, disclosure, pagemeta, shortlinks, tts, videos
from app.best_practices import (PLATFORM_SPECS, add_utm, alt_text, assemble_post_text,
                                sanitize_copy)
from app.decide import decide
from app.formats import choose_format, mix_deficit, recent_format_shares
from app.models import Article, Evaluation, Post, get_setting, utcnow
from app.rules_engine import evaluate_all
from app import rules_engine
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
    touched: set[str] = set()
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

            if videos.is_video_item(article):
                # arhīva klips: kanālam vajag reel/story, un klipi nedrīkst
                # aizņemt ziņu vietu — savs dienas limits
                if not videos.channel_formats(cfg):
                    session.add(Evaluation(article_id=article.id, channel=channel,
                                           outcome="blocked",
                                           reason="video arhīvs: kanālā nav reel/story formāta"))
                    continue
                if videos.over_daily_cap(session, channel):
                    session.add(Evaluation(article_id=article.id, channel=channel,
                                           outcome="blocked",
                                           reason="video arhīvs: dienas limits kanālā"))
                    continue
            format_notes: list[str] = []
            format_trace: dict = {}
            fmt, card_media, recipe = resolve_format(session, channel, cfg,
                                                     article, ch_dec,
                                                     notes=format_notes,
                                                     trace=format_trace)
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
            age_hours = rules_engine.article_age_hours(article, now)
            slot, why = plan_slot(session, channel, cfg, verdict,
                                  article.section, fmt, article.title, now, preferred,
                                  score=score, allow_similar=bool(existing),
                                  age_hours=age_hours)
            late = False
            if slot is None and verdict.latest is not None:
                # Pilna rinda statusa logā: vēlāks slots ir labāks par
                # atmestu saturu. Atmetam TIKAI statusa termiņu — svaiguma
                # griesti (`fresh_until`) paliek, citādi šis ceļš ziņu
                # aizsūtītu divas dienas uz priekšu.
                import dataclasses

                slot, why = plan_slot(session, channel, cfg,
                                      dataclasses.replace(verdict, latest=None),
                                      article.section, fmt, article.title, now,
                                      preferred, score=score,
                                      allow_similar=bool(existing),
                                      age_hours=age_hours)
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
                # lente/stāsts no tv3.lv/video klipa ved uz konkrēto video lapu
                link_url=videos.link_for(article, fmt),
                scheduled_at=slot, state="scheduled", dry_run=runtime.is_dry_run(session),
                # ar kādu grafiku izkārtojumu šis attēls uzzīmēts: ieraksts var
                # nostāvēt rindā stundas, un dizaina labojums citādi to vairs
                # neskartu (sk. refresh_missing_media)
                extra=(({"render_version": cards_mod.RENDER_VERSION}
                        | ({"recipe": recipe} if recipe else {}))
                       if media else {})
                      | ({"format_notes": format_notes} if format_notes else {})
                      # pārplānošanai: statusa termiņš, otrā viļņa «ne agrāk»
                      # un tas, ka `now` ierakstu nedrīkst pārvietot
                      | ({"latest": verdict.latest.isoformat()} if verdict.latest
                         and verdict.outcome != "forced_now" else {})
                      | ({"not_before": repost_at.isoformat()} if existing and repost_at
                         else {})
                      | ({"forced_now": True} if verdict.outcome == "forced_now" else {})
                      | ({"format_trace": {k: format_trace[k]
                                           for k in ("chosen", "decision", "blocked",
                                                     "shares", "run", "ai_choice")
                                           if k in format_trace}}
                         if format_trace else {}),
            )
            session.add(post)
            session.flush()
            if fmt == "reel":
                # iepriekšējos viļņos ieplānotie stāsti pārņem jauno lenti
                upgrade_pending_stories(session, article)
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome="posted",
                                   reason=f"scheduled {slot:%Y-%m-%d %H:%M} UTC as {fmt}"
                                          + (" (otrais vilnis)" if existing else "")
                                          + (" (vēlāk — rinda bija pilna)" if late else "")
                                          + (f" (fixes: {', '.join(fixes)})" if fixes else "")
                                          + (f" (formāts: {'; '.join(format_notes)})"
                                             if format_notes else "")))
            created += 1
            scheduled_here += 1
            touched.add(channel)
        if scheduled_here == 0:
            requeue_for_retry(article, now)
        session.commit()
    # Rinda pēc vērtības un svaiguma, ne ienākšanas kārtas: katrs vilnis
    # pārkārto skartos kanālus (sk. slots.replan_channel)
    from app.slots import replan_channel

    for channel in sorted(touched):
        try:
            outcome = replan_channel(session, channel, channels_cfg.get(channel) or {}, now)
            if outcome["moved"] or outcome["cancelled"]:
                log.info("replan %s: %s", channel, outcome)
        except Exception as e:  # noqa: BLE001 — pārplānošana nedrīkst gāzt vilni
            log.warning("replan %s failed: %s", channel, e)
            session.rollback()
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

    # ne tikai portrets: arī kvadrātiska photopost grafika 1.15:1 laukā tiek
    # griezta pa vertikāli, un tad pirmās pazūd galvas — plats tīrs foto ar
    # mūsu plāksni ir labāks vāks
    if imageinfo.is_wide(article, chosen) is False:
        alt = imageinfo.wide_image(article)
        if alt:
            return alt
    return chosen


def cover_fit_for(article, image: str) -> str:
    """Kā gatavu (photopost) grafiku likt vākā: 'cover' tikai tad, ja tā ir
    plata un kadrs griež vienīgi sānus; kvadrātisku, augstu vai nezināma
    izmēra grafiku rāda veselu ('contain') uz izpludināta fona. Tīram foto
    ar mūsu plāksni vienmēr 'cover' (enkurs augšējā trešdaļā ir šablonā)."""
    if not image or not prebranded(image):
        return "cover"
    from app import imageinfo

    return "cover" if imageinfo.is_wide(article, image) else "contain"


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

    from sqlalchemy.orm import object_session

    from app import reels

    # Vaicājums, ne `article.posts`: kolekcija, kas ielādēta pirms lentes
    # ieraksta (piem., rokas režīmā, kur copy meklē iepriekšējos ierakstus),
    # jaunpievienoto lenti neredz, jo sesija ielādētās kolekcijas neatsvaidzina.
    sess = object_session(article)
    if sess is not None:
        posts = sess.execute(
            select(Post).where(Post.article_id == article.id, Post.format == "reel")
            .order_by(Post.id.desc())).scalars().all()
    else:
        posts = sorted(article.posts or [], key=lambda p: p.id, reverse=True)
    candidates = []
    for post in posts:
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


def upgrade_pending_stories(session, article, rules: dict | None = None) -> int:
    """Raksta vēl nepublicētie stāsti pārņem lenti, kas tapusi pēc tiem.

    Stāsta mediju fiksē lēmuma brīdī. Ja lente rakstam parādās vēlāk — cits
    vilnis, cits kanāls vai redaktora «Uztaisīt» —, ieplānotais stāsts
    citādi aizietu ar statisko attēlu, kaut tieši stāstā ierunātā lente
    strādā vislabāk. Atgriež, cik stāstu pārņēma lenti.
    """
    from adapters.base import is_video

    from app import cards

    reel = article_reel_file(article, rules)
    if not reel:
        return 0
    pending = session.execute(
        select(Post).where(Post.article_id == article.id, Post.format == "story",
                           Post.state.in_(("proposed", "scheduled")))
    ).scalars().all()
    upgraded = 0
    for post in pending:
        current = str((post.media or [""])[0] or "")
        if is_video(current):
            continue
        if (post.article or article).raw_json and (post.article or article).raw_json.get("_digest"):
            continue    # franšīžu grafika nav šī raksta stāsts
        post.media = [reel]
        post.extra = {**(post.extra or {}), "render_version": cards.RENDER_VERSION,
                      "story_from_reel": True}
        upgraded += 1
        log.info("story post %s takes over reel %s", post.id, reel)
    return upgraded


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


def built_media(session, article, fmt: str,
                rules: dict | None = None) -> tuple[list[str], dict] | None:
    """Cita kanāla jau uzbūvētā lente vai karuselis šim rakstam (None ja nav).

    Lente un karuselis nav kanāla, bet RAKSTA grafika: vāks, nodaļas un
    beigu aicinājums «tv3.lv» ir vienādi, vai to rāda Facebook, Instagram,
    Threads vai X. Renderēt un ierunāt to otrreiz nozīmē tērēt TTS un
    minūti procesora tam pašam failam — un riskēt, ka divi kanāli rāda divas
    nedaudz atšķirīgas versijas. Viens fails, visi kanāli; tāpēc arī
    `order_channels` liek lentes un karuseļu kanālus pirmos.

    Ņem jaunāko ierakstu ar to pašu formātu, kura faili vēl ir uz diska
    (konteinerā tie mūžīgi neglabājas); recepte nāk līdzi, lai arī otrs
    ieraksts ir pārzīmējams un statistikā (ierunāts / kluss) skaitās pareizi.
    """
    if fmt not in ("reel", "card_carousel"):
        return None
    rules = config.load_rules() if rules is None else rules
    if not (rules or {}).get("share_built_media", True):
        return None
    posts = session.execute(
        select(Post).where(Post.article_id == article.id, Post.format == fmt)
        .order_by(Post.id.desc())).scalars().all()
    for post in posts:
        media = [str(m) for m in (post.media or []) if m]
        if not media:
            continue
        if all(m.startswith("http") or Path(m).exists() for m in media):
            recipe = dict((post.extra or {}).get("recipe") or {})
            log.info("article %s: %s reuses %s media from post %s",
                     article.id, fmt, post.channel, post.id)
            return media, recipe
    return None


# Dienas griesti «bagātajiem» formātiem. Rotāciju dara `format_max_share`
# (35 % no pēdējiem sešiem) un atkārtojuma sargs — šī kvota ir tikai galējais
# drošinātājs. Mērogs ir svarīgs: kanālā iziet ~30 ierakstu dienā, tāpēc
# kvota 2 nozīmēja, ka pēc diviem karuseļiem formāts uz visu atlikušo dienu
# pazūd — tieši tas plūsmu pārvērta par foto rindu. 35 % no 30 ierakstiem ir
# ~10, tāpēc kvota 8 rotāciju netraucē un tur atpakaļ tikai īstu izbēgšanu.
# Lentei kvota ir arī naudas jautājums: katra maksā ElevenLabs rakstzīmes.
DEFAULT_FORMAT_DAILY_CAP = {"card_carousel": 8, "reel": 4}
RICH_FORMATS = ("card_carousel", "reel")


def format_daily_cap(cfg: dict, fmt: str) -> int | None:
    caps = {**DEFAULT_FORMAT_DAILY_CAP, **(cfg.get("format_daily_cap") or {})}
    cap = caps.get(fmt)
    return int(cap) if cap is not None else None


def posts_today(session, channel: str, fmt: str, now=None) -> int:
    """Cik šī formāta ierakstu kanālā jau ir šodienas (Rīgas) datumā —
    ieplānotie skaitās tāpat kā publicētie."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.TIMEZONE)
    now = now or utcnow()
    today = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
    rows = session.execute(
        select(Post).where(Post.channel == channel, Post.format == fmt,
                           Post.state.in_(("scheduled", "publishing", "published")))
    ).scalars().all()
    count = 0
    for p in rows:
        when = p.scheduled_at or p.published_at or p.created_at
        if when and when.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date() == today:
            count += 1
    return count


def rich_format_gate(session, channel: str, cfg: dict, article, fmt: str,
                     now=None) -> str:
    """Kāpēc AI piedāvāto karuseli/lenti šodien šajā kanālā NEtaisīt ('' =
    drīkst). Sargi, kas līdz šim uz šiem formātiem neattiecās:

    * vienveidība — viens un tas pats formāts pēc kārtas vai virs saviem
      daļas griestiem (`max_same_format_in_row`, `format_max_share`);
    * saites grīda (`format_mix`) — saite ir klikšķu formāts ar CTA
      kartīti, un tai jāpaliek plūsmā arī tad, kad AI katram rakstam redz
      karuseli;
    * dienas kvota (`format_daily_cap`) — pēdējā, vājākā robeža.

    Maksas puse te nav sargs, bet svars: boostot var visus trīs formātus
    (karuseļa kartītes ir saites, foto ierakstam saite ir aprakstā un
    komentārā), tāpēc kurš no tiem par eiro dod vairāk sesiju, izlemj
    izmērītie dati `formats.ad_multipliers`, ne noteikums.
    """
    from app.formats import monotony_reason

    formats = cfg.get("formats") or []
    has_link = "link" in formats and bool(article.canonical_url or article.url)
    why = monotony_reason(session, channel, cfg, fmt)
    if why:
        return why
    # Saites grīda karuseli atceļ tikai tad, ja šim rakstam saite VISPĀR ir
    # iespējama: ja kartīte ir sabojāta (portreta og:image) un saite tāpat
    # kļūtu par foto, karuseļa atcelšana grīdu nepilda — tā tikai dod vēl
    # vienu foto. Tad karuselis ir tieši tā dažādība, ko plūsma prasa.
    if has_link and not link_card_broken(session, channel, cfg, article)[0]:
        shares = recent_format_shares(session, channel)
        # bez vēstures grīda nav «neizpildīta» — tukšā kanālā pirmā lente drīkst būt
        if shares and mix_deficit(shares, cfg.get("format_mix") or {}, ["link"]):
            return "saites grīda (format_mix) pēdējos ierakstos nav izpildīta"
    cap = format_daily_cap(cfg, fmt)
    if cap is not None:
        used = posts_today(session, channel, fmt, now)
        if used >= cap:
            return f"dienas kvota {used}/{cap} jau izpildīta"
    return ""


def resolve_format(session, channel: str, cfg: dict, article, ch_dec: dict,
                   notes: list[str] | None = None, enforce: bool = True,
                   trace: dict | None = None):
    """(format, media, recipe) for this post. A carousel happens only when the
    AI proposed it AND provided usable card points AND the renderer works
    AND the channel's daily quota / link floor allow it; otherwise the
    diversity-aware chooser decides and media is derived from the article.
    The recipe records what the graphic was built from, so an editor can
    redraw it later without cancelling the post. `notes` savāc, kāpēc AI
    formāts netika izpildīts — tas nonāk vērtējumā un ierakstā, lai lapā
    «Kāpēc» redz, ka bija domāta lente. `enforce=False` (rokas režīms)
    kvotu un grīdu neskata."""
    from app import cards

    if notes is None:
        notes = []
    if videos.is_video_item(article):
        return resolve_video_format(session, channel, cfg, article, notes, trace)
    ai_fmt = ch_dec.get("format")
    if ai_fmt in RICH_FORMATS and ai_fmt in (cfg.get("formats") or []) and enforce:
        why = rich_format_gate(session, channel, cfg, article, ai_fmt)
        if why:
            notes.append(f"{ai_fmt} → cits formāts: {why}")
            ai_fmt = None
    if ai_fmt in RICH_FORMATS and ai_fmt in (cfg.get("formats") or []):
        # tas pats raksts, tas pats formāts, cits kanāls — grafika jau ir
        ready = built_media(session, article, ai_fmt)
        if ready is not None:
            return ai_fmt, ready[0], ready[1]
    if ai_fmt == "card_carousel" and "card_carousel" in (cfg.get("formats") or []):
        sections = clean_sections(ch_dec.get("card_sections"))
        if not cards.renderer_available():
            notes.append("card_carousel → cits formāts: attēlu renderētājs nav pieejams")
        elif len(sections) < 2 and len([p for p in (ch_dec.get("card_points") or [])
                                        if isinstance(p, str) and p.strip()]) < 2:
            notes.append("card_carousel → cits formāts: AI nedeva vismaz 2 derīgas sadaļas")
        if len(sections) >= 2 and cards.renderer_available():
            tag = "#" + (article.labels[0].upper().replace(" ", "")
                         if article.labels else article.section.upper())
            image = photo_base_image(article)
            cover_title = not prebranded(image)
            bgs, blur = section_backgrounds(article)
            question = (ch_dec.get("card_end_question")
                        or "Uzzini visu stāstu tv3.lv").strip()
            try:
                fit = cover_fit_for(article, image)
                media = cards.render_section_cards(
                    article.title, article.section, tag, sections, bgs,
                    question, cover_image=image, cover_title=cover_title,
                    blur_image=blur, date_txt=article_date(article),
                    ai_note=disclosure.applies("card_carousel"), cover_fit=fit)
                return "card_carousel", media, {
                    "kind": "article_cards", "article": article.id,
                    "tag": tag, "sections": sections, "question": question,
                    "section": article.section, "date": article_date(article),
                    "cover_fit": fit}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("section cards failed for article %s: %s",
                            article.id, e)
                cards.record_render_failure("card_carousel", e)
                notes.append(f"card_carousel → cits formāts: renderēšana neizdevās ({str(e)[:80]})")
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
                                           date_txt=article_date(article),
                                           cover_fit=cover_fit_for(article, image))
                # recepte, lai redaktors grafiku var pārzīmēt vēlāk: AI
                # kartīšu punkti pēc lēmuma citur nekur nepaliek
                return "card_carousel", media, {
                    "kind": "article_cards", "article": article.id,
                    "tag": tag, "points": points, "question": question,
                    "section": article.section, "date": article_date(article)}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("card render failed for article %s: %s", article.id, e)
                cards.record_render_failure("card_carousel", e)
                notes.append(f"card_carousel → cits formāts: renderēšana neizdevās ({str(e)[:80]})")
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
        if not reels.available():
            notes.append("reel → cits formāts: ffmpeg vai attēlu renderētājs nav pieejams")
        elif len(sections) < 2 and len(points) < 2:
            notes.append("reel → cits formāts: AI nedeva vismaz 2 derīgas sadaļas")
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
                    "speech_seconds": report.get("speech_seconds"),
                    # kura balss un temps tika lietots — sadaļas noteikums
                    # var būt izkomentēts, un tad lente skan ar kopīgo
                    "voice_used": report.get("voice_used"),
                    "voice_rate": report.get("voice_rate"),
                    "voice_provider": report.get("voice_provider"),
                    "voice_by_section": report.get("voice_by_section"),
                    "rate_by_section": report.get("rate_by_section"),
                    "section": article.section, "date": article_date(article)}
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("reel build failed for article %s: %s", article.id, e)
                cards.record_render_failure("reel", e)
                notes.append(f"reel → cits formāts: lentes būve neizdevās ({str(e)[:80]})")
        ai_fmt = None
    from app.formats import explain

    picked = explain(session, channel, cfg, article, ai_fmt)
    log.info("format %s: %s (%s; bloķēti %s)", channel, picked["chosen"],
             picked.get("decision", ""), picked.get("blocked") or "-")
    if trace is not None:
        trace.update(picked)
    fmt = picked["chosen"]
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


def resolve_video_format(session, channel: str, cfg: dict, article,
                         notes: list[str], trace: dict | None = None):
    """Arhīva klipa formāts: reel no paša klipa, kur kanāls to nes; citādi
    story (arī no klipa — sk. story_media). Kvotu un AI izvēles te nav: klips
    ir viens formāts pēc būtības, un tā vietu plūsmā ierobežo
    `video_archive.daily_cap`, ne formātu mikss."""
    from app import cards, reels

    allowed = videos.channel_formats(cfg)
    video = reels.article_video(article)
    picked = {"allowed": allowed, "chosen": "", "decision": ""}
    if "reel" in allowed and video and reels.available():
        try:
            media = [reels.build_video_reel(video)]
            picked.update(chosen="reel", decision="tv3.lv/video klips -> reel no paša klipa")
            if trace is not None:
                trace.update(picked)
            return "reel", media, {"kind": "video_clip", "video": videos.video_page(article)}
        except Exception as e:  # noqa: BLE001
            log.warning("video reel failed for article %s: %s", article.id, e)
            cards.record_render_failure("video_reel", e)
            notes.append(f"reel → story: klipa lente neizdevās ({str(e)[:80]})")
    if "story" in allowed:
        picked.update(chosen="story", decision="tv3.lv/video klips -> story no klipa")
        if trace is not None:
            trace.update(picked)
        return "story", [], {"kind": "video_clip", "video": videos.video_page(article)}
    picked.update(chosen=allowed[0] if allowed else "reel",
                  decision="kanāls klipam nav piemērots")
    if trace is not None:
        trace.update(picked)
    return picked["chosen"], [], {}


def link_card_hurts(session, channel: str, cfg: dict, article,
                    rules: dict | None = None) -> tuple[bool, float]:
    """(vai pārslēgt uz photo TAGAD, cik daudz kartīte nogrieztu).

    Vienveidības sargs ir pārāks par kartītes kvalitāti: ja plūsmas galā jau
    ir divi foto pēc kārtas vai foto pārsniedz savus griestus, arī nogriezta
    saites kartīte ir labāka par vēl vienu foto. Bez šī pārslēgšana apgāja
    visus sargus: choose_format izvēlējās saiti, šeit tā kļuva par foto,
    saites daļa palika 0 %, saites grīda tad bloķēja katru karuseli un
    lenti — plūsma bija tikai foto.
    """
    from app.formats import monotony_reason

    broken, loss = link_card_broken(session, channel, cfg, article, rules)
    if broken and monotony_reason(session, channel, cfg, "photo"):
        return False, loss
    return broken, loss


def link_card_broken(session, channel: str, cfg: dict, article,
                     rules: dict | None = None) -> tuple[bool, float]:
    """(vai saites kartīte šim rakstam ir sabojāta, cik daudz tā nogrieztu).

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
    if not (portrait or loss > rules.get("link_card_max_crop", 0.30)):
        return False, loss
    # Ja Facebook pieņem MŪSU kartītes attēlu (domēns verificēts), og:image
    # griezums vairs neko nenozīmē: kartītē ies mūsu 1.91:1 griezums ar
    # veselu augšu, un saites ieraksts paliek saites ieraksts.
    if link_picture_status(session, rules) == "ok":
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


# Facebook saites kartītes attēls, ko sūtām paši (`picture`), un tā statuss.
# Kartīte ir 1.91:1; ziņu foto parasti ir 3:2 vai 4:3, un FB griež vidu, tāpēc
# augša ar galvām pazūd. Mūsu griezums ir piesiets augšai (PHOTO_FOCUS), un
# attēls kartītē it kā «nobīdās zemāk» — galvas paliek kadrā.
LINK_PICTURE_KEY = "fb_link_picture"
LINK_PICTURE_SIZE = (1200, 628)
LINK_PICTURE_MIN_CROP = 0.05       # zem šī FB griezums ir nemanāms — nav ko labot
LINK_PICTURE_RETRY_DAYS = 7        # pēc noraidījuma mēģinām atkal pēc nedēļas


def link_picture_status(session, rules: dict | None = None) -> str:
    """'ok' — FB pieņēma mūsu attēlu; 'rejected' — noraidīja (domēns nav
    verificēts) un nedēļa vēl nav pagājusi; 'off' — izslēgts noteikumos;
    'unknown' — vēl nav mēģināts."""
    rules = config.load_rules() if rules is None else rules
    if not rules.get("link_card_custom_picture", True):
        return "off"
    row = credentials.info(session, LINK_PICTURE_KEY)
    if row is None or not row.value:
        return "unknown"
    if row.value == "ok":
        return "ok"
    if row.updated_at and utcnow() - row.updated_at > timedelta(days=LINK_PICTURE_RETRY_DAYS):
        return "unknown"
    return "rejected"


def link_picture_for(session, post, cfg: dict, rules: dict | None = None) -> str:
    """Publiskais URL mūsu saites kartītes attēlam šim ierakstam ('' = nesūtīt).

    Tikai Facebook saites ierakstiem, kuru og attēlu kartīte tiešām griež.
    Griezumu zīmē tieši pirms publicēšanas (fails nevar pazust rindā stāvot)
    un patur `extra["link_picture"]`, lai priekšskatījums un lēmumu vēsture
    rāda, kas aizgāja."""
    from pathlib import Path

    rules = config.load_rules() if rules is None else rules
    if (post.format != "link" or post.article is None
            or cfg.get("platform") != "facebook_page"
            or not (post.article.images or [])):
        return ""
    if link_picture_status(session, rules) in ("off", "rejected"):
        return ""
    from adapters.base import public_image_url
    from app import cards, imageinfo

    base = photo_base_image(post.article)
    if imageinfo.link_card_crop(post.article, base) < LINK_PICTURE_MIN_CROP:
        return ""
    current = (post.extra or {}).get("link_picture") or ""
    if not (current and Path(current).exists()):
        try:
            current = cards.render_crop(base, *LINK_PICTURE_SIZE,
                                        position=cards.PHOTO_FOCUS)
        except Exception as e:  # noqa: BLE001 — bez renderētāja iet parastā kartīte
            log.warning("saites kartītes attēls post %s netika uzzīmēts: %s", post.id, e)
            return ""
        post.extra = {**(post.extra or {}), "link_picture": current}
    url = public_image_url(current)
    if not url:
        log.warning("saites kartītes attēlam nav publiska URL (PUBLIC_BASE_URL) — "
                    "post %s iet ar FB griezumu", post.id)
    return url


def remember_link_picture_outcome(session, adapter, picture: str, post) -> None:
    """Pēc publicēšanas: pieraksta, vai FB mūsu attēlu pieņēma."""
    if not picture:
        return
    rejected = getattr(adapter, "picture_rejected", "")
    if rejected:
        credentials.put(session, LINK_PICTURE_KEY, "rejected", label=rejected[:200])
        post.extra = {**(post.extra or {}), "link_picture_rejected": rejected[:200]}
        log.warning("FB noraidīja saites kartītes attēlu (post %s) — verificē tv3.lv "
                    "domēnu Business Manager sadaļā Brand Safety → Domains", post.id)
    elif link_picture_status(session) != "ok":
        credentials.put(session, LINK_PICTURE_KEY, "ok", label="FB pieņem mūsu kartītes attēlu")


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


# Formāti, kuros saite iet arī pirmajā komentārā (FB/IG) vai atbildē (X,
# Threads): tie, kur ierakstu nes mūsu grafika, ne saites kartīte.
MEDIA_FORMATS = ("photo", "photo_album", "card_carousel", "reel", "video")


def link_placement(platform: str, fmt: str, rules: dict) -> tuple[bool, bool]:
    """(saite tekstā?, saite komentārā/atbildē?) — kur saite nonāk.

    Facebook / Instagram: mediju ierakstos saite iet pirmajā komentārā
    (SocialFlow taktika, `link_in_first_comment`) un FB paliek ARĪ aprakstā
    (`link_in_caption`) — tikai apraksts ar saiti ir tas, ko FB var pastiprināt
    kā traffic reklāmu. Instagram apraksta saites nav klikšķināmas, tāpēc tur
    tā aiziet pats par sevi (PLATFORM_SPECS link_in_copy=False).

    X / Threads: saite tekstā ir klikšķināma, un tas ir noklusējums — tieši
    tā lasītājs nonāk portālā ar vienu pieskārienu. `x_link_in_reply` /
    `threads_link_in_reply` pārslēdz uz «saite atbildē» taktiku: teksts bez
    saites, saite pirmajā atbildē. Tā palīdz sasniedzamībai, bet maksā
    klikšķus, tāpēc tā ir izvēle, ne noklusējums — un to var izmērīt (utm).
    """
    if fmt not in MEDIA_FORMATS:
        return True, False
    if platform in ("facebook_page", "instagram"):
        in_comment = bool(rules.get("link_in_first_comment", True))
        return (bool(rules.get("link_in_caption", True)) or not in_comment), in_comment
    if platform in ("x", "threads") and rules.get(f"{platform}_link_in_reply", False):
        return False, True
    return True, False


def link_pointer(platform: str, post, rules: dict) -> str:
    """Rinda apraksta beigās, kas pasaka, KUR saite ir — tikai tur, kur
    lasītājs to tekstā neredz (Instagram, vai X/Threads ar saiti atbildē).
    Facebook to nevajag: tur saite ir arī aprakstā."""
    from app.best_practices import EMOJI_RE, SOBER_SENSITIVITIES

    if platform == "instagram":
        pointer = rules.get("ig_link_pointer", "Saite komentāros 👇")
    elif platform in ("x", "threads"):
        pointer = rules.get("reply_link_pointer", "Saite atbildē 👇")
    else:
        return ""
    pointer = str(pointer or "").strip()
    article = getattr(post, "article", None)
    sensitivity = list(getattr(article, "sensitivity", None) or [])
    if any(s in SOBER_SENSITIVITIES for s in sensitivity):
        # traģēdijās un noziegumos bez emocijzīmēm, kā visā parakstā
        pointer = EMOJI_RE.sub("", pointer).strip()
    return pointer


def digest_items(post) -> list[dict]:
    return [it for it in ((post.extra or {}).get("items") or [])
            if isinstance(it, dict) and it.get("title")]


def reading_list(post, platform: str, rules: dict | None = None,
                 links: bool = True) -> str:
    """Numurēts saraksts, ko digest ieraksts («TOP 5», «Nedēļa 30
    sekundēs») sola: tekstā tikai virsraksti (apraksts paliek tīrs), pirmajā
    komentārā — ar saiti katram rakstam un savu utm_term (marker-N), lai GA4
    redz, kurš no pieciem tiešām atveda lasītāju."""
    from app.best_practices import add_utm

    items = digest_items(post)
    if not items:
        return ""
    rules = config.load_rules() if rules is None else rules
    hook = (post.hook_type or "").strip()
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['title']}")
        if links and it.get("url"):
            full = add_utm(it["url"], platform, post.id,
                           hook=f"{hook}-{i}" if hook else str(i))
            lines.append(full)
    return "\n".join(lines)


def first_comment_text(post, platform: str, shown_link: str,
                       rules: dict | None = None) -> str:
    """Kas iet pirmajā komentārā: digest ierakstam — lasāmais saraksts ar
    saitēm, parastam — tā pati saite, kas aprakstā."""
    return reading_list(post, platform, rules, links=True) or shown_link


def compose_text(post, platform: str, shown_link: str,
                 rules: dict | None = None) -> tuple[str, bool]:
    """(post text, whether the link also goes out as the first comment).

    Kur saite nonāk, nosaka `link_placement`; ja lasītājs to tekstā neredz,
    apraksta beigās ir norāde «Saite komentāros» (`link_pointer`), citādi
    Instagram lasītājs nezina, ka rakstu vispār var atvērt.
    """
    rules = config.load_rules() if rules is None else rules
    in_caption, in_comment = link_placement(platform, post.format, rules)
    in_comment = bool(shown_link) and in_comment
    in_caption = in_caption or not in_comment
    # ES MI akta 50. panta atruna. Noklusēti tikai tur, kur tiešām ir
    # mākslīgi ģenerēts medijs — lentē ar sintezēto balsi. Zem katra ieraksta
    # tā lasījās kā apgalvojums, ka MI ir uzrakstījis RAKSTU, un tas nav
    # taisnība: rakstu raksta žurnālists.
    from app import reels as _reels

    note = ""
    if disclosure.applies(post.format, _reels.has_voice(post), rules):
        note = disclosure.caption_line(platform, rules)
        if note and disclosure.in_caption(post.copy or "", rules):
            note = ""
    copy = post.copy or ""
    # digest ieraksts sola piecus stāstus — nosauc tos tekstā, lai lasītājs
    # zina, ko atradīs; saites katram ir pirmajā komentārā (X/Threads 280–500
    # zīmēs saraksts neietilpst, tur paliek galvenā saite — lasītākais raksts)
    if platform in ("facebook_page", "instagram"):
        titles = reading_list(post, platform, rules, links=False)
        if titles:
            copy = f"{copy}\n\n{titles}" if copy else titles
            in_comment = in_comment or bool(digest_items(post))
    spec = PLATFORM_SPECS.get(platform)
    reader_sees_link = in_caption and (spec.link_in_copy if spec else True)
    if in_comment and not reader_sees_link:
        pointer = link_pointer(platform, post, rules)
        low = copy.lower()
        already = pointer.lower() in low or (
            "saite" in low and ("koment" in low or "atbild" in low))
        if pointer and not already:
            copy = f"{copy}\n\n{pointer}" if copy else pointer
    text = assemble_post_text(copy, post.hashtags or [],
                              shown_link if in_caption else "", platform,
                              disclosure=note)
    return text, in_comment


def stale_now(post, rules: dict | None = None) -> str:
    """Iemesls, kāpēc ierakstu vairs nevajag publicēt ('' ja viss kārtībā).

    Svaigumu pārbauda lēmuma solī, bet ieraksts rindā var nostāvēt stundas.
    Bez šī labojums aizsniegtu tikai jaunos rakstus, un tie, kas jau ieplānoti
    par tālu, tik un tā iznāktu kā vakardienas ziņa šodienas stāstā.
    """
    rules = config.load_rules() if rules is None else rules
    if not rules.get("stale_publish_guard", True):
        return ""
    # Sargs atceļ TIKAI to, ko automātika ieplānojusi pati.
    #   timeless — franšīzes ieraksti («nedēļas TOP», «nedēļas skaitlis»,
    #     kvīzs): tie ir atskatoši pēc būtības, raksts tur ir atsauce, ne
    #     temats, un dienas vecums tiem ir plāns, ne nolaidība.
    #   manual — redaktors formātu pieprasījis pats. Cilvēka apzinātu lēmumu
    #     mēs neatceļam; ja viņš grib vecāku rakstu, tā ir viņa izvēle.
    extra = post.extra or {}
    if extra.get("timeless") or extra.get("manual"):
        return ""
    article = post.article
    if article is None or article.editor_timeframe == "evergreen":
        return ""
    max_age = (rules.get("max_age_hours") or {}).get(article.section)
    if max_age is None:
        return ""
    from app.rules_engine import article_age_hours

    age = article_age_hours(article, utcnow())
    if age <= float(max_age):
        return ""
    return (f"atcelts: saturs novecojis — {age:.0f} h vecs, "
            f"{article.section} limits ir {max_age} h")


def wiped_media_count(session) -> int:
    """Cik rindā stāvošu ierakstu zīmētie faili diskā vairs nav.

    Konteinera pārstarts bez pastāvīgā sējuma noslauka `data/cards`: lentes
    un karuseļus tad publicēt nevar, stāsts ar lenti krīt atpakaļ uz attēlu,
    un TTS kešs sākas no nulles. Startā to pasakām skaļi, lai iemesls
    nebūtu jāmeklē pa ierakstiem.
    """
    from pathlib import Path

    pending = session.execute(
        select(Post).where(Post.state.in_(("proposed", "scheduled")))).scalars().all()
    gone = 0
    for post in pending:
        for m in (post.media or []):
            m = str(m or "")
            if m and not m.startswith("http") and not Path(m).exists():
                gone += 1
                break
    return gone


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
    # stāsts ar attēlu, kamēr rakstam pa to laiku tapusi lente: stāstā balss
    # tiešām skan, tāpēc pēdējā brīdī ņemam lenti
    from adapters.base import is_video
    reel_now = (post.format == "story" and not is_video(current)
                and bool(article_reel_file(article)))
    if not (local_gone or raw_fallback or not media or stale_layout or reel_now):
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
        if stale_now(post):
            post.state = "cancelled"
            post.error = stale_now(post)
            session.commit()
            continue
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
            picture = link_picture_for(session, post, cfg, rules)
            if picture:
                extra_kwargs["picture"] = picture
            post.platform_post_id = adapter.publish(
                text=text, link=link, images=post.media or [], fmt=post.format,
                **extra_kwargs)
            remember_link_picture_outcome(session, adapter, picture, post)
            post.state = "published"
            post.published_at = utcnow()
            post.error = ""
            published += 1
            if first_comment_link and post.platform_post_id:
                try:
                    adapter.comment(post.platform_post_id,
                                    first_comment_text(post, platform, shown, rules))
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
