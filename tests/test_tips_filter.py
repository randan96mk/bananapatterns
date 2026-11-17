"""💡 Filter: the fixture's kinds by code (chatter / promo / image never reach the model), a premium update near an
open hint's levels is not chatter, the model's sort (mocked) and code's check of its link, the fallback without it."""

import json

import pytest

from backend.tips import decode as dc
from backend.tips import filter as flt
from backend.tips import plan as pl
from tests.tips_world import cfg, posts


def test_fixture_shares_by_code():
    kinds = {}
    for p in posts():
        k = flt.code_kind(p["text"], p["has_media"])
        if k is None:
            k = "hint" if dc.looks_like_hint(dc.decode(p["text"], p["posted_at"]), p["text"]) else "follow_up?"
        kinds[k] = kinds.get(k, 0) + 1
        if p["kind"] in ("chatter", "promo", "image"):
            assert k == p["kind"], p["post_id"]
        if p["kind"] == "hint":
            assert k == "hint", p["post_id"]
    assert kinds == {"hint": 15, "follow_up?": 9, "chatter": 3, "promo": 2, "image": 1}


@pytest.mark.parametrize("p", [p for p in posts() if p["kind"] == "follow_up"], ids=lambda p: str(p["post_id"]))
def test_fixture_follow_up_actions(p):
    a = pl.followup_action(p["text"])
    for k, v in p["expect"].items():
        assert a.get(k if k != "action" else "action") == v


def test_premium_update_near_an_open_hint_is_not_chatter():
    h = {"entry": {"lo": 50, "hi": 50}, "stop": {"kind": "premium", "value": 40}, "targets": [60, 80, 100]}
    assert flt.code_kind("50 to 90+++👍👍👍", False) == "chatter"
    assert flt.code_kind("50 to 90+++👍👍👍", False, flt.open_levels([h])) is None


HINTS = [{"id": 12, "symbol": "BANKNIFTY", "strike": 50300, "opt_type": "PE", "side": "BUY", "entry": {"lo": 250, "hi": 270},
          "stop": {"kind": "premium", "value": 190}, "targets": [320, 350, 400]},
         {"id": 13, "symbol": "BANKNIFTY", "strike": 49600, "opt_type": "PE", "side": "BUY", "entry": {"lo": 50, "hi": 50},
          "stop": {"kind": "premium", "value": 40}, "targets": [60, 80, 100]}]


@pytest.fixture
def model(monkeypatch):
    answers = []
    monkeypatch.setattr(flt, "model_allowed", lambda config: True)

    def gen(model, prompt, system=None, json_mode=True, timeout=90, keep_alive="10m", options=None):
        return {"text": json.dumps(answers.pop(0)), "model": model}
    monkeypatch.setattr("backend.ai.client.generate", gen)
    return answers


def test_model_follow_up_link_checked_by_code(model):
    c = cfg()
    model.append({"kind": "follow_up", "hint_id": 13})
    assert flt.model_classify(c, "2 targets done ✅✅✅", HINTS)["hint_id"] == 13
    model.append({"kind": "follow_up", "hint_id": 13})                     # the text names 50300: code re-links to #12
    assert flt.model_classify(c, "50300 PE 2 targets done", HINTS)["hint_id"] == 12
    model.append({"kind": "follow_up", "hint_id": 99})                     # not an open id: code picks
    assert flt.model_classify(c, "90 now", HINTS)["hint_id"] == 13
    model.append({"kind": "nonsense"})
    assert flt.model_classify(c, "x", HINTS) is None


def test_code_fallback_without_the_model():
    assert flt.code_classify("Avoid.. wait", HINTS) == ("follow_up", 13)
    assert flt.code_classify("sl hit", []) == ("chatter", None)
    assert flt.code_classify("banknifty 50300 pe 2 targets done", HINTS) == ("follow_up", 12)
    assert not flt.model_allowed(cfg())                                    # the AI is off: never asked
