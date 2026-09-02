"""Claude izsaukuma parametri: effort tikai modeļiem, kas to pieņem,
kešojams sistēmas prompts un griesti ar rezervi domāšanai."""
from app import claude


def test_effort_only_for_models_that_accept_it():
    assert claude.params("claude-sonnet-5", "medium") == {"output_config": {"effort": "medium"}}
    assert claude.params("claude-opus-5", "low") == {"output_config": {"effort": "low"}}
    # Haiku 4.5 un Sonnet 4.5 parametru noraida ar 400 — tiem nesūtām neko
    assert claude.params("claude-haiku-4-5-20251001") == {}
    assert claude.params("claude-sonnet-4-5") == {}
    assert claude.params("") == {}


def test_thinking_models_get_headroom_in_max_tokens():
    assert claude.max_tokens_for("claude-haiku-4-5-20251001", 300) == 300
    assert claude.max_tokens_for("claude-sonnet-5", 300) == 2000
    assert claude.max_tokens_for("claude-opus-5", 1500) == 4500


def test_system_prompt_is_a_cacheable_block():
    block = claude.cached_system("Tu esi redaktors.")
    assert block == [{"type": "text", "text": "Tu esi redaktors.",
                      "cache_control": {"type": "ephemeral"}}]


def test_decision_call_caches_system_and_sets_effort(session, monkeypatch):
    """Lēmuma izsaukums: sistēmas prompts kešā, effort modelim, kas to
    pieņem, un griesti, kuros ietilpst arī domāšana."""
    import anthropic

    from app import config, credentials, decide
    from app.models import Article
    from app.rules_engine import Verdict

    captured: dict = {}

    class FakeUsage:
        input_tokens = 100
        output_tokens = 50
        cache_read_input_tokens = 80

    class FakeBlock:
        type = "tool_use"
        input = {"publish": False, "reason": "tests"}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return type("R", (), {"content": [FakeBlock()], "usage": FakeUsage()})()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(credentials, "get",
                        lambda key, session=None: "sk-x" if key == "anthropic_api_key" else "")
    monkeypatch.setattr(config, "AI_MODEL_FAST", "claude-sonnet-5")
    monkeypatch.setattr(decide, "validate_decision", lambda d: True)

    article = Article(guid="cp-1", url="https://tv3.lv/x", canonical_url="https://tv3.lv/x",
                      title="T", lead="L", section="news", editor_status="can")
    session.add(article)
    session.flush()
    decide.call_claude(article, {"fb_page": Verdict("eligible")}, session)

    assert captured["model"] == "claude-sonnet-5"
    assert captured["output_config"] == {"effort": "medium"}
    assert captured["max_tokens"] >= 4000
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["tool_choice"]["name"] == "record_decision"
