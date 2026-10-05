from datetime import datetime, timedelta

import pytest

from deskhand import sandboxes


async def test_create_names_by_owner_and_registers(cua):
    record = await sandboxes.create("Login Test #1")
    assert record.name.startswith("deskhand-login-test-1-")
    assert record.viewer_url.endswith(record.name)
    assert [r.name for r in sandboxes.records()] == [record.name]
    assert await sandboxes.running() == [record.name]  # cua's other sandboxes are ignored


async def test_the_fourth_sandbox_is_refused(cua):
    for owner in ("a", "b", "c"):
        cua.names.add(f"deskhand-{owner}-000000")
    with pytest.raises(sandboxes.SandboxLimitError, match="3 sandboxes are already running"):
        await sandboxes.create("d")


async def test_shell_and_screenshot_mark_the_sandbox_used(cua):
    record = await sandboxes.create("t")
    stale = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
    with sandboxes._registry() as registry:
        registry[record.name].last_used = stale
    text, failed = await sandboxes.shell(record.name, "uname -a")
    assert (text, failed) == ("exit code 0\nran uname -a", False)
    assert await sandboxes.screenshot(record.name) == b"\x89PNG fake"
    (fresh,) = sandboxes.records()
    assert fresh.last_used > stale


async def test_sweep_deletes_idle_and_forgets_gone_sandboxes(cua):
    idle = await sandboxes.create("idle")
    busy = await sandboxes.create("busy")
    long_ago = (datetime.now().astimezone() - timedelta(minutes=45)).isoformat(timespec="seconds")
    with sandboxes._registry() as registry:
        registry[idle.name].last_used = long_ago
        registry["deskhand-gone-000000"] = sandboxes.SandboxRecord(
            "deskhand-gone-000000", "x", long_ago, long_ago
        )
    assert await sandboxes.sweep() == [idle.name]
    assert [r.name for r in sandboxes.records()] == [busy.name]
    assert idle.name not in cua.names


def test_shell_output_is_cut(monkeypatch):
    monkeypatch.setattr(sandboxes, "SHELL_OUTPUT_CHARS", 20)
    text, failed = sandboxes.format_shell(2, "x" * 50, "boom")
    assert failed and text.startswith("exit code 2\nxxxxxxx")
    assert "more characters cut" in text


async def test_viewer_url_is_renewed_and_saved(cua):
    record = await sandboxes.create("v")
    with sandboxes._registry() as registry:
        registry[record.name].viewer_url = "old"
    assert await sandboxes.viewer_url(record.name) == f"http://127.0.0.1:1/viewer/#{record.name}"
    (saved,) = sandboxes.records()
    assert saved.viewer_url.endswith(record.name)
