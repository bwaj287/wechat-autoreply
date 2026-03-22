#!/usr/bin/env python3

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

from wechat_autoreply.capture_cleanup import delete_capture_snapshots_older_than
from wechat_autoreply.config_store import default_config
from wechat_autoreply.orchestrator import AutoReplyRunner, choose_inbound_text
from wechat_autoreply.state_store import default_state
from wechat_autoreply.wechat_ui import find_chat


class MemoryStore:
    def __init__(self) -> None:
        self.config = default_config()
        self.config["enabled"] = True
        self.state = default_state()
        self.events: list[dict] = []

    def load_config(self):
        return copy.deepcopy(self.config)

    def load_state(self):
        return copy.deepcopy(self.state)

    def save_state(self, state):
        self.state = copy.deepcopy(state)

    def append_event(self, event_type, **payload):
        self.events.append({"type": event_type, **payload})


class FakeVision:
    def __init__(self, values):
        self.values = list(values)

    def unread_signal(self):
        value = self.values.pop(0) if self.values else False
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(int(value)) if value else ""
        return "1" if value else ""

    def check_unread_dot(self):
        return bool(self.unread_signal())


class FakeIdle:
    def __init__(self, value):
        self.value = value

    def get_idle_time_seconds(self):
        return self.value


class FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def generate_reply(self, contact: str, inbound_text: str) -> str:
        self.calls.append((contact, inbound_text))
        return self.reply


class MappingLLM:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    def generate_reply(self, contact: str, inbound_text: str) -> str:
        self.calls.append((contact, inbound_text))
        return self.mapping[contact]


class PairMappingLLM:
    def __init__(self, mapping: dict[tuple[str, str], str]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    def generate_reply(self, contact: str, inbound_text: str) -> str:
        self.calls.append((contact, inbound_text))
        return self.mapping[(contact, inbound_text)]


class FakeUI:
    def __init__(self, probes):
        self.probes = list(probes)
        self.calls: list = []

    def activate_wechat(self):
        self.calls.append("activate")

    def hide_wechat(self):
        self.calls.append("hide")

    def probe(self, select_chat=None, sleep_after_click=1.0):
        self.calls.append(("probe", select_chat))
        return copy.deepcopy(self.probes.pop(0))

    def focus_input_box(self, probe):
        self.calls.append("focus_input")

    def paste_text(self, text):
        self.calls.append(("paste", text))

    def send_message(self):
        self.calls.append("send")


def run_happy_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "shawn", "preview": "你明天有空吗", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "你明天有空吗", "latestOutbound": "好的"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "你明天有空吗", "latestOutbound": "好的"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "你明天有空吗", "latestOutbound": "有空，怎么啦？"},
            },
        ]
    )
    clock = {"now": 1000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("有空，怎么啦？"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    assert store.state["pending"]["contact"] == "shawn"

    clock["now"] = 1305.0
    second = runner.tick()
    assert second["status"] == "sent", second
    assert store.state["pending"] is None
    assert ("paste", "有空，怎么啦？") in fake_ui.calls
    assert "send" in fake_ui.calls


def run_manual_reply_cancel() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "王哥", "preview": "回到学校了吗", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {"latestInbound": "回到学校了吗", "latestOutbound": "昨晚到的"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {"latestInbound": "回到学校了吗", "latestOutbound": "我等会回你"},
            },
        ]
    )
    clock = {"now": 2000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("好，正准备睡"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    clock["now"] = 2305.0
    second = runner.tick()
    assert second["status"] == "cancelled", second
    assert second["reason"] == "manual_reply_detected", second
    assert store.state["pending"] is None
    assert "send" not in fake_ui.calls


def run_bottom_green_bubble_cancels_pending_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "在吗", "time": "18:26", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {
                    "latestInbound": "在吗",
                    "latestOutbound": "昨晚到的",
                    "inbound": [{"text": "在吗", "top": 0.4}],
                    "outbound": [{"text": "昨晚到的", "top": 0.32}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "visibleChats": [{"name": "Darren", "preview": "在吗", "time": "18:26", "unread": False}],
                "chatPanel": {
                    "latestInbound": "在吗",
                    "latestOutbound": "昨晚到的",
                    "inbound": [{"text": "在吗", "top": 0.42}],
                    "outbound": [{"text": "昨晚到的", "top": 0.66}],
                },
            },
        ]
    )
    clock = {"now": 2050.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("等会回你"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 2355.0
    second = runner.tick()
    assert second["status"] == "cancelled", second
    assert second["reason"] == "manual_reply_detected", second
    assert store.state["pending"] is None
    assert "send" not in fake_ui.calls


def run_old_outbound_before_inbound_is_not_manual_reply_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "加我好友", "time": "14:00", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {
                    "latestInbound": "加我好友",
                    "latestOutbound": "干嘛",
                    "inbound": [{"text": "加我好友", "top": 0.54}],
                    "outbound": [{"text": "干嘛", "top": 0.22}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "加我好友", "time": "14:00", "unread": False}],
                "chatPanel": {
                    "latestInbound": "加我好友",
                    "latestOutbound": "干嘛",
                    "inbound": [{"text": "加我好友", "top": 0.54}],
                    "outbound": [{"text": "干嘛", "top": 0.22}],
                },
            },
        ]
    )
    clock = {"now": 2400.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("行，我搜一下你。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
        dry_run=True,
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 2705.0
    second = runner.tick()
    assert second["status"] == "dry_run_sent", second
    assert not any(event["type"] == "pending_cancelled" for event in store.events), store.events


def run_multi_queue_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {"name": "Darren", "preview": "链接来了", "unread": True},
                    {"name": "May", "preview": "必须面对面", "unread": True},
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {"latestInbound": "链接来了", "latestOutbound": "卡住了"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "必须面对面", "latestOutbound": "本地吗"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "visibleChats": [{"name": "Darren", "preview": "链接来了", "unread": False}],
                "chatPanel": {"latestInbound": "链接来了", "latestOutbound": "卡住了"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "visibleChats": [{"name": "Darren", "preview": "链接来了", "unread": False}],
                "chatPanel": {"latestInbound": "链接来了", "latestOutbound": "收到链接了，我看看。"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "visibleChats": [{"name": "May", "preview": "必须面对面", "unread": False}],
                "chatPanel": {"latestInbound": "必须面对面", "latestOutbound": "本地吗"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "visibleChats": [{"name": "May", "preview": "必须面对面", "unread": False}],
                "chatPanel": {"latestInbound": "必须面对面", "latestOutbound": "行，那约个时间见面吧。"},
            },
        ]
    )
    clock = {"now": 3000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=MappingLLM({"Darren": "收到链接了，我看看。", "May": "行，那约个时间见面吧。"}),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "drafts_saved", first
    assert first["contacts"] == ["Darren", "May"], first
    assert len(store.state["pending_queue"]) == 2
    assert store.state["pending"]["contact"] == "Darren"

    clock["now"] = 3305.0
    second = runner.tick()
    assert second["status"] == "sent", second
    assert second["contact"] == "Darren", second
    assert len(store.state["pending_queue"]) == 1
    assert store.state["pending"]["contact"] == "May"

    clock["now"] = 3606.0
    third = runner.tick()
    assert third["status"] == "sent", third
    assert third["contact"] == "May", third
    assert store.state["pending"] is None
    assert store.state["pending_queue"] == []


def run_follow_up_claim_second_pass_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "链接来了", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {"latestInbound": "链接来了", "latestOutbound": "卡住了"},
            },
            {
                "status": "ok",
                "visibleChats": [
                    {"name": "Darren", "preview": "链接来了", "unread": True},
                    {"name": "May", "preview": "必须面对面", "unread": True},
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "必须面对面", "latestOutbound": "本地吗"},
            },
        ]
    )
    clock = {"now": 3500.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=MappingLLM({"Darren": "收到链接了，我看看。", "May": "行，那约个时间见面吧。"}),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "drafts_saved", first
    assert first["contacts"] == ["Darren", "May"], first
    assert [item["contact"] for item in store.state["pending_queue"]] == ["Darren", "May"]
    assert any(event["type"] == "claim_follow_up_candidates" for event in store.events)


def run_history_marker_trim_path() -> None:
    panel = {
        "inbound": [
            {"text": "Yesterday 20:39", "top": 0.15},
            {"text": "今天有湖人比赛吗", "top": 0.29},
            {"text": "Haode", "top": 0.83},
        ]
    }
    assert choose_inbound_text(panel, "") == "Haode"
    multiline_panel = {"latestInbound": "Yesterday 20:39\n今天有湖人比赛吗\nHaode"}
    assert choose_inbound_text(multiline_panel, "Haode") == "Haode"


def run_preview_matching_outbound_is_not_inbound_path() -> None:
    panel = {
        "latestInbound": "",
        "latestOutbound": "哈哈行，收到你的消息心情瞬间变好啦！我也就随便聊聊，没啥事。你今天过得咋样？有没有吃好吃的？",
    }
    preview = "没啥事。你今天过得咋样？有没有吃好吃的？"
    assert choose_inbound_text(panel, preview) == ""


def run_latest_message_refresh_path() -> None:
    store = MemoryStore()
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Barrys", "preview": "为啥", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "为啥", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "", "latestOutbound": "哈哈，收到你的表情了。"},
            },
        ]
    )
    clock = {"now": 4000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, True, False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=PairMappingLLM(
            {
                ("Barrys", "为啥"): "因为还没到时间呀。",
                ("Barrys", "[表情]"): "哈哈，收到你的表情了。",
            }
        ),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    assert store.state["pending"]["inbound_text"] == "为啥"

    clock["now"] = 4050.0
    second = runner.tick()
    assert second["status"] == "pending_wait_delay", second
    assert len(store.state["pending_queue"]) == 1
    assert store.state["pending"]["contact"] == "Barrys"
    assert store.state["pending"]["inbound_text"] == "为啥"
    assert store.state["pending"]["draft_text"] == "因为还没到时间呀。"

    clock["now"] = 4355.0
    third = runner.tick()
    assert third["status"] == "pending_refreshed", third
    assert store.state["pending"]["inbound_text"] == "[表情]"
    assert store.state["pending"]["draft_text"] == "哈哈，收到你的表情了。"

    clock["now"] = 4660.0
    fourth = runner.tick()
    assert fourth["status"] == "sent", fourth
    assert store.state["pending"] is None
    assert any(event["type"] == "pending_refreshed_latest" for event in store.events)


def run_no_claim_sweep_while_pending_wait_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "shawn", "preview": "hi", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "hi", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 9050.0
    second = runner.tick()
    assert second["status"] == "pending_wait_delay", second
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        ("probe", "shawn"),
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_pending_menu_flicker_does_not_trigger_claim_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    pending = {
        "contact": "shawn",
        "inbound_text": "hi",
        "message_time": "10:00",
        "inbound_fingerprint": "fp-shawn",
        "draft_text": "yo",
        "created_at": 8_900.0,
        "due_at": 9_500.0,
        "outbound_snapshot": "",
        "active_chat_title": "shawn",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    store.state["last_claim_menu_signal"] = "1"
    store.state["last_menu_signal"] = "1"
    fake_ui = FakeUI([])
    clock = {"now": 9_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "pending_wait_delay", first
    assert store.state["last_claim_menu_signal"] == "1"

    clock["now"] = 9_020.0
    second = runner.tick()
    assert second["status"] == "pending_wait_delay", second
    assert store.state["last_claim_menu_signal"] == "1"
    assert fake_ui.calls == [], fake_ui.calls
    assert not any(event["type"] == "claim_candidates" for event in store.events), store.events


def run_queue_claims_on_menu_rising_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "shawn", "preview": "hi", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "hi", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "May", "preview": "在吗", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "在吗", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9200.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([1, 1, 2]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=MappingLLM({"shawn": "yo", "May": "在呢"}),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn"]

    clock["now"] = 9220.0
    second = runner.tick()
    assert second["status"] == "pending_wait_delay", second
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn"]

    clock["now"] = 9240.0
    third = runner.tick()
    assert third["status"] == "pending_wait_delay", third
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn", "May"]
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        ("probe", "shawn"),
        ("probe", None),
        "hide",
        "activate",
        ("probe", None),
        ("probe", "May"),
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_queue_claims_while_pending_after_sweep_interval_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 30
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "shawn", "preview": "hi", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "shawn",
                "chatPanel": {"latestInbound": "hi", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "May", "preview": "在吗", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "在吗", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_500.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, True, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=MappingLLM({"shawn": "yo", "May": "在呢"}),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn"]

    clock["now"] = 9_520.0
    second = runner.tick()
    assert second["status"] == "pending_wait_delay", second
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn"]

    clock["now"] = 9_532.0
    third = runner.tick()
    assert third["status"] == "pending_wait_delay", third
    assert [item["contact"] for item in store.state["pending_queue"]] == ["shawn"]
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        ("probe", "shawn"),
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_stale_pending_gc_path() -> None:
    store = MemoryStore()
    store.config["pending_stale_ttl_seconds"] = 30
    store.config["roster_sweep_interval_seconds"] = 9999
    stale_item = {
        "contact": "Barrys",
        "inbound_text": "hi",
        "message_time": "10:00",
        "inbound_fingerprint": "fp1",
        "draft_text": "yo",
        "created_at": 9500.0,
        "due_at": 9550.0,
        "outbound_snapshot": "",
        "active_chat_title": "Barrys",
    }
    store.state["pending_queue"] = [copy.deepcopy(stale_item)]
    store.state["pending"] = copy.deepcopy(stale_item)
    fake_ui = FakeUI([])
    clock = {"now": 9600.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "idle_wait", result
    assert store.state["pending"] is None
    assert store.state["pending_queue"] == []
    assert any(
        event["type"] == "pending_gc_removed" and event.get("removed_count") == 1 for event in store.events
    ), store.events
    assert fake_ui.calls == [], fake_ui.calls


def run_unknown_menu_signal_does_not_claim_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 30
    fake_ui = FakeUI([])
    clock = {"now": 13_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision(["?"]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "idle_wait", result
    assert fake_ui.calls == [], fake_ui.calls


def run_non_whitelist_unread_cleared_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {"name": "Sara", "preview": "在吗", "unread": True},
                    {"name": "Official Acco...", "preview": "[4] 青春杭州", "unread": True},
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Sara",
                "chatPanel": {"latestInbound": "在吗", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Official Acco...",
                "chatPanel": {"latestInbound": "[4] 青春杭州", "latestOutbound": ""},
            },
        ]
    )
    clock = {"now": 9100.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "no_candidate", first
    assert store.state["pending"] is None
    assert store.state["last_menu_unread"] is True
    assert ("probe", "Sara") in fake_ui.calls
    assert ("probe", "Official Acco...") in fake_ui.calls
    assert any(event["type"] == "non_whitelist_unread_cleared" for event in store.events)


def run_active_whitelist_chat_claim_without_unread_badge_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "晚上打不打守望先锋", "time": "19:51", "unread": False}],
                "chatPanel": {
                    "latestInbound": "晚上打不打守望先锋\n我们有四个人",
                    "latestOutbound": "加了",
                    "inbound": [
                        {"text": "晚上打不打守望先锋", "top": 0.55},
                        {"text": "我们有四个人", "top": 0.64},
                    ],
                    "outbound": [{"text": "加了", "top": 0.28}],
                },
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "晚上打不打守望先锋", "time": "19:51", "unread": False}],
                "chatPanel": {
                    "latestInbound": "晚上打不打守望先锋\n我们有四个人",
                    "latestOutbound": "加了",
                    "inbound": [
                        {"text": "晚上打不打守望先锋", "top": 0.55},
                        {"text": "我们有四个人", "top": 0.64},
                    ],
                    "outbound": [{"text": "加了", "top": 0.28}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_250.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("行，来吧。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "draft_saved", result
    assert result["contact"] == "1ock", result
    assert store.state["pending"]["contact"] == "1ock"
    assert "我们有四个人" in store.state["pending"]["inbound_text"]


def run_active_whitelist_chat_latest_outbound_skips_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "晚上打不打守望先锋", "time": "19:51", "unread": False}],
                "chatPanel": {
                    "latestInbound": "晚上打不打守望先锋",
                    "latestOutbound": "我知道了",
                    "inbound": [{"text": "晚上打不打守望先锋", "top": 0.46}],
                    "outbound": [{"text": "我知道了", "top": 0.66}],
                },
            },
        ]
    )
    clock = {"now": 9_260.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("行。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls


def run_whitelist_preview_fallback_claim_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "May", "preview": "在吗", "time": "20:59", "unread": False}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "在吗", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 11_200.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("在呢，怎么了？"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "draft_saved", result
    assert result["contact"] == "May", result
    assert store.state["pending"]["contact"] == "May"
    assert store.state["pending"]["inbound_text"] == "在吗"


def run_unread_whitelist_candidate_latest_outbound_skips_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Barrys", "preview": "你在吗", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {
                    "latestInbound": "你在吗",
                    "latestOutbound": "在，刚忙完",
                    "inbound": [{"text": "你在吗", "top": 0.49}],
                    "outbound": [{"text": "在，刚忙完", "top": 0.66}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_340.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert any(
        event.get("type") == "claim_skipped"
        and event.get("reason") == "latest_message_outbound"
        and event.get("contact") == "Barrys"
        for event in store.events
    ), store.events
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        ("probe", "Barrys"),
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_empty_queue_persistent_unread_waits_for_signal_change_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9150.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "no_candidate", first
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls

    clock["now"] = 9170.0
    second = runner.tick()
    assert second["status"] == "idle_wait", second
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls


def run_empty_queue_persistent_unread_sweeps_after_interval_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 30
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_150.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "no_candidate", first
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls

    clock["now"] = 9_170.0
    second = runner.tick()
    assert second["status"] == "idle_wait", second
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls

    clock["now"] = 9_182.0
    third = runner.tick()
    assert third["status"] == "no_candidate", third
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        "hide",
        "activate",
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_send_confirmation_retry_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["send_verify_retry_seconds"] = 45
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Barrys", "preview": "别玩ow了", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "别玩ow了", "latestOutbound": "牛逼"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "别玩ow了", "latestOutbound": "牛逼"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "别玩ow了", "latestOutbound": "牛逼"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "别玩ow了", "latestOutbound": "行行行，不玩了。"},
            },
        ]
    )
    clock = {"now": 5000.0}
    draft_text = "行行行，不玩了。"
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM(draft_text),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 5305.0
    second = runner.tick()
    assert second["status"] == "send_unconfirmed_retry", second
    assert store.state["pending"]["send_attempts"] == 1

    clock["now"] = 5351.0
    third = runner.tick()
    assert third["status"] == "sent", third
    assert store.state["pending"] is None
    assert fake_ui.calls.count("send") == 1
    assert any(event["type"] == "send_unconfirmed_retry_scheduled" for event in store.events)


def run_empty_inbound_recheck_cancels_pending_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "试试", "time": "19:02", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "试试", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "", "time": "19:02", "unread": False}],
                "chatPanel": {"latestInbound": "", "latestOutbound": ""},
            },
        ]
    )
    clock = {"now": 6100.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("秒回几率不大，他可能正忙着。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 6405.0
    second = runner.tick()
    assert second["status"] == "cancelled", second
    assert second["reason"] == "empty_inbound_recheck", second
    assert store.state["pending"] is None


def run_compose_text_does_not_count_as_sent() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["send_verify_retry_seconds"] = 45
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Barrys", "preview": "你最爱的kpop", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {"latestInbound": "你最爱的kpop", "latestOutbound": "牛逼"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {
                    "latestInbound": "你最爱的kpop",
                    "latestOutbound": "牛逼",
                    "outbound": [{"text": "牛逼", "top": 0.16}],
                },
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "chatPanel": {
                    "latestInbound": "你最爱的kpop",
                    "latestOutbound": "收到！Barrys 的 Kpop 歌单必须收藏，今晚就循环起来，太好听了！",
                    "outbound": [
                        {
                            "text": "收到！Barrys 的 Kpop 歌单必须收藏，今晚就循环起来，太好听了！",
                            "top": 0.86,
                        }
                    ],
                },
            },
        ]
    )
    clock = {"now": 7000.0}
    draft_text = "收到！Barrys 的 Kpop 歌单必须收藏，今晚就循环起来，太好听了！"
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM(draft_text),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 7305.0
    second = runner.tick()
    assert second["status"] == "send_unconfirmed_retry", second
    assert store.state["pending"]["send_attempts"] == 1
    assert not any(event["type"] == "auto_sent" for event in store.events)


def run_repeated_identical_text_new_time_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "Video Call", "time": "22:38", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "Call canceled by caller", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "Video Call", "time": "22:45", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "Call canceled by caller", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 6000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, False, True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("哎呀，刚才在忙没接到，不好意思哈。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    store.state["pending_queue"] = []
    store.state["pending"] = None
    store.state["last_menu_unread"] = False
    store.state["last_menu_signal"] = ""
    store.state["last_claim_menu_signal"] = ""

    clock["now"] = 6300.0
    second = runner.tick()
    assert second["status"] == "idle_wait", second

    clock["now"] = 6320.0
    third = runner.tick()
    assert third["status"] == "draft_saved", third
    assert store.state["pending"]["contact"] == "1ock"


def run_ocr_alias_contact_round_trip_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "10ck", "preview": "两小时时差呢", "time": "02:09", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "两小时时差呢", "latestOutbound": "这个比较麻烦"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "两小时时差呢", "time": "02:09", "unread": False}],
                "chatPanel": {"latestInbound": "两小时时差呢", "latestOutbound": "这个比较麻烦"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [{"name": "1ock", "preview": "两小时时差呢", "time": "02:09", "unread": False}],
                "chatPanel": {"latestInbound": "两小时时差呢", "latestOutbound": "去睡吧，晚安。"},
            },
        ]
    )
    clock = {"now": 8000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("去睡吧，晚安。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first
    assert store.state["pending"]["contact"] == "1ock"

    clock["now"] = 8305.0
    second = runner.tick()
    assert second["status"] == "sent", second
    assert store.state["pending"] is None


def run_find_chat_alias_match_path() -> None:
    chats = [{"name": "1ock"}, {"name": "王哥"}]
    assert find_chat(chats, "10ck") == {"name": "1ock"}


def run_capture_cleanup_deletes_old_snapshots_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capture_dir = Path(tmp)
        old_roster = capture_dir / "wechat-roster-old.png"
        old_chat = capture_dir / "wechat-chat-old.png"
        fresh_roster = capture_dir / "wechat-roster-fresh.png"
        keep_note = capture_dir / "notes.txt"
        for path in (old_roster, old_chat, fresh_roster, keep_note):
            path.write_text("x", encoding="utf-8")

        now = 10_000.0
        old_ts = now - (2 * 24 * 60 * 60)
        fresh_ts = now - 60
        os.utime(old_roster, (old_ts, old_ts))
        os.utime(old_chat, (old_ts, old_ts))
        os.utime(fresh_roster, (fresh_ts, fresh_ts))
        os.utime(keep_note, (old_ts, old_ts))

        result = delete_capture_snapshots_older_than(
            older_than_seconds=24 * 60 * 60,
            capture_dir=capture_dir,
            now=now,
        )

        assert result["deleted_count"] == 2, result
        assert not old_roster.exists()
        assert not old_chat.exists()
        assert fresh_roster.exists()
        assert keep_note.exists()


def run_ocr_variant_same_message_does_not_refresh_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "jade 爸妈没来", "time": "14:41", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {
                    "latestInbound": "jade 爸妈没来\n晚上想吃啥",
                    "latestOutbound": "",
                    "inbound": [
                        {"text": "jade 爸妈没来", "top": 0.42},
                        {"text": "晚上想吃啥", "top": 0.57},
                    ],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "visibleChats": [{"name": "Darren", "preview": "jade 爸妈没来", "time": "14:41", "unread": False}],
                "chatPanel": {
                    "latestInbound": "晚上想吃啥",
                    "latestOutbound": "",
                    "inbound": [{"text": "晚上想吃啥", "top": 0.57}],
                },
            },
        ]
    )
    clock = {"now": 12_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("随便，你定。"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
        dry_run=True,
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 12_305.0
    second = runner.tick()
    assert second["status"] == "dry_run_sent", second
    assert not any(event["type"] == "pending_refreshed_latest" for event in store.events), store.events


def main() -> int:
    run_happy_path()
    run_manual_reply_cancel()
    run_bottom_green_bubble_cancels_pending_path()
    run_old_outbound_before_inbound_is_not_manual_reply_path()
    run_multi_queue_path()
    run_follow_up_claim_second_pass_path()
    run_history_marker_trim_path()
    run_preview_matching_outbound_is_not_inbound_path()
    run_latest_message_refresh_path()
    run_send_confirmation_retry_path()
    run_empty_inbound_recheck_cancels_pending_path()
    run_compose_text_does_not_count_as_sent()
    run_repeated_identical_text_new_time_path()
    run_ocr_alias_contact_round_trip_path()
    run_find_chat_alias_match_path()
    run_capture_cleanup_deletes_old_snapshots_path()
    run_ocr_variant_same_message_does_not_refresh_path()
    run_no_claim_sweep_while_pending_wait_path()
    run_pending_menu_flicker_does_not_trigger_claim_path()
    run_queue_claims_on_menu_rising_path()
    run_queue_claims_while_pending_after_sweep_interval_path()
    run_stale_pending_gc_path()
    run_unknown_menu_signal_does_not_claim_path()
    run_non_whitelist_unread_cleared_path()
    run_active_whitelist_chat_claim_without_unread_badge_path()
    run_active_whitelist_chat_latest_outbound_skips_path()
    run_whitelist_preview_fallback_claim_path()
    run_unread_whitelist_candidate_latest_outbound_skips_path()
    run_empty_queue_persistent_unread_waits_for_signal_change_path()
    run_empty_queue_persistent_unread_sweeps_after_interval_path()
    print("selftest: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
