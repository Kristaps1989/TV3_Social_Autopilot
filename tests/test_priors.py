from datetime import timedelta

from app import priors
from app.formats import choose_format
from app.models import Article, Post, PostMetrics, utcnow


def seed(session, channel="fb_p", n=40, fmt="link", base_hour=19, score=10):
    """n published posts at base_hour local (UTC+3 summer => hour-3 UTC)."""
    for i in range(n):
        a = Article(guid=f"{channel}-{fmt}-{base_hour}-{i}", url=f"https://tv3.lv/p{i}",
                    canonical_url=f"https://tv3.lv/p{channel}{fmt}{base_hour}{i}",
                    title=f"Raksts {channel} {fmt} {i}", section="news")
        session.add(a)
        session.flush()
        published = (utcnow() - timedelta(days=1 + i % 20)).replace(
            hour=(base_hour - 3) % 24, minute=0)
        p = Post(article_id=a.id, channel=channel, format=fmt, state="published",
                 published_at=published, scheduled_at=published, copy="x")
        session.add(p)
        session.flush()
        session.add(PostMetrics(post_id=p.id, ga_sessions=score, clicks=score))
    session.commit()


def test_hour_curve_needs_enough_data(session):
    seed(session, n=10)
    assert priors.channel_hour_weights(session, "fb_p") is None


def test_hour_curve_peaks_at_measured_hour(session):
    seed(session, n=25, base_hour=20, score=50)
    seed(session, n=25, base_hour=9, score=5)
    weights = priors.channel_hour_weights(session, "fb_p")
    assert weights is not None
    assert weights[20] == 1.0  # peak normalized
    assert weights[9] < 0.3
    assert 0 < weights[3] < 1  # never-posted hours keep exploration weight


def test_format_multipliers_reward_measured_winners(session):
    seed(session, channel="fb_q", n=10, fmt="link", score=10)
    seed(session, channel="fb_q", n=10, fmt="photo", base_hour=12, score=40)
    mult = priors.format_multipliers(session, "fb_q")
    assert mult["photo"] > 1.0 > mult["link"]


def test_format_multiplier_flips_chooser(session):
    # config prefers link, but measured data shows photo earns 4x sessions;
    # recent feed is link-heavy so diversity doesn't fight the measurement
    seed(session, channel="fb_r", n=10, fmt="photo", base_hour=12, score=40)
    seed(session, channel="fb_r", n=10, fmt="link", score=10)
    cfg = {"formats": ["link", "photo"],
           "format_weights": {"link": 1.0, "photo": 0.9}}
    a = Article(guid="pr-x", url="https://tv3.lv/x", canonical_url="https://tv3.lv/x",
                title="Cits", section="entertainment", images=["i.jpg"])
    session.add(a)
    session.flush()
    assert choose_format(session, "fb_r", cfg, a) == "photo"


def test_summary_and_top_posts(session):
    seed(session, channel="fb_s", n=6, score=30)
    s = priors.channel_summary(session, "fb_s")
    assert s["posts"] == 6
    assert s["sessions"] == 180
    assert s["formats"][0]["format"] == "link"
    top = priors.top_posts(session, 3)
    assert len(top) == 3
    assert top[0]["score"] >= top[-1]["score"]


def test_prompt_context_mentions_formats(session):
    seed(session, channel="fb_t", n=40, score=25)
    text = priors.prompt_context(session, ["fb_t"])
    assert "fb_t" in text and "link" in text
