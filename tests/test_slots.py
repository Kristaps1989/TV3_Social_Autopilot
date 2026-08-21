from datetime import datetime, timedelta

from app.models import Article, Post
from app.rules_engine import Verdict
from app.slots import find_slot, violates_diversity

# 10:00 UTC = 13:00 Riga (EEST, summer) — daytime, outside quiet hours
NOW = datetime(2026, 8, 20, 10, 0)

CHANNEL = {"platform": "x", "min_gap_minutes": 30, "daily_cap": 5,
           "quiet_hours": ["01:00-06:00"], "formats": ["link"]}


def eligible(latest=None):
    return Verdict("eligible", earliest=NOW, latest=latest)


def make_article(session, guid="a1", title="Pavisam cits virsraksts par būvniecību", **kw):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title=title, section=kw.pop("section", "news"),
                first_seen_at=NOW, **kw)
    session.add(a)
    session.flush()
    return a


def sched(session, article, offset_minutes, fmt="link", state="scheduled"):
    p = Post(article_id=article.id, channel="x_test", format=fmt, copy=article.title,
             scheduled_at=NOW + timedelta(minutes=offset_minutes), state=state)
    session.add(p)
    session.flush()
    return p


def test_empty_queue_gets_prompt_slot(session):
    slot = find_slot(session, "x_test", CHANNEL, eligible(), "news", "link",
                     "Jauns virsraksts", NOW)
    assert slot is not None
    assert slot - NOW < timedelta(hours=4)


def test_min_gap_respected(session):
    a = make_article(session)
    sched(session, a, 0)
    slot = find_slot(session, "x_test", CHANNEL, eligible(), "sport", "photo",
                     "Cits teksts par hokeju", NOW)
    assert slot is not None
    assert slot - NOW >= timedelta(minutes=30)


def test_daily_cap_pushes_to_next_day(session):
    for i in range(5):
        a = make_article(session, guid=f"g{i}", title=f"Unikāls virsraksts numur {i} tēma {i}",
                         section="sport" if i % 2 else "news")
        sched(session, a, i * 40, fmt="photo" if i % 2 else "link")
    slot = find_slot(session, "x_test", CHANNEL, eligible(), "news", "link",
                     "Vēl viens pilnīgi cits stāsts", NOW)
    # 5 posts already today (cap=5) -> slot must fall on another local day
    from zoneinfo import ZoneInfo

    local_slot = slot.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Riga"))
    local_now = NOW.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Riga"))
    assert slot is None or local_slot.date() != local_now.date()


def test_similarity_guard_blocks_duplicate(session):
    a = make_article(session, title="Rīgā atklāta jauna sporta halle")
    sched(session, a, -10, state="published")
    slot = find_slot(session, "x_test", CHANNEL, eligible(), "news", "link",
                     "Jauna sporta halle atklāta Rīgā", NOW)
    assert slot is None


def test_must_deadline_respected(session):
    deadline = NOW + timedelta(hours=2)
    slot = find_slot(session, "x_test", CHANNEL, eligible(latest=deadline),
                     "news", "link", "Steidzams stāsts", NOW)
    assert slot is not None
    assert slot <= deadline


def test_forced_now_immediate_on_empty_queue(session):
    v = Verdict("forced_now", earliest=NOW, latest=NOW + timedelta(minutes=5))
    slot = find_slot(session, "x_test", CHANNEL, v, "news", "link", "Ārkārtas ziņa", NOW)
    assert slot == NOW


def test_forced_now_burst_is_spaced(session):
    # a 'now' item at the same moment as an existing post gets spaced out
    # by the burst gap instead of flooding the channel
    a = make_article(session)
    sched(session, a, 0)
    v = Verdict("forced_now", earliest=NOW, latest=NOW + timedelta(minutes=5))
    slot = find_slot(session, "x_test", CHANNEL, v, "news", "link", "Ārkārtas ziņa", NOW)
    assert slot == NOW + timedelta(minutes=5)


def test_diversity_needs_mixed_sections():
    rules = {"section_mix": {"window": 3, "min_distinct_sections": 2,
                             "min_distinct_formats": 1}}

    class P:  # minimal stand-in
        def __init__(self, section, fmt, when):
            self.article = type("A", (), {"section": section})()
            self.format = fmt
            self.scheduled_at = when

    slot = NOW + timedelta(hours=1)
    same = [P("news", "link", NOW - timedelta(minutes=i)) for i in (10, 20)]
    assert violates_diversity(same, "news", "link", slot, rules)
    assert not violates_diversity(same, "sport", "link", slot, rules)


def test_quiet_hours_avoided(session):
    # 23:30 UTC = 02:30 Riga -> inside 01:00-06:00 quiet window
    night = datetime(2026, 8, 20, 23, 30)
    slot = find_slot(session, "x_test", CHANNEL, Verdict("eligible", earliest=night),
                     "news", "link", "Nakts ziņa", night)
    assert slot is not None
    from zoneinfo import ZoneInfo

    local = slot.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Riga"))
    assert not (1 <= local.hour < 6)
