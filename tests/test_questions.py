import asyncio

from deskhand import questions


async def test_a_background_question_is_answered_through_the_run_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(questions, "POLL_S", 0.01)
    ask = questions.RunFolderQuestions(tmp_path, timeout_s=5)
    task = asyncio.create_task(ask("Allow it?", who="deskhand's guard"))
    for _ in range(500):  # the question shows up within a few polls
        if questions.pending(tmp_path) is not None:
            break
        await asyncio.sleep(0.01)
    asked = questions.pending(tmp_path)
    assert asked is not None and asked["who"] == "deskhand's guard"
    assert questions.answer(tmp_path, "yes")
    assert await task == "yes"
    assert questions.pending(tmp_path) is None and not (tmp_path / "answer.json").exists()
    assert not questions.answer(tmp_path, "too late")


async def test_no_answer_in_time_reads_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(questions, "POLL_S", 0.01)
    assert await questions.RunFolderQuestions(tmp_path, timeout_s=0.05)("Allow it?") == ""
    assert questions.pending(tmp_path) is None
