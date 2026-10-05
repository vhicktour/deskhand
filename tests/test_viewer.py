import fcntl

import pytest

from deskhand import sandboxes, viewer
from deskhand.paths import viewer_lock_file


@pytest.fixture(autouse=True)
def let_go():
    yield
    viewer.release()


def hold_lock():
    """Another viewer process, as far as the lock can tell."""
    viewer_lock_file().parent.mkdir(parents=True, exist_ok=True)
    lock = viewer_lock_file().open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return lock


def register(name="deskhand-t-170000"):
    stamp = "2026-10-05T17:00:00+00:00"
    with sandboxes._registry() as registry:
        registry[name] = sandboxes.SandboxRecord(name, "t", stamp, stamp)


def test_only_one_viewer_holds_the_window():
    assert not viewer.running()
    other = hold_lock()
    assert viewer.running() and not viewer.claim()
    other.close()
    assert viewer.claim() and viewer.running()
    viewer.release()
    assert not viewer.running()


def test_show_starts_a_viewer_only_when_none_is_up(monkeypatch):
    started = []
    monkeypatch.setattr(viewer.subprocess, "Popen", lambda args, **kwargs: started.append(args))
    other = hold_lock()
    viewer.show()
    assert started == []
    other.close()
    viewer.show()
    assert [args[1:] for args in started] == [["-m", "deskhand.viewer"]]


def test_with_nothing_to_show_the_viewer_steps_down():
    assert viewer.claim()
    assert viewer.step_down() is True
    assert not viewer.running()  # free for the next viewer


def test_a_sandbox_made_meanwhile_keeps_the_viewer():
    assert viewer.claim()
    register()
    assert viewer.step_down() is False
    assert viewer.running()  # still held by this viewer, so a new one is turned away


def test_a_viewer_that_took_over_meanwhile_shows_the_sandbox(monkeypatch):
    assert viewer.claim()
    newcomer = []

    def records():  # a new viewer grabs the lock while this one looks
        newcomer.append(hold_lock())
        return [object()]

    monkeypatch.setattr(viewer.sandboxes, "records", records)
    assert viewer.step_down() is True
    newcomer[0].close()
