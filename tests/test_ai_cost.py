"""Claude izsaukumu ekonomija: neatkārtot to, kas jau ir izlemts, un
nesūtīt pilnā cenā to, kas katram rakstam ir vienāds."""
from app import config, decide, play
from app.models import Article, DecisionLog, utcnow
from app.rules_engine import Verdict

DECISION = {"publish": True, "score": 0.8, "reason": "der",
            "section": "news", "labels": [], "sensitivity": [],
            "channels": [{"channel": "fb_main", "format": "link", "copy": "teksts"}]}
VERDICTS = {"fb_main": Verdict("eligible", "ok")}


def _article(session, **kw):
    kw.setdefault("feed_name", "tv3")
    a = Article(guid="g1", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="Virsraksts", section="news",
                editor_status="can", published_at=utcnow(), raw_json={}, **kw)
    session.add(a)
    session.flush()
    return a


def test_retry_reuses_the_decision_instead_of_asking_again(session, monkeypatch):
    """Raksts, kam rinda bija pilna, atgriežas vēl astoņas reizes. Katra no
    tām bija pilns jauns izsaukums, kaut atbilde bija tā pati."""
    a = _article(session)
    calls = []

    def fake(article, verdicts, sess):
        calls.append(article.id)
        return DECISION

    monkeypatch.setattr(decide, "call_claude", fake)
    first = decide.decide(a, VERDICTS, session)
    assert first["score"] == 0.8 and calls == [a.id]

    # otrais mēģinājums: izsaukuma vairs nav, lēmums tas pats
    again = decide.decide(a, VERDICTS, session)
    assert again == first and calls == [a.id]
    logs = session.query(DecisionLog).all()
    assert [r.reused for r in logs] == [1]


def test_a_changed_channel_set_forces_a_fresh_decision(session, monkeypatch):
    """Ja kanāls pa to laiku aizvēries vai atvēries, vecā atbilde vairs neder."""
    a = _article(session)
    calls = []
    monkeypatch.setattr(decide, "call_claude",
                        lambda art, v, s: (calls.append(1), DECISION)[1])
    decide.decide(a, VERDICTS, session)
    decide.decide(a, {**VERDICTS, "x_main": Verdict("eligible", "ok")}, session)
    assert len(calls) == 2


def test_the_static_guide_is_not_in_the_paid_user_message(session, monkeypatch):
    """Formātu rokasgrāmata ir vienāda katram rakstam, tāpēc tā pieder
    kešotajam sistēmas promptam, ne lietotāja ziņai."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session)
    prompt = decide.build_user_prompt(a, VERDICTS, {"fb_main": {}}, session)
    assert "Formātu izvēle. Noklusējums" not in prompt
    assert "Formātu izvēle. Noklusējums" in decide.FORMAT_GUIDE
    assert len(decide.FORMAT_GUIDE) > 4000     # tik daudz katrā izsaukumā ietaupīts
    assert "Virsraksts: Virsraksts" in prompt  # mainīgais paliek lietotāja ziņā


def test_play_guards_run_before_the_expensive_call(session, monkeypatch):
    """Žurnālā katrs kataloga nosaukums vispirms maksāja pilnu izsaukumu un
    tikai tad uzzināja, ka dienas limits jau izlietots. Sargiem jābūt priekšā."""
    from app import pipeline, play

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, feed_name=play.FEED_NAME)
    a.raw_json = {"_play": {"genres": ["Drāmas"], "url": a.url}}
    session.flush()
    calls = []
    monkeypatch.setattr(decide, "call_claude",
                        lambda art, v, s: (calls.append(1), DECISION)[1])
    # Play izslēgts -> allowed_now saka nē, un izsaukumam nav jānotiek
    monkeypatch.setattr(play, "allowed_now",
                        lambda *a, **k: (False, "dienas limits izlietots"))
    monkeypatch.setattr(pipeline, "evaluate_all", lambda art, now: VERDICTS)
    pipeline.run_decisions(session, limit=5)
    assert calls == []
    # nosaukums nav pazudis: tas guļ līdz nākamajai reizei, bez soda punktiem
    assert a.decided_at is None
    assert (a.raw_json or {}).get("_decide_retries") is None
    assert (a.raw_json or {}).get("_decide_retry_after")


def test_cache_share_counts_the_cached_tokens_too(session):
    """API input_tokens NEIETVER kešoto daļu, tāpēc dalot ar to, kešs
    izskatījās daudzkārt sliktāks, nekā ir."""
    from app import diagnostics

    session.add(DecisionLog(article_id=1, model="claude-sonnet-5",
                            input_tokens=3000, output_tokens=600, cached_tokens=7000))
    session.flush()
    out = diagnostics._ai_cost(session)
    assert out["total_input"] == 10000
    assert out["cache_pct"] == 70
