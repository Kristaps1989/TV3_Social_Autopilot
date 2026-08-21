from datetime import datetime, timedelta

from app.models import Article
from app.rules_engine import evaluate, in_window, parse_window, title_similarity

NOW = datetime(2026, 8, 20, 10, 0)  # UTC

RULES = {
    "must_max_delay_hours": 6,
    "now_max_delay_minutes": 5,
    "max_age_hours": {"news": 12, "sport": 8, "entertainment": 48},
    "time_windows": [
        {"match": {"sensitivity": ["nudity", "party"]},
         "allow": ["22:00-06:00"], "unless_status": ["now"]},
    ],
    "term_blocklist": {"ch1": [999]},
    "term_allowlist": {},
}
CHANNEL = {"platform": "x", "sections": [], "formats": ["link"]}


def art(**kw):
    defaults = dict(guid="g1", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                    title="Test title", section="news", editor_status="can",
                    editor_timeframe="", term_ids=[], sensitivity=[],
                    published_at=NOW - timedelta(hours=1), first_seen_at=NOW)
    defaults.update(kw)
    return Article(**defaults)


def test_dont_blocks():
    v = evaluate(art(editor_status="dont"), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "blocked"


def test_now_forces_immediate():
    v = evaluate(art(editor_status="now"), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "forced_now"
    assert v.latest == NOW + timedelta(minutes=5)


def test_must_gets_deadline():
    v = evaluate(art(editor_status="must"), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "eligible"
    assert v.latest == NOW + timedelta(hours=6)


def test_can_is_eligible_without_deadline():
    v = evaluate(art(), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "eligible"
    assert v.latest is None


def test_stale_news_blocked():
    v = evaluate(art(published_at=NOW - timedelta(hours=13)), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "blocked"
    assert "too old" in v.reason


def test_evergreen_never_stale():
    v = evaluate(art(published_at=NOW - timedelta(hours=100), editor_timeframe="evergreen"),
                 "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "eligible"


def test_section_ages_differ():
    sport_old = art(section="sport", published_at=NOW - timedelta(hours=9))
    assert evaluate(sport_old, "ch1", CHANNEL, RULES, NOW).outcome == "blocked"
    ent_old = art(section="entertainment", published_at=NOW - timedelta(hours=9))
    assert evaluate(ent_old, "ch1", CHANNEL, RULES, NOW).outcome == "eligible"


def test_section_routing():
    cfg = dict(CHANNEL, sections=["sport"])
    assert evaluate(art(section="news"), "ch1", cfg, RULES, NOW).outcome == "blocked"
    assert evaluate(art(section="sport"), "ch1", cfg, RULES, NOW).outcome == "eligible"


def test_term_blocklist():
    v = evaluate(art(term_ids=["999"]), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "blocked"
    assert "blocklist" in v.reason


def test_term_allowlist():
    rules = dict(RULES, term_allowlist={"ch1": [26336]})
    assert evaluate(art(term_ids=["3"]), "ch1", CHANNEL, rules, NOW).outcome == "blocked"
    assert evaluate(art(term_ids=["26336"]), "ch1", CHANNEL, rules, NOW).outcome == "eligible"


def test_sensitivity_gets_night_window():
    v = evaluate(art(sensitivity=["nudity"]), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "eligible"
    assert v.allowed_windows == [parse_window("22:00-06:00")]


def test_now_overrides_sensitivity_window():
    v = evaluate(art(sensitivity=["nudity"], editor_status="now"), "ch1", CHANNEL, RULES, NOW)
    assert v.outcome == "forced_now"


def test_overnight_window_logic():
    start, end = parse_window("22:00-06:00")
    from datetime import time

    assert in_window(time(23, 0), start, end)
    assert in_window(time(2, 0), start, end)
    assert not in_window(time(12, 0), start, end)


def test_title_similarity():
    a = "Rīgā atklāta jauna sporta halle pēc rekonstrukcijas"
    b = "Rīgā pēc rekonstrukcijas atklāta jauna sporta halle"
    c = "Valdība apstiprina nākamā gada budžetu"
    assert title_similarity(a, b) > 0.7
    assert title_similarity(a, c) < 0.2
