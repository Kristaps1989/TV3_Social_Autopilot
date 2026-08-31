"""Grafikas pārģenerēšana jau ieplānotam ierakstam.

Attēlus renderējam LĒMUMA brīdī, tāpēc ieraksts rindā nes savu tābrīža
grafiku. Kad dizains ir uzlabots, kad renderētājs bija nokritis vai kad
attēls vienkārši neielādējās, redaktoram jābūt pogai «Pārģenerēt», nevis
jāatceļ ieraksts un jāgaida, kamēr AI to izlemj no jauna.

Lai to varētu izdarīt, katrs renderētais ieraksts nes savu RECEPTI
(post.extra["recipe"]) — no kā tieši grafika bija uzbūvēta. Bez tās
karuseli vairs nevar atkārtot: AI kartīšu punkti pēc lēmuma nekur
nepaliek, un digest kartītes nāk no vairākiem rakstiem.
"""
from __future__ import annotations

import logging

from app.models import Article, utcnow

log = logging.getLogger(__name__)

KINDS = ("cards", "quiz", "mosaic", "reel", "number", "share",
         "article_cards", "article_reel")


# Franšīzes, kuras var uzbūvēt no jauna arī BEZ receptes — vienkārši palaižot
# to pašu celtnieku vēlreiz. Vecajiem ierakstiem (uzbūvētiem pirms receptēm)
# tas ir vienīgais ceļš, un kvīzam tas turklāt ir pareizais: jaunie jautājumi
# tiek ģenerēti pēc pašreizējiem noteikumiem.
REBUILDABLE = ("digest", "mondaytop5", "mondaystory", "dailystory",
               "digestreel", "quiz", "guide", "number", "question")


def can_regenerate(post) -> bool:
    """Vai šim ierakstam grafiku vispār var uzzīmēt no jauna."""
    if post is None or post.state not in ("proposed", "scheduled", "failed"):
        return False
    kind = ((post.extra or {}).get("recipe") or {}).get("kind")
    if kind in KINDS:
        return True
    if (post.hook_type or "") in REBUILDABLE:
        return True
    # photo/story bez receptes: pārzīmējam no paša raksta
    return post.format in ("photo", "story") and post.article is not None


def _rebuild_franchise(session, post) -> tuple[bool, str]:
    """Franšīzes ieraksts bez receptes: palaižam to pašu celtnieku vēlreiz un
    pārceļam rezultātu uz esošo ierakstu. Publicēšanas laiku saglabājam — to
    redaktors jau zina; mainās saturs, ne grafiks."""
    from app import weekend

    marker = post.hook_type or ""
    day = (post.scheduled_at or post.created_at or utcnow()).date()
    builders = {
        "digest": lambda: weekend.build_top5(
            session, day, "sport" if "sport" in (post.link_url or "") else None),
        "mondaytop5": lambda: weekend.build_monday_top5(session, day),
        "mondaystory": lambda: weekend.build_monday_story(session, day),
        "dailystory": lambda: weekend.build_daily_story(session, day),
        "digestreel": lambda: weekend.build_reel_digest(session, day),
        "quiz": lambda: weekend.build_quiz(session, day),
        "guide": lambda: weekend.build_weekend_guide(session, day),
        "number": lambda: weekend.build_number(session, day),
        "question": lambda: weekend.build_question(session, day),
    }
    builder = builders.get(marker)
    if builder is None:
        return False, "Šo franšīzi pārbūvēt nevar."
    try:
        fresh = builder()
    except Exception as e:  # noqa: BLE001
        log.warning("rebuild %s failed for post %s: %s", marker, post.id, e)
        return False, f"Pārbūve neizdevās: {e}"
    if fresh is None:
        return False, ("Nepietiek datu, lai formātu uzbūvētu no jauna "
                       "(par maz rakstu, nav AI atslēgas vai renderētājs klusē).")
    # nolasām rezultātu, jauno ierakstu izmetam, un tikai tad pārrakstām
    # esošo — citādi abi ieraksti īsu brīdi konkurē par vienu un to pašu vietu
    built = {"media": list(fresh.media or []), "copy": fresh.copy,
             "article_id": fresh.article_id, "link_url": fresh.link_url,
             "extra": dict(fresh.extra or {})}
    session.delete(fresh)
    session.flush()
    post.media = built["media"]
    post.copy = built["copy"]
    post.article_id = built["article_id"]
    post.link_url = built["link_url"]
    post.extra = built["extra"]
    session.commit()
    return True, "Formāts pārbūvēts no jauna ar pašreizējiem noteikumiem."


def _articles(session, ids: list) -> list[Article]:
    out = [session.get(Article, i) for i in ids or []]
    return [a for a in out if a is not None]


def regenerate(session, post) -> tuple[bool, str]:
    """(izdevās, ziņa redaktoram). Media aizstājam tikai tad, ja jaunais
    renders tiešām sanāca — neizdevies mēģinājums nedrīkst atstāt ierakstu
    bez attēla."""
    from app import cards, reels, tts
    from app.weekend import _any_image, _clean_image

    if not can_regenerate(post):
        return False, "Šim ierakstam grafiku pārģenerēt nevar."
    recipe = (post.extra or {}).get("recipe") or {}
    kind = recipe.get("kind", "")
    if not kind and (post.hook_type or "") in REBUILDABLE:
        return _rebuild_franchise(session, post)
    date_txt = recipe.get("date", "")
    section = recipe.get("section", "news")
    try:
        if kind == "cards":
            arts = _articles(session, recipe.get("articles"))
            if len(arts) < 2:
                return False, "Raksti, no kuriem karuselis bija būvēts, vairs nav pieejami."
            media = cards.render_cards(
                recipe.get("title", ""), section, recipe.get("tag", "#TOP5"),
                [a.title.rstrip(".") for a in arts], "", "",
                date_txt=date_txt,
                point_images=[_clean_image(a) for a in arts],
                point_blur=[("" if _clean_image(a) else _any_image(a))
                            for a in arts],
                point_dates=recipe.get("dates") or [],
                include_cover=False, include_end=False,
                label=recipe.get("ribbon", ""))
        elif kind == "quiz":
            cover = _articles(session, recipe.get("articles"))
            image = next((i for i in (_clean_image(a) for a in cover) if i), "")
            blur = "" if image else next(
                (i for i in (_any_image(a) for a in cover) if i), "")
            media = cards.render_cards(
                recipe.get("title", ""), section, recipe.get("tag", "#QUIZ"),
                recipe.get("questions") or [], image,
                recipe.get("question", "Atbildes — tv3.lv"),
                cover_blur=blur, date_txt=date_txt)
        elif kind == "article_cards":
            art = session.get(Article, recipe.get("article"))
            if art is None:
                return False, "Raksts vairs nav pieejams."
            from app.pipeline import photo_base_image, prebranded

            image = photo_base_image(art)
            media = cards.render_cards(
                art.title, art.section or section, recipe.get("tag", ""),
                recipe.get("points") or [], image,
                recipe.get("question", "Uzzini visu stāstu tv3.lv"),
                cover_title=not prebranded(image),
                point_bg=_clean_image(art), date_txt=date_txt)
        elif kind == "mosaic":
            arts = _articles(session, recipe.get("articles"))
            images = [i for i in (_clean_image(a) for a in arts) if i]
            if len(images) < 3:
                return False, "Nepietiek tīru foto mozaīkai (vajag vismaz trīs)."
            media = [cards.render_mosaic_story(recipe.get("title", ""), section,
                                               images, date_txt=date_txt)]
        elif kind == "number":
            art = session.get(Article, recipe.get("article"))
            media = [cards.render_number_card(
                recipe.get("number", ""), recipe.get("context", ""),
                (art.section if art else section) or section,
                _clean_image(art) if art else "", date_txt=date_txt)]
        elif kind == "share":
            art = session.get(Article, recipe.get("article"))
            media = [cards.render_share_image(
                recipe.get("title", ""), (art.section if art else section) or section,
                _clean_image(art) if art else "",
                kicker=recipe.get("kicker", ""), width=1080, height=1350,
                date_txt=date_txt)]
        elif kind == "reel":
            arts = _articles(session, recipe.get("articles"))
            imgs = [_clean_image(a) for a in arts]
            media = [reels.build_reel(
                recipe.get("title", ""), section, "",
                recipe.get("points") or [], max_points=5, frame_seconds=6.0,
                edge_seconds=3.0, cover_images=[i for i in imgs if i],
                point_images=imgs)]
        elif kind == "article_reel":
            art = session.get(Article, recipe.get("article"))
            if art is None:
                return False, "Raksts vairs nav pieejams."
            # ieruna nāk no receptes teksta: pārzīmējot to pašu reelu, Azure
            # atbilde jau ir kešā, tāpēc otrreiz par to nemaksājam
            audio = tts.reel_voice(recipe, session=session)
            media = [reels.build_reel(art.title, art.section or section,
                                      recipe.get("image", ""),
                                      recipe.get("points") or [],
                                      voice=audio or None)]
            recipe = {**recipe, "voiced": bool(audio)}
        else:   # photo / story bez receptes — zīmējam no paša raksta
            from app.pipeline import (branded_photo, photo_base_image,
                                      story_media)

            art = post.article
            channels = __import__("app.config", fromlist=["config"]).load_channels()
            platform = (channels.get(post.channel) or {}).get("platform", "")
            if post.format == "photo":
                image = photo_base_image(art)
                media = [branded_photo(art, image, platform)] if image else []
            else:
                media = story_media(art, (art.images or [""])[0])
    except Exception as e:  # noqa: BLE001 — kļūda nedrīkst nogāzt lapu
        log.warning("regenerate failed for post %s: %s", post.id, e)
        cards.record_render_failure(kind or post.format, e)
        return False, f"Renderēšana neizdevās: {e}"
    if not media:
        return False, "Renderēšana neizdevās — attēls netika uzzīmēts."
    post.media = media
    # recepte tiek rakstīta atpakaļ, jo pārzīmēšana to var precizēt — piem.,
    # reels ar balsi, kas iepriekš bija kluss (atslēga pieslēgta pa vidu)
    post.extra = {**(post.extra or {}), "render_version": cards.RENDER_VERSION,
                  **({"recipe": recipe} if recipe else {})}
    session.commit()
    return True, f"Grafika pārģenerēta ({len(media)} attēls/-i)."
