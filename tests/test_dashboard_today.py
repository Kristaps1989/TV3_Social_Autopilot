"""Pārskata «Šodien publicēti» skaita Rīgas kalendāro dienu, ne pēdējās 24 h."""
from datetime import datetime

from app import config, main
from app.models import Article, Post


def test_today_count_is_the_riga_calendar_day(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = Article(guid="td-1", url="https://tv3.lv/td", canonical_url="https://tv3.lv/td",
                title="Ziņa", section="news")
    session.add(a)
    session.flush()
    now = datetime(2026, 9, 3, 6, 50)                  # 09:50 Rīgā
    times = {
        "vakar 23:00 Rīgā": datetime(2026, 9, 2, 20, 0),   # ārpus šodienas
        "šodien 01:00 Rīgā": datetime(2026, 9, 2, 22, 0),  # jau šodien
        "šodien 08:24 Rīgā": datetime(2026, 9, 3, 5, 24),
    }
    for t in times.values():
        session.add(Post(article_id=a.id, channel="fb_tv3lv", format="link", copy="c",
                         state="published", published_at=t, scheduled_at=t))
    session.commit()
    assert main.riga_day_start(now) == datetime(2026, 9, 2, 21, 0)

    monkeypatch.setattr(main, "utcnow", lambda: now)
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    body = client.get("/").text
    assert "Šodien publicēti: 2 /" in body
