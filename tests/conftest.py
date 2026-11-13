from pathlib import Path

import pytest

from decisionforge.config import load_workflow
from decisionforge.engine import Engine
from decisionforge.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    store = Store(str(tmp_path / "decisions.db"))
    for path in (ROOT / "configs").glob("*.yaml"):
        store.publish(load_workflow(path), clock())
    return store


@pytest.fixture
def engine(store, clock):
    return Engine(store, clock)


@pytest.fixture
def loan():
    return {"applicant_id": "app-1", "age": 30, "income": 60000, "amount": 25000, "consent": True}
