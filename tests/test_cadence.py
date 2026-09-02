"""Kadence ziņām un sportam: adaptīva atstarpe, rindas pārplānošana pēc
vērtības un svaiguma, kluso stundu izņēmumi."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app import config, pipeline, slots
from app.models import Article, Post
from app.rules_engine import Verdict

NOW = datetime(2026, 9, 2, 17, 0)          # 20:00 Rīgā — vakara ziņu vilnis
FB = {"platform": "facebook_page", "min_gap_minutes": 45, "min_gap_floor_minutes": 15,
      "daily_cap": 0, "quiet_hours": ["01:00-06:00"],
      "formats": ["link", "photo", "card_carousel"]}


def _article(session, guid, title, section="news", score=0.5, age_minutes=10,
             status="can"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title=title, section=section, ai_score=score, editor_status=status,
                first_seen_at=NOW - timedelta(minutes=age_minutes),
                published_at=NOW - timedelta(minutes=age_minutes))
    session.add(a)
    session.flush()
    return a


def _post(session, article, at, channel="fb_cad", fmt="link", extra=None):
    p = Post(article_id=article.id, channel=channel, format=fmt, copy=article.title,
             scheduled_at=at, state="scheduled", extra=extra or {})
    session.add(p)
    session.flush()
    return p


def test_gap_shrinks_with_the_backlog_but_never_below_the_floor(monkeypatch):
    monkeypatch.setattr(config, "load_rules", lambda: {"backlog_horizon_hours": 2})
    assert slots.adaptive_gap(FB, 0) == timedelta(minutes=45)      # tukša rinda
    assert slots.adaptive_gap(FB, 1) == timedelta(minutes=45)      # 120/2 = 60 -> 45
    assert slots.adaptive_gap(FB, 3) == timedelta(minutes=30)      # 120/4
    assert slots.adaptive_gap(FB, 7) == timedelta(minutes=15)      # 120/8 -> grīda
    assert slots.adaptive_gap(FB, 40) == timedelta(minutes=15)
    # stāstu kanālam adaptācijas nav — tur atstarpe nav pret pārplūdi
    stories = {"platform": "facebook_page", "min_gap_minutes": 120, "formats": ["story"]}
    assert slots.adaptive_gap(stories, 10) == timedelta(minutes=120)
    # platformas noklusējums, ja kanālā grīda nav dota
    x = {"platform": "x", "min_gap_minutes": 15, "formats": ["link", "photo"]}
    assert slots.gap_floor(x) == timedelta(minutes=10)


def test_deep_evening_backlog_no_longer_spills_into_tomorrow(session, monkeypatch):
    """Ekrānuzņēmuma gadījums: ar fiksētu 45 min astoņas vakara ziņas
    sarindojās līdz 09:45 nākamajā rītā. Adaptīvā atstarpe tās izlaiž līdz
    pusnaktij."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    titles = ["Saeima pieņem budžeta grozījumus", "Rīgā sāk tramvaja līnijas remontu",
              "Vētra Kurzemē atstāj tūkstošus bez elektrības", "Latvijas hokejisti uzvar Somiju",
              "Eiro kurss sasniedz gada augstāko līmeni", "Policija aiztur aizdomās turamo Daugavpilī",
              "Jaunā skola Mārupē atver durvis", "Zinātnieki atklāj jaunu sugu Gaujas parkā"]
    slot_times = []
    for i, title in enumerate(titles):
        a = _article(session, f"ev-{i}", title, age_minutes=5)
        v = Verdict("eligible", earliest=NOW, fresh_until=NOW + timedelta(hours=12))
        slot, why = slots.plan_slot(session, "fb_cad", FB, v, "news", "link", a.title,
                                    NOW, score=0.5, age_hours=0.1)
        assert slot is not None, why
        _post(session, a, slot)
        slot_times.append(slot)
    # pēdējais slots ir pirms pusnakts Rīgā (21:00 UTC), ne rīt 09:45.
    # Vēlāk plānotie var aizpildīt agrākus caurumus (atstarpei sarūkot), tāpēc
    # kārtojam pēc laika — galīgo secību pēc vērtības tāpat nosaka replan.
    ordered = sorted(slot_times)
    assert ordered[-1] <= NOW + timedelta(hours=4)
    gaps = [(b - a) for a, b in zip(ordered, ordered[1:])]
    assert min(gaps) >= timedelta(minutes=15)
    assert min(gaps) < timedelta(minutes=45)       # atstarpe saraujas, rindai augot
    # ar fiksētu 45 min astoņi ieraksti aizņemtu 5 h 15 min
    assert ordered[-1] - ordered[0] < timedelta(hours=5)


def test_replan_orders_by_value_and_freshness_not_arrival(session, monkeypatch):
    """Redakcijas "must" iet pirmais, vēlāk ienākusi svaiga sporta ziņa ar
    augstu vērtējumu iet pirms vecākas vājas ziņas, kas rindā stāvēja pirmā."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    old_news = _article(session, "rp-1", "Veca vāja ziņa par kaut ko", score=0.3,
                        age_minutes=240)
    fresh_sport = _article(session, "rp-2", "Svaigs sporta rezultāts hokejā", section="sport",
                           score=0.9, age_minutes=10)
    must = _article(session, "rp-3", "Redakcijas svarīgā ziņa", score=0.6, age_minutes=60,
                    status="must")
    p1 = _post(session, old_news, NOW + timedelta(minutes=30))
    p2 = _post(session, fresh_sport, NOW + timedelta(minutes=75))
    p3 = _post(session, must, NOW + timedelta(minutes=120))
    session.commit()
    # redakcijas "must" iet pirmais; svaigais sports apsteidz veco vājo ziņu,
    # lai gan rindā tā stāvēja pirmā
    assert slots.priority(p3, NOW) > slots.priority(p2, NOW) > slots.priority(p1, NOW)

    out = slots.replan_channel(session, "fb_cad", FB, NOW)
    assert out["moved"] >= 2
    order = sorted([p1, p2, p3], key=lambda p: p.scheduled_at)
    assert [p.id for p in order] == [p3.id, p2.id, p1.id]
    assert p2.extra["replanned"]["priority"] > 1.0


def test_replan_leaves_manual_franchise_and_now_posts_alone(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, "fx-1", "Parasta ziņa viens", score=0.2, age_minutes=300)
    b = _article(session, "fx-2", "Parasta ziņa divi", score=0.9, age_minutes=5)
    fixed = _post(session, a, NOW + timedelta(minutes=30), extra={"manual": True})
    franchise = _post(session, a, NOW + timedelta(hours=2), extra={"timeless": True})
    now_post = _post(session, a, NOW + timedelta(minutes=5), extra={"forced_now": True})
    weak = _post(session, a, NOW + timedelta(minutes=60))
    strong = _post(session, b, NOW + timedelta(minutes=105))
    session.commit()
    slots.replan_channel(session, "fb_cad", FB, NOW)
    assert fixed.scheduled_at == NOW + timedelta(minutes=30)
    assert franchise.scheduled_at == NOW + timedelta(hours=2)
    assert now_post.scheduled_at == NOW + timedelta(minutes=5)
    assert strong.scheduled_at < weak.scheduled_at


def test_replan_cancels_what_would_be_stale_by_its_slot(session, monkeypatch):
    """Vecas ziņas publicēšana ir sliktāka par nepublicēšanu: ja rindā
    stāvošais raksts jau pārsniedz max_age_hours, to atceļ uzreiz un slots
    aiziet svaigākam."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    stale = _article(session, "st-1", "Ziņa no rīta, kas rindā novecoja",
                     age_minutes=13 * 60)           # news max_age 12 h
    fresh = _article(session, "st-2", "Svaiga ziņa", age_minutes=5)
    p_stale = _post(session, stale, NOW + timedelta(minutes=30))
    p_fresh = _post(session, fresh, NOW + timedelta(minutes=75))
    session.commit()
    out = slots.replan_channel(session, "fb_cad", FB, NOW)
    assert out["cancelled"] == 1
    assert p_stale.state == "cancelled" and "novecojis" in p_stale.error
    assert p_fresh.state == "scheduled" and p_fresh.scheduled_at <= NOW + timedelta(minutes=45)


def test_quiet_hours_let_through_sport_results_and_strong_news(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    night = datetime(2026, 9, 2, 23, 30)            # 02:30 Rīgā — klusās stundas
    v = Verdict("eligible", earliest=night, fresh_until=night + timedelta(hours=8))
    # parasta ziņa gaida rītu
    slot, _ = slots.plan_slot(session, "fb_cad", FB, v, "news", "link", "Parasta ziņa",
                              night, score=0.5, age_hours=0.5)
    assert slot >= datetime(2026, 9, 3, 3, 0)       # 06:00 Rīgā
    # sporta rezultāts pēc vakara mača iet uzreiz
    slot, _ = slots.plan_slot(session, "fb_cad", FB, v, "sport", "link", "Hokeja rezultāts",
                              night, score=0.5, age_hours=0.5)
    assert slot == night
    # ļoti spēcīga ziņa arī
    slot, _ = slots.plan_slot(session, "fb_cad", FB, v, "news", "link", "Liela ziņa",
                              night, score=0.9, age_hours=0.5)
    assert slot == night
    # ...bet ne vecs sporta raksts (izņēmums ir svaigumam, ne sadaļai)
    slot, _ = slots.plan_slot(session, "fb_cad", FB, v, "sport", "link", "Vecs sporta raksts",
                              night, score=0.5, age_hours=5)
    assert slot >= datetime(2026, 9, 3, 3, 0)


def test_the_wave_replans_the_channel_it_touched(session, monkeypatch):
    """Pilns vilnis: pēc lēmuma rinda tiek pārkārtota, un ierakstā paliek
    statusa termiņš pārplānotājam."""
    from app.models import utcnow

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    seen = {}
    real = slots.replan_channel

    def spy(session_, channel, cfg, now, rules=None):
        out = real(session_, channel, cfg, now, rules)
        seen[channel] = out
        return out

    monkeypatch.setattr(pipeline, "replan_channel", spy, raising=False)
    monkeypatch.setattr("app.slots.replan_channel", spy)
    decision = {"publish": True, "reason": "", "channels": [
        {"channel": "fb_tv3lv", "format": "link", "copy": "C", "hook_type": "fact"}]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)
    a = Article(guid="wave-1", url="https://tv3.lv/w1", canonical_url="https://tv3.lv/w1",
                title="Svarīga ziņa", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"], published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    pipeline.run_decisions(session)
    post = session.execute(select(Post).where(Post.article_id == a.id)).scalar_one()
    assert "latest" in post.extra                    # must termiņš glabājas
    assert "fb_tv3lv" in seen
