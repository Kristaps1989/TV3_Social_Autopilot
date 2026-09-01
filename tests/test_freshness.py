"""Svaigums ir griesti arī slotam, ne tikai lēmumam."""
from datetime import datetime, timedelta

from app import config
from app.models import Article, Post


def _news(session, session_hours_old=1.0, guid="fr-1"):
    now = datetime(2026, 9, 1, 19, 0)
    a = Article(guid=guid, url="u", canonical_url="u", title="Šodienas ziņa",
                section="news", raw_json={}, images=["https://cdn/f.jpg"],
                published_at=now - timedelta(hours=session_hours_old),
                first_seen_at=now - timedelta(hours=session_hours_old))
    session.add(a)
    session.flush()
    return a, now


def test_the_slot_may_not_outlive_the_articles_freshness(session):
    """max_age_hours pārbaudīja TIKAI lēmuma brīdi; slots pēc tam varēja
    aizceļot 48 h uz priekšu, un šodienas ziņa iznāca kā parīta stāsts."""
    from app.rules_engine import evaluate
    from app.slots import plan_slot

    a, now = _news(session)
    cfg = {"platform": "facebook_page", "formats": ["story"],
           "quiet_hours": ["00:00-07:00"], "min_gap_minutes": 120,
           "daily_cap_per_section": 2}
    v = evaluate(a, "fb_stories", cfg, config.load_rules(), now)
    assert v.outcome == "eligible"
    # news limits ir 12 h; raksts publicēts pirms stundas -> griesti 12 h no
    # publicēšanas, nevis 48 h no šodienas
    assert v.fresh_until == a.published_at + timedelta(hours=12)

    # aizpildām šodienu un rītdienu tā, ka brīvs paliek tikai 03.09
    for day, hour in ((1, 20), (1, 22), (2, 7), (2, 9), (2, 11)):
        session.add(Post(article_id=a.id, channel="fb_stories", format="story",
                         state="scheduled",
                         scheduled_at=datetime(2026, 9, day, hour, 0)))
    session.commit()
    slot, why = plan_slot(session, "fb_stories", cfg, v, "news", "story",
                          a.title, now, allow_similar=True)
    assert slot is None, f"neievietojams, bet dabūjām {slot}"
    assert "par vecu" in why


def test_an_evergreen_article_has_no_ceiling(session):
    from app.rules_engine import evaluate

    a, now = _news(session, guid="fr-2")
    a.editor_timeframe = "evergreen"
    session.flush()
    cfg = {"platform": "facebook_page", "formats": ["story"]}
    assert evaluate(a, "fb_stories", cfg, config.load_rules(),
                    now).fresh_until is None


def test_a_full_queue_may_drop_the_status_deadline_but_not_freshness(session):
    """«vēlāk — rinda bija pilna» ceļš atmeta VISU termiņu, arī svaigumu."""
    import dataclasses

    from app.rules_engine import evaluate
    from app.slots import plan_slot

    a, now = _news(session, guid="fr-3")
    a.editor_status = "must"
    session.flush()
    cfg = {"platform": "facebook_page", "formats": ["story"],
           "quiet_hours": ["00:00-07:00"], "min_gap_minutes": 120}
    v = evaluate(a, "fb_stories", cfg, config.load_rules(), now)
    assert v.latest is not None and v.fresh_until is not None
    # tieši to dara cauruļvads, kad statusa logā vietas nav
    relaxed = dataclasses.replace(v, latest=None)
    assert relaxed.fresh_until == v.fresh_until

    for hour in (19, 21, 23):
        session.add(Post(article_id=a.id, channel="fb_stories", format="story",
                         state="scheduled",
                         scheduled_at=datetime(2026, 9, 1, hour, 5)))
    session.commit()
    slot, _why = plan_slot(session, "fb_stories", cfg, relaxed, "news", "story",
                           a.title, now, allow_similar=True)
    # ja slots atrodas, tas nekad nav aiz svaiguma griestiem
    assert slot is None or slot <= v.fresh_until


def test_a_post_that_went_stale_in_the_queue_is_cancelled(session):
    """Ieraksts rindā var nostāvēt stundas. Bez šī labojums aizsniegtu tikai
    jaunos rakstus, un jau ieplānotie tik un tā iznāktu novecojuši."""
    from app.models import utcnow
    from app.pipeline import stale_now

    now = utcnow()
    a = Article(guid="fr-4", url="u", canonical_url="u", title="Vakardienas ziņa",
                section="news", raw_json={},
                published_at=now - timedelta(hours=30),
                first_seen_at=now - timedelta(hours=30))
    session.add(a)
    session.flush()
    post = Post(article_id=a.id, channel="fb_stories", format="story",
                copy="c", hashtags=[], state="scheduled", scheduled_at=now)
    session.add(post)
    session.flush()

    why = stale_now(post)
    assert why and "novecojis" in why and "30" in why

    # evergreen nenoveco
    a.editor_timeframe = "evergreen"
    session.flush()
    assert stale_now(post) == ""

    # un sargu var izslēgt
    a.editor_timeframe = ""
    session.flush()
    assert stale_now(post) != ""
    assert stale_now(post, {"stale_publish_guard": False}) == ""


def test_a_fresh_post_is_left_alone(session):
    from app.models import utcnow
    from app.pipeline import stale_now

    now = utcnow()
    a = Article(guid="fr-5", url="u", canonical_url="u", title="Svaiga ziņa",
                section="news", raw_json={},
                published_at=now - timedelta(hours=2),
                first_seen_at=now - timedelta(hours=2))
    session.add(a)
    session.flush()
    post = Post(article_id=a.id, channel="fb_stories", format="story",
                copy="c", hashtags=[], state="scheduled", scheduled_at=now)
    session.add(post)
    session.flush()
    assert stale_now(post) == ""


def test_the_weekly_franchise_is_retrospective_by_design(session):
    """«Nedēļas TOP» atsaucas uz nedēļas rakstiem: dienas vecums tiem ir
    plāns, ne nolaidība. Bez šī izņēmuma sargs atceltu visu nedēļas
    nogales programmu."""
    from app.models import utcnow
    from app.pipeline import stale_now

    now = utcnow()
    a = Article(guid="fr-6", url="u", canonical_url="u", title="Nedēļas raksts",
                section="news", raw_json={},
                published_at=now - timedelta(days=4),
                first_seen_at=now - timedelta(days=4))
    session.add(a)
    session.flush()
    plain = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="c",
                 hashtags=[], state="scheduled", scheduled_at=now)
    franchise = Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                     copy="c", hashtags=[], state="scheduled",
                     scheduled_at=now, extra={"timeless": True})
    session.add_all([plain, franchise])
    session.flush()

    assert stale_now(plain) != ""        # parasts ieraksts: 4 dienas ir par vecu
    assert stale_now(franchise) == ""    # franšīze: tā tam jābūt


def test_the_weekend_builder_marks_its_posts_timeless():
    """Karogs jāuzliek tur, kur ieraksts top — ne jāuzmin pēc receptes veida."""
    import inspect

    from app import weekend

    assert '"timeless": True' in inspect.getsource(weekend._schedule)


def test_an_editor_request_is_never_cancelled_as_stale(session):
    """Cilvēka apzinātu lēmumu automātika neatceļ: ja redaktors pieprasa
    formātu vecākam rakstam, tā ir viņa izvēle."""
    from app.models import utcnow
    from app.pipeline import stale_now

    now = utcnow()
    a = Article(guid="fr-7", url="u", canonical_url="u", title="Vecāka ziņa",
                section="news", raw_json={},
                published_at=now - timedelta(hours=30),
                first_seen_at=now - timedelta(hours=30))
    session.add(a)
    session.flush()
    auto = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="c",
                hashtags=[], state="scheduled", scheduled_at=now)
    asked = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="c",
                 hashtags=[], state="scheduled", scheduled_at=now,
                 extra={"manual": True})
    session.add_all([auto, asked])
    session.flush()
    assert stale_now(auto) != ""
    assert stale_now(asked) == ""
