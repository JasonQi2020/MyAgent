"""Enabling groups at runtime (design/im_integration.md §11): the store and the hub rules."""

import json

from agent_demo.channels.base import GroupEvent
from agent_demo.channels.groups import GroupStore
from agent_demo.llm import LLMReply
from test_im_hub import GROUP, OWNER, STRANGER, FakeChannel, call, dm, group, make_hub, run_until_approval

NEW = "oc_new_group"


# ---- GroupStore -------------------------------------------------------------


def test_store_enable_disable_and_persist(tmp_path):
    path = tmp_path / "data" / "im_groups.json"
    store = GroupStore(path)
    assert store.enable("feishu", NEW, by=OWNER, via="command", name="Team") is True
    assert store.enable("feishu", NEW, by=OWNER, via="command") is False  # already
    assert store.is_enabled("feishu", NEW) and not store.is_enabled("whatsapp", NEW)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["feishu"][NEW]["enabled_by"] == OWNER and saved["feishu"][NEW]["name"] == "Team"
    assert GroupStore(path).is_enabled("feishu", NEW)  # survives a restart
    assert [p.name for p in path.parent.iterdir()] == ["im_groups.json"]  # no temp files left

    assert store.disable("feishu", NEW) is True and store.disable("feishu", NEW) is False
    assert not GroupStore(path).is_enabled("feishu", NEW)


def test_store_with_corrupt_file(tmp_path, caplog):
    path = tmp_path / "im_groups.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="agent_demo.im"):
        store = GroupStore(path)
    assert not store.is_enabled("feishu", NEW) and "Could not read" in caplog.text


# ---- bot added / removed ----------------------------------------------------


def added(by=OWNER, chat=NEW, external=False):
    return GroupEvent("fake", chat, by, "bot_added", "Team", external)


async def test_bot_added_by_allowed_user_enables_and_welcomes():
    hub, ch = make_hub([LLMReply(content="hello group")])
    await hub.on_group_event(added())
    assert hub.groups.is_enabled("fake", NEW)
    assert ch.sent[0][0] == NEW and "Everyone in this group can read my answers" in ch.sent[0][1]
    await hub.on_message(group("hi", "g1", chat=NEW))
    await hub.drain()
    assert ch.sent[-1] == (NEW, "hello group", "g1")


async def test_bot_added_by_others_or_external_is_not_enabled(caplog):
    hub, ch = make_hub([])
    with caplog.at_level("WARNING", logger="agent_demo.im"):
        await hub.on_group_event(added(by=STRANGER))
        await hub.on_group_event(added(chat="oc_ext", external=True))
    assert not hub.groups.is_enabled("fake", NEW) and not hub.groups.is_enabled("fake", "oc_ext")
    assert ch.sent == []  # silent
    assert f"user_id={STRANGER}, who is not allowed" in caplog.text and "external group oc_ext" in caplog.text


async def test_bot_removed_disables_and_drops_sessions():
    emails = []
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")], sent_emails=emails)
    await hub.on_group_event(added())
    await run_until_approval(hub, ch, group("email me", "g1", chat=NEW))
    await hub.on_group_event(GroupEvent("fake", NEW, "", "bot_removed"))
    await hub.drain()
    assert not hub.groups.is_enabled("fake", NEW) and hub.sessions == {}
    assert emails == []  # the pending approval became "no"


# ---- /enable and /disable ---------------------------------------------------


async def test_enable_command_in_a_new_group():
    hub, ch = make_hub([LLMReply(content="answer")])
    await hub.on_message(group("question", "g1", chat=NEW))
    assert ch.sent == []  # not enabled yet
    await hub.on_message(group("/enable", "g2", chat=NEW))
    welcome = " ".join(text for _, text, _ in ch.sent)  # long texts are split into parts
    assert hub.groups.is_enabled("fake", NEW) and "enabled in this group" in welcome
    assert "read the recent messages of this group" in welcome
    assert {reply_to for _, _, reply_to in ch.sent} == {"g2"}  # threaded under the command
    await hub.on_message(group("/ENABLE", "g3", chat=NEW))
    assert ch.sent[-1][1] == "Already enabled in this group."
    await hub.on_message(group("question", "g4", chat=NEW))
    await hub.drain()
    assert ch.sent[-1][1] == "answer"


async def test_disable_command():
    hub, ch = make_hub([LLMReply(content="a")])
    await hub.on_message(group("/enable", "g1", chat=NEW))
    await hub.on_message(group("hi", "g2", chat=NEW))
    await hub.drain()
    await hub.on_message(group("/disable", "g3", chat=NEW))
    assert ch.sent[-1][1].startswith("Disabled.") and hub.sessions == {}
    assert not hub.groups.is_enabled("fake", NEW)
    await hub.on_message(group("/disable", "g4", chat=NEW))
    assert ch.sent[-1][1] == "I'm not enabled in this group."


async def test_disable_static_group_explains_env():
    hub, ch = make_hub([])
    await hub.on_message(group("/disable", "g1"))  # GROUP is in FakeChannel.allowed_chats
    assert "server settings (.env)" in ch.sent[-1][1]


async def test_group_commands_need_an_allowed_user_and_a_group():
    hub, ch = make_hub([], channel=FakeChannel(allowed_users=(OWNER,)))
    await hub.on_message(group("/enable", "g1", chat=NEW, user=STRANGER))
    assert ch.sent == [] and not hub.groups.is_enabled("fake", NEW)
    await hub.on_message(group("/enable", "g2", chat=NEW, mentioned=False))  # not addressed to the bot
    assert ch.sent == []
    await hub.on_message(dm("/enable", "d1"))
    assert ch.sent[-1] == ("dm-ou_owner", "This command only works in group chats.", None)
