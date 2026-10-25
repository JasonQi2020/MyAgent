"""Task reports go where the task was created: the web UI or the Feishu chat (the reported bug)."""

import pytest

import agent_demo.tasks as tasks
from agent_demo.events import ApprovalRequest, TaskReport
from agent_demo.llm import LLMReply
from agent_demo.schedule_tools import TaskService
from agent_demo.tasks import TaskStore
from agent_demo.web_delivery import WEB_TARGET, WebDelivery
from test_im_hub import make_hub


class FakeConn:
    def __init__(self, answer=True):
        self.sent, self.asked, self.answer = [], [], answer

    async def send(self, event):
        self.sent.append(event)

    async def approve(self, request):
        self.asked.append(request)
        return self.answer


class FakeAgent:
    def __init__(self):
        self.notes = []

    def add_note(self, text):
        self.notes.append(text)


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "state" / "tasks.db")


def add(store, target, created_by, title):
    service = TaskService(store)
    service.add({"title": title, "prompt": "Report FTNT.", "cron": "0 21 * * *"}, lambda: target, created_by,
                "assistant", "weak")
    return store.list()[-1]


async def test_each_report_goes_where_its_task_was_created(store):
    hub, ch = make_hub([LLMReply(content="web report"), LLMReply(content="feishu report")])
    hub.runtime.task_service = TaskService(store)
    web = WebDelivery(hub.runtime)
    tab, tab_agent = FakeConn(), FakeAgent()
    await web.connect(tab, tab_agent)

    from agent_demo.server import start_scheduler

    web_task = add(store, WEB_TARGET, "web", "from the web UI")
    im_task = add(store, ("fake", "dm-ou_owner"), "ou_owner", "from Feishu")
    scheduler = await start_scheduler(hub.runtime, hub, web)
    try:
        assert await scheduler.runner(web_task) == "ok"
        assert await scheduler.runner(im_task) == "ok"
    finally:
        await scheduler.stop()

    web_reports = [e.text for e in tab.sent if isinstance(e, TaskReport)]
    assert web_reports == ["⏰ from the web UI (task #1)\n\nweb report"]
    assert ch.texts() == ["⏰ from Feishu (task #2)\n\nfeishu report"]  # only the Feishu task's report
    assert tab_agent.notes and "from the web UI" in tab_agent.notes[0]


async def test_old_web_tasks_are_moved_to_the_web_ui(store):
    hub, _ = make_hub([])
    hub.runtime.task_service = TaskService(store)
    old = add(store, ("feishu", "oc_owner_dm"), "web", "created in the web UI before the fix")
    keep = add(store, ("feishu", "oc_owner_dm"), "ou_owner", "created in Feishu")

    from agent_demo.server import start_scheduler

    scheduler = await start_scheduler(hub.runtime, None, WebDelivery(hub.runtime))
    await scheduler.stop()
    assert store.get(old.id).target == WEB_TARGET and store.get(keep.id).target == ("feishu", "oc_owner_dm")


async def test_no_tab_open_keeps_reports_in_an_inbox():
    web = WebDelivery(runtime=None)
    await web.deliver("⏰ report 1", "note 1")
    await web.deliver("⏰ report 2", "note 2")
    assert (tasks.STATE_DIR / "web_inbox.json").exists()

    tab, agent = FakeConn(), FakeAgent()
    await web.connect(tab, agent)
    assert [e.text for e in tab.sent] == ["⏰ report 1", "⏰ report 2"] and agent.notes == ["note 1", "note 2"]
    assert not (tasks.STATE_DIR / "web_inbox.json").exists()  # shown once

    web.disconnect(tab)
    await web.deliver("⏰ report 3", "note 3")
    assert [e.text for e in tab.sent][-1] == "⏰ report 2"  # a closed tab gets nothing more


async def test_all_open_tabs_get_the_report():
    web = WebDelivery(runtime=None)
    tabs = [(FakeConn(), FakeAgent()), (FakeConn(), FakeAgent())]
    for conn, agent in tabs:
        await web.connect(conn, agent)
    await web.deliver("⏰ r", "n")
    assert all(conn.sent[-1].text == "⏰ r" and agent.notes == ["n"] for conn, agent in tabs)


async def test_approvals_go_to_the_newest_tab_or_are_no():
    web = WebDelivery(runtime=None)
    request = ApprovalRequest("c1", "mail__send_email", {"to": "me"})
    assert await web.approve(request) is False  # no tab open: nobody can say yes
    old, new = FakeConn(), FakeConn()
    await web.connect(old, FakeAgent())
    await web.connect(new, FakeAgent())
    assert await web.approve(request) is True and new.asked == [request] and old.asked == []


def test_web_tasks_say_they_report_to_the_web_ui(store):
    service = TaskService(store)
    preview = service.preview({"cron": "0 21 * * *"}, lambda: WEB_TARGET)
    assert preview["reports_to"] == "the web UI"
    text = service.add({"title": "t", "prompt": "p", "cron": "0 21 * * *"}, lambda: WEB_TARGET, "web", "assistant", "weak")
    assert text.endswith("Reports go to the web UI.")
