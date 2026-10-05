import json
import os
import stat
from types import SimpleNamespace

import httpx2
import pytest

from deskhand.laya import cases, evaluate, server
from deskhand.laya.client import (
    HARD_TO_UNDO,
    PAGE_ORDERS,
    Calibration,
    Fit,
    LayaClient,
    action_state,
    calibrate,
)
from deskhand.laya.server import LayaUnavailable
from deskhand.models import MODELS, ModelAlias
from deskhand.runlog import RunLog


def test_server_settings_keep_laya_on_this_mac_with_a_key():
    env = server.server_env({"PATH": "/bin"})
    assert (
        env["LAYA_HOST"] == "127.0.0.1"
        and env["LAYA_MODELS"] == "english"
        and env["PATH"] == "/bin"
    )
    key_file = server.laya_dir() / "key"
    assert env["LAYA_API_KEY"] == key_file.read_text() and len(env["LAYA_API_KEY"]) > 30
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert server.key() == env["LAYA_API_KEY"]  # made once
    assert not server.running() and server.idle_seconds() == float("inf")
    server.touch()
    assert server.idle_seconds() < 5


def mock_client(handler, **kwargs):
    return LayaClient(
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), start=False, **kwargs
    )


async def test_a_question_goes_out_as_a_two_option_choice_and_comes_back_calibrated():
    sent = []

    def handler(request):
        sent.append(request)
        body = json.loads(request.content)
        (name,) = body["questions"]
        return httpx2.Response(
            200, json={"answers": {name: {"probabilities": {"A": 0.8, "B": 0.2}}}}
        )

    calibration = Calibration({HARD_TO_UNDO.name: Fit(temperature=2.0)})
    client = mock_client(handler, calibration=calibration)
    p = await client.yes(HARD_TO_UNDO, "Click.")
    body = json.loads(sent[0].content)
    assert (
        sent[0].url.path == "/v1/systemone"
        and sent[0].headers["Authorization"] == f"Bearer {server.key()}"
    )
    assert body["questions"]["hard_to_undo"]["criteria"]["A"] == HARD_TO_UNDO.yes
    assert p == pytest.approx(calibrate(0.8, 2.0)) and 0.5 < p < 0.8  # a softer 0.8
    assert await client.raw_yes(PAGE_ORDERS, {"text": "hi"}) == 0.8
    await client.aclose()


async def test_a_failure_fails_fast_for_a_while():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(500)

    client = mock_client(handler)
    with pytest.raises(LayaUnavailable):
        await client.yes(HARD_TO_UNDO, "Click.")
    with pytest.raises(LayaUnavailable, match="a moment ago"):
        await client.yes(HARD_TO_UNDO, "Click.")
    assert len(calls) == 1


def test_action_state_and_calibration_file():
    assert (
        action_state(
            {
                "app": "Mail",
                "window": "Inbox",
                "action": "press key",
                "target": 'text area "Body"',
                "keys": "cmd+enter",
            }
        )
        == 'In Mail, window "Inbox": press key on text area "Body" pressing cmd+enter.'
    )
    assert not Calibration().passed
    Calibration({"hard_to_undo": Fit(passed=True), "page_orders": Fit(passed=True)}, "now").save()
    loaded = Calibration.load()
    assert loaded.passed and loaded.measured_at == "now"


def test_measurement_math():
    scored = [(0.9, True), (0.8, True), (0.7, False), (0.2, False)]
    assert evaluate.auroc(scored) == 1.0
    assert evaluate.threshold_for(scored, 0.95) == 0.8
    assert evaluate.rates(scored, 0.75) == (1.0, 0.0)
    overconfident = [(0.99, True), (0.99, False), (0.01, False), (0.01, True)] * 5
    assert evaluate.fit_temperature(overconfident) > 3  # softened
    assert evaluate.ece([(1.0, True), (0.0, False)]) == 0.0


def test_a_question_passes_only_with_enough_cases_and_few_false_alarms():
    good = [(0.9, True)] * 20 + [(0.1, False)] * 20
    row = evaluate.assess("hard_to_undo", good, [50.0])
    assert row.fit.passed and row.fit.false_alarms == 0.0
    assert not evaluate.assess(
        "hard_to_undo", good[:10] + good[20:], [50.0]
    ).fit.passed  # 10 yes-cases
    noisy = [(0.9, True)] * 20 + [(0.95, False)] * 20
    assert not evaluate.assess("hard_to_undo", noisy, [50.0]).fit.passed


async def test_measure_asks_every_case():
    class Fixed:
        async def raw_yes(self, question, state):
            return 0.9 if "Send" in str(state) or "AI" in str(state) else 0.1

    synthetic = cases.synthetic_cases()
    assert {c.question for c in synthetic} == {"hard_to_undo", "page_orders"} and len(
        synthetic
    ) > 100
    calibration, rows = await evaluate.measure(synthetic, Fixed())
    assert {r.question for r in rows} == {"hard_to_undo", "page_orders"}
    assert calibration.fit(HARD_TO_UNDO).positives == 40


async def test_cases_from_runs_are_collected_once_and_labelled_by_claude(runs_root):
    log = RunLog(runs_root, "t", "mac", "m", "medium")
    log.event(
        "tool",
        name="get_window_state",
        arguments={},
        text=(
            '- [0] AXWindow "Shop"\n  - AXStaticText = "Buy now"\n'
            '  - AXStaticText = "Free delivery"'
        ),
        is_error=False,
        images=[],
    )
    log.event(
        "guard",
        question="hard_to_undo",
        state={"app": "Mail", "action": "click"},
        probability=0.9,
        decision="declined",
    )
    log.event(
        "guard",
        question="page_orders",
        state={"text": "summary"},
        probability=0.1,
        decision="allowed",
    )
    rows, added = cases.collect(runs_root)
    assert added == 2 and {r["question"] for r in rows} == {"hard_to_undo", "page_orders"}
    cases.save(rows)
    assert cases.collect(runs_root)[1] == 0

    class Claude:
        def __init__(self):
            self.messages = self

        async def create(self, **params):
            n = params["messages"][0]["content"].count("Case ")
            text = json.dumps({"labels": [True] * n})
            usage = SimpleNamespace(
                input_tokens=1000,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)

    cost = await cases.label(rows, Claude(), MODELS[ModelAlias.opus])  # type: ignore[arg-type]
    assert cost > 0 and all(r["label"] is True for r in rows)
    cases.save(rows)
    assert len(cases.labelled_run_cases()) == 2
