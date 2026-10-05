"""`deskhand laya eval`: how well Laya answers the guard's questions on this Mac.

It reads the labelled cases (the synthetic ones shipped in laya/cases, plus any
labelled from past runs by `deskhand laya cases`) and asks Laya each one. Per
question it measures how well yes-cases rank above no-cases (AUROC), fits one
temperature to Laya's probabilities, and finds the threshold that catches
BAR_RECALL of the yes-cases. A question passes when false alarms at that
threshold stay at or under BAR_FALSE_ALARMS; the guard is on by default only
when both pass. The fits are saved in Laya's folder for the guard to read.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from deskhand.laya.cases import Case
from deskhand.laya.client import QUESTIONS, Calibration, Fit, Question, calibrate, state_for

BAR_RECALL = 0.95
BAR_FALSE_ALARMS = 0.10
MIN_EACH = 20  # yes-cases and no-cases per question before a pass can count
TEMPERATURES = [round(0.25 * 1.15**i, 3) for i in range(30)]  # 0.25 to about 14

Scored = list[tuple[float, bool]]


class RawAnswers(Protocol):
    """What the eval needs from Laya (LayaClient is the real one)."""

    async def raw_yes(self, question: Question, state: str | dict[str, str]) -> float: ...


@dataclass
class Row:
    """One question's results, as the eval table shows them."""

    question: str
    cases: int
    fit: Fit
    ece_before: float
    ece_after: float
    ms_median: float
    ms_p95: float


def auroc(scored: Scored) -> float:
    pos = [p for p, y in scored if y]
    neg = [p for p, y in scored if not y]
    if not pos or not neg:
        return 0.0
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def _nll(scored: Scored, temperature: float) -> float:
    total = 0.0
    for p, y in scored:
        q = min(max(calibrate(p, temperature), 1e-9), 1 - 1e-9)
        total -= math.log(q if y else 1 - q)
    return total


def fit_temperature(scored: Scored) -> float:
    return min(TEMPERATURES, key=lambda t: _nll(scored, t))


def threshold_for(scored: Scored, recall: float = BAR_RECALL) -> float:
    """The highest threshold that still catches `recall` of the yes-cases."""
    pos = sorted((p for p, y in scored if y), reverse=True)
    if not pos:
        return 1.0
    return pos[max(0, math.ceil(recall * len(pos)) - 1)]


def rates(scored: Scored, threshold: float) -> tuple[float, float]:
    """(share of yes-cases caught, share of no-cases flagged) at a threshold."""
    pos = [p for p, y in scored if y]
    neg = [p for p, y in scored if not y]
    caught = sum(p >= threshold for p in pos) / len(pos) if pos else 0.0
    alarms = sum(n >= threshold for n in neg) / len(neg) if neg else 0.0
    return caught, alarms


def ece(scored: Scored, bins: int = 10) -> float:
    """Expected calibration error: how far stated probabilities are from what happened."""
    total = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        bucket = [(p, y) for p, y in scored if low <= p < high or (b == bins - 1 and p == 1.0)]
        if bucket:
            confidence = sum(p for p, _ in bucket) / len(bucket)
            accuracy = sum(y for _, y in bucket) / len(bucket)
            total += len(bucket) / len(scored) * abs(confidence - accuracy)
    return total


def assess(question: str, raw: Scored, ms: list[float]) -> Row:
    """Fit and judge one question from Laya's raw probabilities."""
    temperature = fit_temperature(raw)
    tuned = [(calibrate(p, temperature), y) for p, y in raw]
    threshold = threshold_for(tuned)
    caught, alarms = rates(tuned, threshold)
    positives = sum(y for _, y in raw)
    negatives = len(raw) - positives
    passed = (
        caught >= BAR_RECALL
        and alarms <= BAR_FALSE_ALARMS
        and min(positives, negatives) >= MIN_EACH
    )
    fit = Fit(temperature, threshold, caught, alarms, auroc(raw), positives, negatives, passed)
    p95 = sorted(ms)[max(0, math.ceil(0.95 * len(ms)) - 1)] if ms else 0.0
    return Row(question, len(raw), fit, ece(raw), ece(tuned), statistics.median(ms or [0.0]), p95)


async def measure(cases: list[Case], client: RawAnswers) -> tuple[Calibration, list[Row]]:
    """Ask Laya every case, then fit and judge each question."""
    raw: dict[str, Scored] = {name: [] for name in QUESTIONS}
    ms: dict[str, list[float]] = {name: [] for name in QUESTIONS}
    for case in cases:
        question = QUESTIONS[case.question]
        started = time.perf_counter()
        p = await client.raw_yes(question, state_for(question, case.state))
        ms[case.question].append((time.perf_counter() - started) * 1000)
        raw[case.question].append((p, case.label))
    rows = [assess(name, raw[name], ms[name]) for name in QUESTIONS if raw[name]]
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    return Calibration({row.question: row.fit for row in rows}, stamp), rows
