import pytest

from deskhand import mcp_server, sandboxes


@pytest.fixture
def shown(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.viewer, "show", lambda: calls.append("show"))
    return calls


async def test_sandbox_create_opens_the_viewer(cua, shown):
    made = await mcp_server.sandbox_create(owner="checkout-flow")
    assert made["name"].startswith("deskhand-checkout-flow-") and shown == ["show"]
    listed = await mcp_server.sandbox_list()
    assert [s["name"] for s in listed] == [made["name"]]
    assert await mcp_server.sandbox_delete(made["name"]) == f"Deleted {made['name']}."
    assert await mcp_server.sandbox_list() == []


async def test_sandbox_shell_and_screenshot(cua, shown):
    made = await mcp_server.sandbox_create()
    assert await mcp_server.sandbox_shell(made["name"], "ls") == "exit code 0\nran ls"
    image = await mcp_server.sandbox_screenshot(made["name"])
    assert image.data == b"\x89PNG fake"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"on": "cloud"}, "on must be"),
        ({"on": "mac", "sandbox": "deskhand-x"}, "doesn't take a sandbox"),
        ({"model": "gpt"}, "model must be"),
        ({"effort": "huge"}, "effort must be"),
        ({"max_steps": 0}, "must be positive"),
    ],
)
async def test_task_start_rejects_bad_arguments(kwargs, message, shown):
    with pytest.raises(ValueError, match=message):
        await mcp_server.task_start("t", **kwargs)


async def test_task_start_hands_off_to_a_background_run(monkeypatch, shown):
    started = {}

    def start(task, **kwargs):
        started.update(task=task, **kwargs)
        return "run-1"

    monkeypatch.setattr(mcp_server.tasks, "start", start)
    reply = await mcp_server.task_start("Check the page", sandbox="deskhand-a-1")
    assert reply == {"run_id": "run-1", "status": "running"}
    assert started["sandbox"] == "deskhand-a-1" and started["on"] == "sandbox"
    assert shown == []  # the run opens the viewer itself, once its sandbox is up


async def test_sandbox_limit_reaches_claude_as_an_error(cua, shown):
    for owner in ("a", "b", "c"):
        cua.names.add(f"deskhand-{owner}-000000")
    with pytest.raises(sandboxes.SandboxLimitError):
        await mcp_server.sandbox_create()
