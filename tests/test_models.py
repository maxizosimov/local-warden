"""Local model jobs and their rule fallbacks."""
from warden import models


def test_pick_falls_back_to_cheapest_without_a_model(hub):
    r = hub.submit_text("buy a chair")
    assert r["product"]["sku"] == "CHR-200" and r["picked_by"] == "rules · cheapest" and r["parser"] == "rules"


def test_local_model_picks_and_is_credited(hub, monkeypatch):
    def fake_chat(system, user, timeout=60):
        if system == models.PICK_SYSTEM:
            assert "1. Basic Task Chair" in user and "2. Ergonomic Mesh Office Chair" in user
            return "2"
        return None  # parsing falls back to rules
    monkeypatch.setattr(models, "local_chat", fake_chat)
    r = hub.submit_text("buy a chair, my back hurts")  # two chairs match; the model chooses
    assert r["product"]["sku"] == "CHR-100" and r["picked_by"] == models.local_label()


def test_bad_model_answer_is_ignored(monkeypatch):
    from warden.merchants import REGISTRY
    chairs = [REGISTRY["demo-store"].get_product(s) for s in ("CHR-200", "CHR-100")]
    monkeypatch.setattr(models, "local_chat", lambda *a, **k: "I'd go with number 7")
    assert models.pick_product("x", chairs) == (0, "rules · cheapest")


def test_roles_cover_the_whole_flow():
    tasks = [r["task"] for r in models.ROLES]
    assert tasks[0] == "Understand your request" and tasks[-1] == "Approve and pay"
    assert [r["by"] for r in models.ROLES if r["task"] == "Allow, ask or deny"] == ["policy engine"]
