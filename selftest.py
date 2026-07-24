#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from wechat_autoreply.badge_detection import fallback_row_badge_detection
from wechat_autoreply.capture_cleanup import delete_capture_snapshots_older_than
from wechat_autoreply.config_store import default_config
from wechat_autoreply.json_output import load_first_json_object
from wechat_autoreply.orchestrator import AutoReplyRunner, choose_inbound_text, fingerprint
from wechat_autoreply.state_store import default_state
from wechat_autoreply.wechat_ui import (
    _extract_chat_panel,
    _pick_selected_title,
    find_chat,
    names_match,
    wechat_login_required,
)


class MemoryStore:
    def __init__(self) -> None:
        self.config = default_config()
        self.config["enabled"] = True
        self.config["passive_roster_sweep_enabled"] = False
        self.state = default_state()
        self.saved_states: list[dict] = []
        self.events: list[dict] = []

    def load_config(self):
        return copy.deepcopy(self.config)

    def load_state(self):
        return copy.deepcopy(self.state)

    def save_state(self, state):
        self.state = copy.deepcopy(state)
        self.saved_states.append(copy.deepcopy(state))

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

    def generate_reply(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict | None = None,
        screenshot_path: str | None = None,
        quoted_message: dict[str, str] | None = None,
    ) -> str:
        self.calls.append((contact, inbound_text))
        return self.reply


class MappingLLM:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    def generate_reply(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict | None = None,
        screenshot_path: str | None = None,
        quoted_message: dict[str, str] | None = None,
    ) -> str:
        self.calls.append((contact, inbound_text))
        return self.mapping[contact]


class PairMappingLLM:
    def __init__(self, mapping: dict[tuple[str, str], str]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    def generate_reply(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict | None = None,
        screenshot_path: str | None = None,
        quoted_message: dict[str, str] | None = None,
    ) -> str:
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

    def probe(self, select_chat=None, sleep_after_click=1.0, select_chat_click=None):
        self.calls.append(("probe", select_chat))
        result = copy.deepcopy(self.probes.pop(0))
        visible_chats = list(result.get("visibleChats") or [])
        for chat in visible_chats:
            if not isinstance(chat, dict):
                continue
            if not bool(chat.get("unread")):
                chat.setdefault("redPixelCount", 0)
                chat.setdefault("digitPixelCount", 0)
                chat.setdefault("numericBadge", False)
                continue
            # Older tests only set unread=True. The runtime now requires an
            # explicit numeric badge signal, so synthesize a minimal badge for
            # legacy fixtures unless the test already provided one.
            chat.setdefault("redPixelCount", 120)
            chat.setdefault("digitPixelCount", 10)
            chat.setdefault("numericBadge", True)
        result["visibleChats"] = visible_chats
        return result

    def focus_input_box(self, probe):
        self.calls.append("focus_input")

    def paste_text(self, text):
        self.calls.append(("paste", text))

    def send_message(self):
        self.calls.append("send")


class FakeBackgroundUI(FakeUI):
    def __init__(self, probes, background_probes):
        super().__init__(probes)
        self.background_probes = list(background_probes)

    def probe_roster_background(self):
        self.calls.append(("probe_background", None))
        return copy.deepcopy(self.background_probes.pop(0))


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
                "chatPanel": {
                    "latestInbound": "回到学校了吗",
                    "latestOutbound": "我等会回你",
                    "inbound": [{"text": "回到学校了吗", "top": 0.42}],
                    "outbound": [
                        {
                            "text": "我等会回你",
                            "top": 0.68,
                            "left": 0.72,
                            "width": 0.18,
                            "greenPixels": 120,
                        }
                    ],
                },
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
                    "outbound": [
                        {
                            "text": "昨晚到的",
                            "top": 0.66,
                            "left": 0.72,
                            "width": 0.18,
                            "greenPixels": 120,
                        }
                    ],
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


def run_pending_selection_failure_retries_instead_of_cancel_path() -> None:
    store = MemoryStore()
    store.config["pending_selection_retry_seconds"] = 60
    store.config["pending_selection_retry_max_attempts"] = 2
    pending = {
        "contact": "1ock",
        "inbound_text": "我真的吓死了",
        "message_time": "11:25",
        "inbound_fingerprint": "fp-1ock",
        "draft_text": "吓啥，稳住",
        "created_at": 900.0,
        "due_at": 950.0,
        "outbound_snapshot": "",
        "active_chat_title": "1ock",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "May",
                "chatPanel": {},
            }
        ]
    )
    state = store.load_state()
    config = store.load_config()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1000.0,
    )

    result = runner._handle_pending(config, state, state["pending_queue"][0], idle_seconds=45, now=1000.0)

    assert result["status"] == "pending_retry_selection_not_confirmed", result
    assert [item["contact"] for item in store.state["pending_queue"]] == ["1ock"]
    assert store.state["pending"]["selection_retry_count"] == 1
    assert store.state["pending"]["due_at"] == 1060.0
    assert any(event["type"] == "pending_selection_retry_scheduled" for event in store.events), store.events
    assert not any(event["type"] == "pending_cancelled" for event in store.events), store.events


def run_pending_title_ocr_garbage_uses_panel_preview_evidence_path() -> None:
    store = MemoryStore()
    store.config["recheck_vote_frames"] = 1
    pending = {
        "contact": "May",
        "inbound_text": "排位也变成132吗",
        "message_time": "01:28",
        "inbound_fingerprint": fingerprint("May", "排位也变成132吗", "01:28"),
        "draft_text": "你是说段位还是分？",
        "created_at": 900.0,
        "due_at": 950.0,
        "outbound_snapshot": "",
        "active_chat_title": "May",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "JAJWTHBXN UTSPミミッE",
                "selectedChat": "May",
                "selectedChatRequested": "May",
                "visibleChats": [
                    {"name": "May", "preview": "排位也变成132吗", "time": "01:28", "unread": False}
                ],
                "chatPanel": {
                    "latestInbound": "排位也变成132吗",
                    "latestOutbound": "",
                    "inbound": [
                        {
                            "text": "排位也变成132吗",
                            "top": 0.72,
                            "left": 0.08,
                            "width": 0.24,
                            "grayPixels": 120,
                            "greenPixels": 0,
                        }
                    ],
                    "outbound": [],
                },
            }
        ]
    )
    state = store.load_state()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1000.0,
        dry_run=True,
    )

    result = runner._handle_pending(
        store.load_config(), state, state["pending_queue"][0], idle_seconds=45, now=1000.0
    )

    assert result["status"] == "dry_run_sent", result
    assert state["pending"] is None
    assert any(
        event["type"] == "pending_selection_confirmed_by_panel_evidence"
        and event.get("contact") == "May"
        for event in store.events
    ), store.events
    assert not any(event["type"] == "pending_selection_retry_scheduled" for event in store.events), store.events


def run_pending_selection_failure_snoozes_after_retry_budget_path() -> None:
    store = MemoryStore()
    store.config["pending_selection_retry_seconds"] = 60
    store.config["pending_selection_retry_max_attempts"] = 2
    store.config["pending_selection_snooze_seconds"] = 1800
    pending = {
        "contact": "1ock",
        "inbound_text": "我真的吓死了",
        "message_time": "11:25",
        "inbound_fingerprint": "fp-1ock",
        "draft_text": "吓啥，稳住",
        "created_at": 900.0,
        "due_at": 950.0,
        "outbound_snapshot": "",
        "active_chat_title": "1ock",
        "selection_retry_count": 2,
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "May",
                "chatPanel": {},
            }
        ]
    )
    state = store.load_state()
    config = store.load_config()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1000.0,
    )

    result = runner._handle_pending(config, state, state["pending_queue"][0], idle_seconds=45, now=1000.0)

    assert result["status"] == "pending_selection_snoozed", result
    assert [item["contact"] for item in store.state["pending_queue"]] == ["1ock"]
    assert store.state["pending"]["selection_retry_count"] == 0
    assert store.state["pending"]["selection_snooze_count"] == 1
    assert store.state["pending"]["due_at"] == 2800.0
    assert any(event["type"] == "pending_selection_snoozed" for event in store.events), store.events
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


def run_overdue_pending_bypasses_snoozed_queue_head_path() -> None:
    store = MemoryStore()
    store.config["contact_memory_enabled"] = False
    store.config["roster_sweep_interval_seconds"] = 9999
    snoozed = {
        "contact": "1ock",
        "inbound_text": "稍后再看",
        "message_time": "12:21",
        "inbound_fingerprint": "fp-1ock-snoozed",
        "draft_text": "行",
        "created_at": 900.0,
        "due_at": 1_300.0,
    }
    overdue = {
        "contact": "Darren",
        "inbound_text": "我今天回来\n7.40到",
        "message_time": "17:21",
        "inbound_fingerprint": "fp-darren-overdue",
        "draft_text": "行，7:40到是吧，到了说一声",
        "created_at": 700.0,
        "due_at": 800.0,
        "send_attempts": 1,
        "last_send_attempt_at": 700.0,
    }
    store.state["pending_queue"] = [copy.deepcopy(snoozed), copy.deepcopy(overdue)]
    store.state["pending"] = copy.deepcopy(snoozed)
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=FakeUI([]),
        llm_client=FakeLLM("unused"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1_000.0,
    )

    result = runner.tick()

    assert result["status"] == "sent", result
    assert result["contact"] == "Darren", result
    assert [item["contact"] for item in store.state["pending_queue"]] == ["1ock"]


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


def run_card_then_new_messages_prefers_new_message_group_path() -> None:
    panel = {
        "inbound": [
            {"text": "万锦免费赛车体验！跑进1分20秒送＄15礼卡", "top": 0.4283},
            {"text": "@鱼多多's note14 Shares", "top": 0.4915},
            {"text": "0设 小红书", "top": 0.5690},
            {"text": "我今天回来", "top": 0.6802},
            {"text": "7.40到", "top": 0.7530},
        ],
        "outbound": [{"text": "他没说在哪里啊", "top": 0.3018}],
    }
    assert choose_inbound_text(panel, "7.40 到") == "我今天回来\n7.40到"


def run_chat_title_near_panel_edge_is_not_replaced_by_message_path() -> None:
    observations = [
        {
            "text": "10ck",
            "bbox": {"left": 0.3275, "top": 0.0291, "w": 0.0401},
        },
        {
            "text": "个石相活动！烟孙一旅北夫赘区以州图！尽西耳有许啊",
            "bbox": {"left": 0.3885, "top": 0.0775, "w": 0.3345},
        },
    ]
    title = _pick_selected_title(observations)
    assert title == "10ck", title
    assert names_match(title, "1ock")


def run_wechat_login_required_detection_path() -> None:
    assert wechat_login_required([{"text": "Enter Weixin"}])
    assert wechat_login_required([{"text": "Switch Account"}, {"text": "Transfer files only"}])
    assert not wechat_login_required([{"text": "May"}, {"text": "在吗"}])


def run_ocr_symbol_tail_trim_path() -> None:
    assert choose_inbound_text({"latestInbound": "去不去\n日％•"}, "去不去") == "去不去"
    assert choose_inbound_text({"latestInbound": "没事哈哈哈哈\n日％♥"}, "没事哈哈哈哈") == "没事哈哈哈哈"
    assert choose_inbound_text({"latestInbound": "现在在专心赌球\nU"}, "现在在专心赌球") == "现在在专心赌球"


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
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "[表情]", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "[表情]", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "[表情]", "latestOutbound": "哈哈，收到你的表情了。"},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                "visibleChats": [{"name": "Barrys", "preview": "[表情]", "time": "12:00", "unread": False}],
                "chatPanel": {"latestInbound": "[表情]", "latestOutbound": "哈哈，收到你的表情了。"},
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


def run_empty_claim_menu_flicker_does_not_reopen_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_100.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True, False, True]),
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
    assert store.state["last_claim_menu_signal"] == "1"

    clock["now"] = 9_120.0
    second = runner.tick()
    assert second["status"] == "idle_wait", second
    assert store.state["last_claim_menu_signal"] == "1"
    assert store.state["pending_menu_clear_streak"] == 1

    clock["now"] = 9_140.0
    third = runner.tick()
    assert third["status"] == "idle_wait", third
    assert fake_ui.calls.count("activate") == 1, fake_ui.calls
    assert sum(1 for event in store.events if event["type"] == "claim_candidates") == 1, store.events


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


def run_passive_roster_sweep_claims_without_menu_signal_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    fake_ui = FakeBackgroundUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "费了！！", "time": "22:52", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "visibleChats": [{"name": "Darren", "preview": "费了！！", "time": "22:52", "unread": False}],
                "chatPanel": {"latestInbound": "费了！！", "latestOutbound": "上一条回复"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ],
        [
            {
                "status": "ok",
                "screenshot": "/tmp/background-roster-passive.png",
                "visibleChats": [
                    {
                        "name": "Darren",
                        "preview": "费了！！",
                        "unread": True,
                        "redPixelCount": 120,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("确实有点费"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 20_000.0,
    )

    result = runner.tick()
    assert result["status"] == "draft_saved", result
    assert result["contact"] == "Darren", result
    assert store.state["pending"]["inbound_text"] == "费了！！"
    assert any(
        event["type"] == "wechat_window_action"
        and event.get("action") == "open"
        and event.get("trigger") == "passive_roster_sweep"
        for event in store.events
    ), store.events


def run_passive_roster_sweep_stays_background_without_badge_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    fake_ui = FakeBackgroundUI(
        [],
        [
            {
                "status": "ok",
                "screenshot": "/tmp/background-roster.png",
                "visibleChats": [
                    {
                        "name": "Darren",
                        "preview": "无了",
                        "unread": False,
                        "redPixelCount": 0,
                        "digitPixelCount": 0,
                        "numericBadge": False,
                    }
                ],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不该生成"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 20_000.0,
    )

    result = runner.tick()
    assert result["status"] == "idle_wait", result
    assert fake_ui.calls == [("probe_background", None)], fake_ui.calls
    assert store.state["last_roster_sweep_at"] == 20_000.0
    assert any(
        event.get("type") == "passive_roster_preflight"
        and event.get("badge_detected") is False
        for event in store.events
    ), store.events
    assert not any(event.get("type") == "wechat_window_action" for event in store.events), store.events


def run_passive_roster_sweep_opens_only_after_background_badge_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    fake_ui = FakeBackgroundUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "费了", "time": "22:52", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {
                    "latestInbound": "费了",
                    "latestOutbound": "上一条",
                    "inbound": [
                        {
                            "text": "费了",
                            "top": 0.72,
                            "left": 0.08,
                            "width": 0.12,
                            "grayPixels": 120,
                        }
                    ],
                    "outbound": [{"text": "上一条", "top": 0.42}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ],
        [
            {
                "status": "ok",
                "screenshot": "/tmp/background-roster-badge.png",
                "visibleChats": [
                    {
                        "name": "Darren",
                        "preview": "费了",
                        "unread": True,
                        "redPixelCount": 120,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("确实费了"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 20_000.0,
    )

    result = runner.tick()
    assert result["status"] == "draft_saved", result
    assert fake_ui.calls[0] == ("probe_background", None), fake_ui.calls
    assert "activate" in fake_ui.calls
    assert any(
        event.get("type") == "passive_roster_preflight"
        and event.get("badge_detected") is True
        for event in store.events
    ), store.events


def run_passive_roster_sweep_queues_whitelist_while_pending_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    store.config["allowed_contacts"] = ["Darren", "1ock"]
    pending = {
        "contact": "Darren",
        "inbound_text": "对 饿了 走了",
        "message_time": "11:14",
        "inbound_fingerprint": "fp-darren",
        "draft_text": "行那你先吃，慢走啊",
        "created_at": 21_000.0,
        "due_at": 22_000.0,
        "outbound_snapshot": "",
        "active_chat_title": "Darren",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeBackgroundUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "晚上打不打", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "晚上打不打", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ],
        [
            {
                "status": "ok",
                "screenshot": "/tmp/background-roster-pending-1ock.png",
                "visibleChats": [
                    {
                        "name": "1ock",
                        "preview": "晚上打不打",
                        "unread": True,
                        "redPixelCount": 120,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("打，几点"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 21_100.0,
    )

    result = runner.tick()
    assert result["status"] == "pending_wait_delay", result
    assert result["queue_length"] == 2, result
    assert [item["contact"] for item in store.state["pending_queue"]] == ["Darren", "1ock"]
    assert store.state["pending"]["contact"] == "Darren"
    assert ("probe_background", None) in fake_ui.calls
    assert "activate" in fake_ui.calls


def run_passive_roster_sweep_ignores_badge_for_already_queued_contact_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    store.config["allowed_contacts"] = ["1ock"]
    pending = {
        "contact": "1ock",
        "inbound_text": "我真的吓死了",
        "message_time": "11:25",
        "inbound_fingerprint": "fp-1ock",
        "draft_text": "吓啥，稳住",
        "created_at": 31_000.0,
        "due_at": 32_000.0,
        "outbound_snapshot": "",
        "active_chat_title": "1ock",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeBackgroundUI(
        [],
        [
            {
                "status": "ok",
                "screenshot": "/tmp/background-roster-queued-1ock.png",
                "visibleChats": [
                    {
                        "name": "1ock",
                        "preview": "我真的吓死了",
                        "unread": True,
                        "redPixelCount": 95,
                        "digitPixelCount": 8,
                        "numericBadge": True,
                    }
                ],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 31_100.0,
    )

    result = runner.tick()
    assert result["status"] == "pending_wait_delay", result
    assert fake_ui.calls == [("probe_background", None)], fake_ui.calls
    assert [item["contact"] for item in store.state["pending_queue"]] == ["1ock"]
    assert any(
        event.get("type") == "passive_roster_preflight"
        and event.get("badge_detected") is True
        and not event.get("actionable_badge_rows")
        and event.get("ignored_queued_badge_rows")
        for event in store.events
    ), store.events
    assert not any(event.get("type") == "wechat_window_action" for event in store.events), store.events


def run_claim_persists_pending_before_return_path() -> None:
    store = MemoryStore()
    config = store.load_config()
    config["allowed_contacts"] = ["1ock"]
    config["send_delay_seconds"] = 180
    state = store.load_state()
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": "我真的吓死了", "time": "11:25", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {
                    "latestInbound": "我真的吓死了",
                    "latestOutbound": "",
                    "inbound": [{"text": "我真的吓死了", "top": 0.72, "left": 0.08, "grayPixels": 120}],
                    "outbound": [],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("吓啥，稳住"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 30_000.0,
    )

    result = runner._handle_claim(
        config,
        state,
        idle_seconds=45,
        now=30_000.0,
        claim_trigger="passive_roster_sweep",
    )

    assert result["status"] == "draft_saved", result
    assert store.saved_states, "claim flow should persist pending before tick finally"
    saved_queue = list(store.saved_states[0].get("pending_queue") or [])
    assert [item["contact"] for item in saved_queue] == ["1ock"], store.saved_states[0]
    assert saved_queue[0]["draft_text"] == "吓啥，稳住"
    assert store.saved_states[0].get("last_seen_inbound", {}).get("1ock")


def run_claim_title_ocr_garbage_uses_panel_preview_evidence_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["allowed_contacts"] = ["1ock"]
    inbound_text = "哥哥 山火会不会影响班夫啊"
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "JAJWTHBXN UTSPミミッE",
                "selectedChat": "1ock",
                "selectedChatRequested": "1ock",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": {
                    "latestInbound": inbound_text,
                    "latestOutbound": "行，那先别折腾快递啦",
                    "inbound": [
                        {"text": inbound_text, "top": 0.75, "left": 0.39, "width": 0.18, "grayPixels": 12000}
                    ],
                    "outbound": [{"text": "行，那先别折腾快递啦", "top": 0.60, "left": 0.58}],
                },
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": {},
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("应该影响不大，但得看山火和风向"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 40_000.0,
    )

    result = runner.tick()

    assert result["status"] == "draft_saved", result
    assert store.state["pending"]["contact"] == "1ock"
    assert store.state["pending"]["inbound_text"] == inbound_text
    assert fake_ui.calls.count(("probe", "1ock")) == 1, fake_ui.calls
    assert any(
        event["type"] == "claim_selection_confirmed_by_panel_evidence"
        and event.get("contact") == "1ock"
        and event.get("mode") == "panel_preview_match"
        for event in store.events
    ), store.events


def run_claim_selection_failure_persists_one_badge_backed_retry_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["allowed_contacts"] = ["1ock"]
    inbound_text = "哥哥 山火会不会影响班夫啊"
    wrong_panel = {
        "latestInbound": "不相关的旧消息",
        "inbound": [{"text": "不相关的旧消息", "top": 0.75, "left": 0.39, "grayPixels": 8000}],
        "outbound": [],
    }
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "May",
                "selectedChat": "1ock",
                "selectedChatRequested": "1ock",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": wrong_panel,
            },
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "May",
                "selectedChat": "1ock",
                "selectedChatRequested": "1ock",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": wrong_panel,
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": wrong_panel,
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": wrong_panel,
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "selectedChat": "1ock",
                "selectedChatRequested": "1ock",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": {
                    "latestInbound": inbound_text,
                    "inbound": [{"text": inbound_text, "top": 0.75, "left": 0.39, "grayPixels": 12000}],
                    "outbound": [],
                },
            },
            {
                "status": "ok",
                "visibleChats": [{"name": "1ock", "preview": inbound_text, "time": "01:01", "unread": False}],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 41_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("应该影响不大，但得看山火和风向"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "no_candidate", first
    assert store.state["claim_retry_pending"] is True
    assert store.state["claim_retry_candidate"]["contact"] == "1ock"

    clock["now"] = 41_005.0
    second = runner.tick()
    assert second["status"] == "draft_saved", second
    assert store.state["pending"]["contact"] == "1ock"
    assert store.state["claim_retry_pending"] is False
    assert store.state["claim_retry_candidate"] is None
    assert any(event["type"] == "claim_selection_retry_candidate" for event in store.events), store.events


def run_passive_roster_sweep_ignores_background_non_whitelist_badge_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    fake_ui = FakeBackgroundUI(
        [],
        [
            {
                "status": "ok",
                "visibleChats": [
                    {
                        "name": "工作群",
                        "preview": "新消息",
                        "unread": True,
                        "redPixelCount": 120,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不该生成"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 20_000.0,
    )

    result = runner.tick()
    assert result["status"] == "idle_wait", result
    assert fake_ui.calls == [("probe_background", None)], fake_ui.calls
    assert not any(event.get("type") == "wechat_window_action" for event in store.events), store.events


def run_passive_roster_sweep_does_not_clear_non_whitelist_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 60
    fake_ui = FakeBackgroundUI(
        [],
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Sara", "preview": "在吗", "time": "22:52", "unread": True}],
            }
        ],
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("yo"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 20_000.0,
    )

    result = runner.tick()
    assert result["status"] == "idle_wait", result
    assert fake_ui.calls == [("probe_background", None)], fake_ui.calls
    assert not any(event["type"] == "non_whitelist_unread_cleared" for event in store.events)


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


def run_active_whitelist_chat_without_unread_badge_skips_path() -> None:
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
                        {"text": "晚上打不打守望先锋", "top": 0.55, "left": 0.42, "width": 0.24, "grayPixels": 120},
                        {"text": "我们有四个人", "top": 0.64, "left": 0.42, "width": 0.18, "grayPixels": 120},
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
                        {"text": "晚上打不打守望先锋", "top": 0.55, "left": 0.42, "width": 0.24, "grayPixels": 120},
                        {"text": "我们有四个人", "top": 0.64, "left": 0.42, "width": 0.18, "grayPixels": 120},
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
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert ("probe", "1ock") not in fake_ui.calls, fake_ui.calls


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
                "visibleChats": [
                    {
                        "name": "May",
                        "preview": "在吗",
                        "time": "20:59",
                        "unread": False,
                        "redPixelCount": 120,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
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


def run_global_signal_visible_whitelist_without_badge_skips_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["allowed_contacts"] = ["1ock"]
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {
                        "name": "10ck",
                        "preview": "哥哥我发现你长得像高司令",
                        "time": "22:46",
                        "unread": False,
                    }
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "visibleChats": [
                    {
                        "name": "1ock",
                        "preview": "哥哥我发现你长得像高司令",
                        "time": "22:46",
                        "unread": False,
                    }
                ],
                "chatPanel": {"latestInbound": "哥哥我发现你长得像高司令", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([2]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("哈哈你别说还真有点"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 11_260.0,
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert ("probe", "1ock") not in fake_ui.calls, fake_ui.calls
    assert not any(event["type"] == "claim_global_signal_whitelist_probe_candidates" for event in store.events)


def run_global_signal_hidden_whitelist_search_skips_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    store.config["allowed_contacts"] = ["May", "1ock"]
    store.state["last_seen_inbound"] = {"May": "older-message"}
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "visibleChats": [{"name": "May", "preview": "好了", "time": "01:50", "unread": True}],
                "chatPanel": {"latestInbound": "好了", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([2]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("好嘞"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 11_280.0,
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert ("probe", "May") not in fake_ui.calls, fake_ui.calls
    assert not any(event["type"] == "claim_global_signal_whitelist_probe_candidates" for event in store.events)


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
    assert result["status"] == "draft_saved", result
    assert result["contact"] == "Barrys", result
    assert store.state["pending"]["contact"] == "Barrys"
    assert store.state["pending"]["inbound_text"] == "你在吗"
    assert any(
        event.get("type") == "claim_outbound_recheck"
        and event.get("contact") == "Barrys"
        and event.get("still_outbound") is True
        for event in store.events
    ), store.events
    assert any(
        event.get("type") == "claim_preview_fallback"
        and event.get("reason") == "latest_outbound_recheck"
        and event.get("contact") == "Barrys"
        for event in store.events
    ), store.events
    assert fake_ui.calls == [
        "activate",
        ("probe", None),
        ("probe", "Barrys"),
        ("probe", "Barrys"),
        ("probe", None),
        "hide",
    ], fake_ui.calls


def run_numeric_badge_inbound_bubble_overrides_outbound_text_match_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {
                        "name": "Darren",
                        "preview": "无了",
                        "time": "23:29",
                        "unread": True,
                        "redPixelCount": 179,
                        "digitPixelCount": 10,
                        "numericBadge": True,
                    }
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {
                    "latestInbound": "无了",
                    "latestOutbound": "无了？",
                    "inbound": [
                        {
                            "text": "无了",
                            "top": 0.75,
                            "left": 0.08,
                            "width": 0.12,
                            "grayPixels": 120,
                        }
                    ],
                    "outbound": [
                        {
                            "text": "无了？",
                            "top": 0.64,
                            "left": 0.72,
                            "width": 0.12,
                            "greenPixels": 120,
                        }
                    ],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    llm = FakeLLM("哪能无了，接着整。")
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=llm,
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 9_360.0,
    )

    result = runner.tick()
    assert result["status"] == "draft_saved", result
    assert result["contact"] == "Darren", result
    assert store.state["pending"]["inbound_text"] == "无了"
    assert llm.calls == [("Darren", "无了")]
    assert any(
        event.get("type") == "claim_self_match_overridden_by_inbound_badge"
        and event.get("contact") == "Darren"
        for event in store.events
    ), store.events


def run_numeric_badge_latest_outbound_text_match_still_skips_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "Darren", "preview": "无了？", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Darren",
                "chatPanel": {
                    "latestInbound": "费了",
                    "latestOutbound": "无了？",
                    "inbound": [
                        {
                            "text": "费了",
                            "top": 0.49,
                            "left": 0.08,
                            "width": 0.12,
                            "grayPixels": 120,
                        }
                    ],
                    "outbound": [
                        {
                            "text": "无了？",
                            "top": 0.72,
                            "left": 0.72,
                            "width": 0.12,
                            "greenPixels": 120,
                        }
                    ],
                },
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    llm = FakeLLM("不该生成")
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=llm,
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 9_380.0,
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert llm.calls == []
    assert any(
        event.get("type") == "claim_skipped"
        and event.get("reason") == "preview_matches_outbound"
        and event.get("contact") == "Darren"
        for event in store.events
    ), store.events


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


def run_empty_queue_persistent_unread_suppresses_repeat_claim_path() -> None:
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
    assert third["status"] == "idle_wait", third
    assert third["menu_unread"] is True, third
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls


def run_same_menu_signal_with_passive_sweep_does_not_reopen_path() -> None:
    store = MemoryStore()
    store.config["passive_roster_sweep_enabled"] = True
    store.config["roster_sweep_interval_seconds"] = 30
    store.config["allowed_contacts"] = ["May"]
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 9_150.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([2, 2]),
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

    clock["now"] = 9_182.0
    second = runner.tick()
    assert second["status"] == "idle_wait", second
    assert second["menu_unread"] is True, second
    assert fake_ui.calls == ["activate", ("probe", None), "hide"], fake_ui.calls


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
    snoozed = {
        "contact": "May",
        "inbound_text": "稍后处理",
        "message_time": "17:00",
        "inbound_fingerprint": "fp-may-snoozed",
        "draft_text": "行",
        "created_at": 5_000.0,
        "due_at": 6_000.0,
    }
    store.state["pending_queue"].insert(0, copy.deepcopy(snoozed))
    store.state["pending"] = copy.deepcopy(snoozed)

    clock["now"] = 5305.0
    second = runner.tick()
    assert second["status"] == "send_unconfirmed_retry", second
    assert [item["contact"] for item in store.state["pending_queue"]] == ["May", "Barrys"]
    assert "send_attempts" not in store.state["pending_queue"][0]
    assert store.state["pending_queue"][1]["send_attempts"] == 1

    clock["now"] = 5351.0
    third = runner.tick()
    assert third["status"] == "sent", third
    assert [item["contact"] for item in store.state["pending_queue"]] == ["May"]
    assert store.state["pending"]["contact"] == "May"
    assert fake_ui.calls.count("send") == 1
    assert any(event["type"] == "send_unconfirmed_retry_scheduled" for event in store.events)


def run_stale_unconfirmed_send_closes_without_opening_path() -> None:
    store = MemoryStore()
    store.config["contact_memory_enabled"] = False
    store.config["send_confirmation_timeout_seconds"] = 180
    pending = {
        "contact": "May",
        "inbound_text": "我4号去底特律",
        "message_time": "17:41",
        "inbound_fingerprint": "fp-may-detroit",
        "draft_text": "行，那这趟稳了。到时候直接飞底特律？",
        "created_at": 1_000.0,
        "due_at": 2_800.0,
        "outbound_snapshot": "你来机票呢",
        "active_chat_title": "May",
        "send_attempts": 1,
        "last_send_attempt_at": 1_100.0,
        "send_confirmation_attempts": 1,
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI([])
    state = store.load_state()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(0),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1_301.0,
    )

    result = runner._handle_pending(
        store.load_config(), state, state["pending_queue"][0], idle_seconds=0, now=1_301.0
    )

    assert result["status"] == "sent", result
    assert result["confirmation"] == "send_action_timeout", result
    assert state["pending"] is None
    assert fake_ui.calls == [], fake_ui.calls
    assert any(
        event["type"] == "auto_sent"
        and event.get("confirmation") == "send_action_timeout"
        and event.get("inferred") is True
        for event in store.events
    ), store.events


def run_unconfirmed_send_selection_failures_have_finite_budget_path() -> None:
    store = MemoryStore()
    store.config["contact_memory_enabled"] = False
    store.config["send_confirmation_max_attempts"] = 3
    pending = {
        "contact": "May",
        "inbound_text": "我4号去底特律",
        "message_time": "17:41",
        "inbound_fingerprint": "fp-may-detroit",
        "draft_text": "行，那这趟稳了。到时候直接飞底特律？",
        "created_at": 1_000.0,
        "due_at": 1_200.0,
        "outbound_snapshot": "你来机票呢",
        "active_chat_title": "May",
        "send_attempts": 1,
        "last_send_attempt_at": 1_190.0,
        "send_confirmation_attempts": 2,
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "",
                "chatPanel": {},
            }
        ]
    )
    state = store.load_state()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 1_205.0,
    )

    result = runner._handle_pending(
        store.load_config(), state, state["pending_queue"][0], idle_seconds=45, now=1_205.0
    )

    assert result["status"] == "sent", result
    assert result["confirmation"] == "send_confirmation_exhausted", result
    assert state["pending"] is None
    assert fake_ui.calls.count("send") == 0, fake_ui.calls
    assert fake_ui.calls.count("activate") == 1, fake_ui.calls
    assert fake_ui.calls.count("hide") == 1, fake_ui.calls


def run_send_confirmation_self_echo_marks_sent_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    draft_text = "这也能整出来？别吓唬我，最近身体咋样，实习安排得还顺利不？"
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "王哥", "preview": "朋友们，问你一个细思极恐的问题", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {"latestInbound": "朋友们，问你一个细思极恐的问题", "latestOutbound": ""},
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
                "chatPanel": {"latestInbound": "朋友们，问你一个细思极恐的问题", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {
                    "latestInbound": "还顺利不？",
                    "latestOutbound": "小红书",
                    "inbound": [{"text": "还顺利不？", "top": 0.72}],
                    "outbound": [{"text": "小红书", "top": 0.16}],
                },
            },
        ]
    )
    clock = {"now": 5_600.0}
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

    clock["now"] = 5_905.0
    second = runner.tick()
    assert second["status"] == "sent", second
    assert store.state["pending"] is None
    assert not any(event["type"] == "send_unconfirmed_retry_scheduled" for event in store.events), store.events
    assert any(
        event["type"] == "auto_sent"
        and event.get("confirmation") == "self_echo_after_send"
        and event.get("match_mode") == "tail_fragment"
        for event in store.events
    ), store.events


def run_empty_panel_second_click_recovers_pending_send_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    pending = {
        "contact": "May",
        "inbound_text": "在电影爆笑",
        "message_time": "00:50",
        "inbound_fingerprint": fingerprint("May", "在电影爆笑", "00:50"),
        "draft_text": "这也能算“笑”？我看是惊吓吧",
        "created_at": 6_000.0,
        "due_at": 6_100.0,
        "outbound_snapshot": "",
        "active_chat_title": "May",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": False,
                "activeChat": "",
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "在电影爆笑", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "在电影爆笑", "latestOutbound": "这也能算“笑”？我看是惊吓吧"},
            },
        ]
    )
    state = store.load_state()
    config = store.load_config()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 6_200.0,
    )

    result = runner._handle_pending(config, state, state["pending_queue"][0], idle_seconds=45, now=6_200.0)

    assert result["status"] == "sent", result
    assert state["pending"] is None
    assert fake_ui.calls.count(("probe", "May")) == 4, fake_ui.calls
    assert any(event["type"] == "pending_reselect_empty_panel_second_click" for event in store.events), store.events
    assert any(event["type"] == "pending_reselect_empty_panel_recovered" for event in store.events), store.events


def run_historical_outbound_draft_clears_retry_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    draft_text = "这也能算“笑”？我看是惊吓吧，你脑子没被炸坏就行"
    pending = {
        "contact": "May",
        "inbound_text": "最近还有啥好看\n你去看了 scary movie没\n巨好笑\n在电影爆笑",
        "message_time": "00:50",
        "inbound_fingerprint": "fp-may-movie",
        "draft_text": draft_text,
        "created_at": 6_000.0,
        "due_at": 6_100.0,
        "outbound_snapshot": "不嘻嘻",
        "active_chat_title": "May",
        "send_attempts": 1,
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {
                    "latestInbound": "硬太密了",
                    "latestOutbound": "没入院我看我只有万字解说",
                    "inbound": [
                        {"text": "在电影爆笑", "top": 0.18},
                        {"text": "硬太密了", "top": 0.82},
                    ],
                    "outbound": [
                        {"text": draft_text, "top": 0.34},
                        {"text": "没入院我看我只有万字解说", "top": 0.58},
                    ],
                },
            },
        ]
    )
    state = store.load_state()
    config = store.load_config()
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不用"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: 6_200.0,
    )

    result = runner._handle_pending(config, state, state["pending_queue"][0], idle_seconds=45, now=6_200.0)

    assert result["status"] == "sent", result
    assert state["pending"] is None
    assert "send" not in fake_ui.calls, fake_ui.calls
    assert any(
        event["type"] == "auto_sent"
        and event.get("confirmation") == "historical_outbound"
        and event.get("match_mode")
        for event in store.events
    ), store.events


def run_empty_inbound_recheck_retries_pending_path() -> None:
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
    assert second["status"] == "pending_retry_selection_not_confirmed", second
    assert second["reason"] == "empty_inbound_recheck", second
    assert store.state["pending"]["contact"] == "1ock"
    assert store.state["pending"]["selection_retry_count"] == 1
    assert any(event["type"] == "pending_selection_retry_scheduled" for event in store.events), store.events


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
                    "inbound": [{"text": "你最爱的kpop", "top": 0.54}],
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
                    "inbound": [{"text": "你最爱的kpop", "top": 0.54}],
                    "outbound": [
                        {
                            "text": "收到！Barrys 的 Kpop 歌单必须收藏，今晚就循环起来，太好听了！",
                            "top": 0.94,
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


def run_claim_preview_fallback_on_panel_mismatch_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {"name": "10ck", "preview": "你咋看的啊", "time": "13:29", "unread": True},
                    {"name": "Barrys", "preview": "帮我看看big datat", "time": "13:22", "unread": True},
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "1ock",
                "chatPanel": {"latestInbound": "你咋看的啊", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "Barrys",
                # Simulate stale panel content from previous chat; claim should fallback to preview.
                "chatPanel": {"latestInbound": "你咋看的啊", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    clock = {"now": 13_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=PairMappingLLM(
            {
                ("1ock", "你咋看的啊"): "刚刷的 感觉有点意思 你看了没",
                ("Barrys", "帮我看看big datat"): "我看了，稍后给你细说",
            }
        ),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] in {"draft_saved", "drafts_saved"}, first
    queue = list(store.state.get("pending_queue") or [])
    assert len(queue) == 2, queue
    mapping = {item["contact"]: item["inbound_text"] for item in queue}
    assert mapping.get("1ock") == "你咋看的啊", mapping
    assert mapping.get("Barrys") == "帮我看看big datat", mapping
    assert any(
        event["type"] == "claim_preview_fallback"
        and event.get("contact") == "Barrys"
        and event.get("reason") == "panel_preview_mismatch"
        for event in store.events
    ), store.events


def run_recent_auto_outbound_preview_is_not_claimed_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    clock = {"now": 13_500.0}
    recent_text = "这也能整出来？别吓唬我，最近身体咋样，实习安排得还顺利不？"
    store.state["recent_auto_outbounds"] = {
        "王哥": [{"text": recent_text, "ts": clock["now"] - 120, "source": "send_attempt"}]
    }
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [
                    {"name": "王哥", "preview": "这也能整出来？别吓唬我，最近身…", "time": "16:13", "unread": True}
                ],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {"latestInbound": "还顺利不？", "latestOutbound": "小红书"},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
        ]
    )
    llm = FakeLLM("不该生成")
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=llm,
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "no_candidate", result
    assert store.state["pending"] is None
    assert llm.calls == []
    assert any(event["type"] == "claim_skipped_recent_self_preview" for event in store.events), store.events


def run_pending_self_echo_tail_cancels_before_refresh_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    clock = {"now": 13_800.0}
    recent_text = "这也能整出来？别吓唬我，最近身体咋样，实习安排得还顺利不？"
    pending = {
        "contact": "王哥",
        "inbound_text": "这也能整出来？别吓唬我，最近身…",
        "message_time": "16:13",
        "inbound_fingerprint": "fp-self-preview",
        "draft_text": "这哪是整出来，是你那脑洞太大。最近身体咋样，别光在那儿瞎琢磨，好好休息，实习还顺利不？",
        "created_at": clock["now"] - 305,
        "due_at": clock["now"] - 1,
        "outbound_snapshot": "小红书",
        "active_chat_title": "王哥",
    }
    store.state["pending_queue"] = [copy.deepcopy(pending)]
    store.state["pending"] = copy.deepcopy(pending)
    store.state["recent_auto_outbounds"] = {
        "王哥": [{"text": recent_text, "ts": clock["now"] - 600, "source": "send_attempt"}]
    }
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "王哥",
                "chatPanel": {
                    "latestInbound": "还顺利不？",
                    "latestOutbound": "小红书",
                    "inbound": [{"text": "还顺利不？", "top": 0.72}],
                    "outbound": [{"text": "小红书", "top": 0.16}],
                },
            },
        ]
    )
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([False]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("不该刷新"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    result = runner.tick()
    assert result["status"] == "cancelled", result
    assert result["reason"] == "current_inbound_matches_recent_auto_outbound", result
    assert store.state["pending"] is None
    assert not any(event["type"] == "pending_refreshed_latest" for event in store.events), store.events


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


def run_right_side_bubble_overrides_inbound_color_misclass_path() -> None:
    panel = _extract_chat_panel(
        [
            {
                "text": "后天上",
                "bbox": {"top": 0.361, "left": 0.531, "w": 0.069},
                "bubbleRole": "inbound",
                "greenPixels": 0,
                "grayPixels": 2376,
            },
            {
                "text": "看热闹不嫌事大",
                "bbox": {"top": 0.625, "left": 0.687, "w": 0.191},
                "bubbleRole": "inbound",
                "greenPixels": 0,
                "grayPixels": 8200,
            },
            {
                "text": "我在twitch领箱子",
                "bbox": {"top": 0.722, "left": 0.688, "w": 0.181},
                "bubbleRole": "inbound",
                "greenPixels": 0,
                "grayPixels": 7106,
            },
        ]
    )
    assert panel["latestInbound"] == "后天上", panel
    assert panel["latestOutbound"] == "我在twitch领箱子", panel


def run_manual_reply_cancels_even_with_noisy_inbound_tail_path() -> None:
    store = MemoryStore()
    store.config["roster_sweep_interval_seconds"] = 9999
    fake_ui = FakeUI(
        [
            {
                "status": "ok",
                "visibleChats": [{"name": "May", "preview": "昨晚睡了", "time": "13:44", "unread": True}],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {"latestInbound": "昨晚睡了", "latestOutbound": ""},
            },
            {
                "status": "ok",
                "visibleChats": [],
                "chatPanel": {},
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {
                    "latestInbound": "G",
                    "latestOutbound": "我在twitch领箱子",
                    "inbound": [{"text": "G", "top": 0.84}],
                    "outbound": [
                        {
                            "text": "我在twitch领箱子",
                            "top": 0.72,
                            "left": 0.68,
                            "width": 0.24,
                            "greenPixels": 120,
                        }
                    ],
                },
            },
            {
                "status": "ok",
                "selectionConfirmed": True,
                "activeChat": "May",
                "chatPanel": {
                    "latestInbound": "",
                    "latestOutbound": "我在twitch领箱子",
                    "inbound": [{"text": "G", "top": 0.84}],
                    "outbound": [
                        {
                            "text": "我在twitch领箱子",
                            "top": 0.72,
                            "left": 0.68,
                            "width": 0.24,
                            "greenPixels": 120,
                        }
                    ],
                },
            },
        ]
    )
    clock = {"now": 50_000.0}
    runner = AutoReplyRunner(
        vision_sensor=FakeVision([True]),
        idle_sensor=FakeIdle(45),
        ui=fake_ui,
        llm_client=FakeLLM("那就好，睡饱了才精神，今天加油"),
        load_config_fn=store.load_config,
        load_state_fn=store.load_state,
        save_state_fn=store.save_state,
        append_event_fn=store.append_event,
        now_fn=lambda: clock["now"],
    )

    first = runner.tick()
    assert first["status"] == "draft_saved", first

    clock["now"] = 50_305.0
    second = runner.tick()
    assert second["status"] == "cancelled", second
    assert second["reason"] == "manual_reply_detected", second
    assert store.state["pending"] is None
    assert "send" not in fake_ui.calls


def run_peekaboo_duplicate_json_output_path() -> None:
    first = {"data": {"windows": [{"window_id": 42}]}}
    duplicate = {"data": {"windows": [{"window_id": 99}]}}
    payload = load_first_json_object(f"{json.dumps(first)}\n{json.dumps(duplicate)}\n")
    assert payload == first, payload
    payload = load_first_json_object(f"{json.dumps(first)}\nUnable to find a valid E5 model")
    assert payload == first, payload


def run_warm_avatar_attached_badge_detection_path() -> None:
    row = {
        "rowTop": 0.0356,
        "rowBottom": 0.1631,
        "nameLeft": 0.1063,
    }
    image = Image.new("RGB", (1148, 735), (36, 36, 36))
    draw = ImageDraw.Draw(image)
    draw.rectangle((90, 82, 115, 112), fill=(180, 45, 20))
    draw.ellipse((106, 71, 122, 87), fill=(250, 55, 50))
    draw.line((114, 76, 114, 82), fill=(255, 255, 255), width=2)
    detected = fallback_row_badge_detection(image, row)
    assert detected is not None, detected
    assert detected["numericBadge"] is True, detected
    assert detected["digitPixelCount"] >= 6, detected

    avatar_only = Image.new("RGB", (1148, 735), (36, 36, 36))
    avatar_draw = ImageDraw.Draw(avatar_only)
    avatar_draw.rectangle((90, 82, 115, 112), fill=(180, 45, 20))
    avatar_draw.line((97, 90, 98, 96), fill=(255, 255, 255), width=2)
    assert fallback_row_badge_detection(avatar_only, row) is None


def main() -> int:
    run_happy_path()
    run_manual_reply_cancel()
    run_bottom_green_bubble_cancels_pending_path()
    run_old_outbound_before_inbound_is_not_manual_reply_path()
    run_pending_selection_failure_retries_instead_of_cancel_path()
    run_pending_title_ocr_garbage_uses_panel_preview_evidence_path()
    run_pending_selection_failure_snoozes_after_retry_budget_path()
    run_multi_queue_path()
    run_overdue_pending_bypasses_snoozed_queue_head_path()
    run_follow_up_claim_second_pass_path()
    run_history_marker_trim_path()
    run_card_then_new_messages_prefers_new_message_group_path()
    run_chat_title_near_panel_edge_is_not_replaced_by_message_path()
    run_wechat_login_required_detection_path()
    run_ocr_symbol_tail_trim_path()
    run_preview_matching_outbound_is_not_inbound_path()
    run_latest_message_refresh_path()
    run_send_confirmation_retry_path()
    run_stale_unconfirmed_send_closes_without_opening_path()
    run_unconfirmed_send_selection_failures_have_finite_budget_path()
    run_send_confirmation_self_echo_marks_sent_path()
    run_empty_panel_second_click_recovers_pending_send_path()
    run_historical_outbound_draft_clears_retry_path()
    run_empty_inbound_recheck_retries_pending_path()
    run_compose_text_does_not_count_as_sent()
    run_repeated_identical_text_new_time_path()
    run_ocr_alias_contact_round_trip_path()
    run_claim_preview_fallback_on_panel_mismatch_path()
    run_recent_auto_outbound_preview_is_not_claimed_path()
    run_pending_self_echo_tail_cancels_before_refresh_path()
    run_find_chat_alias_match_path()
    run_capture_cleanup_deletes_old_snapshots_path()
    run_ocr_variant_same_message_does_not_refresh_path()
    run_right_side_bubble_overrides_inbound_color_misclass_path()
    run_manual_reply_cancels_even_with_noisy_inbound_tail_path()
    run_peekaboo_duplicate_json_output_path()
    run_warm_avatar_attached_badge_detection_path()
    run_no_claim_sweep_while_pending_wait_path()
    run_pending_menu_flicker_does_not_trigger_claim_path()
    run_empty_claim_menu_flicker_does_not_reopen_path()
    run_queue_claims_on_menu_rising_path()
    run_queue_claims_while_pending_after_sweep_interval_path()
    run_stale_pending_gc_path()
    run_unknown_menu_signal_does_not_claim_path()
    run_passive_roster_sweep_claims_without_menu_signal_path()
    run_passive_roster_sweep_stays_background_without_badge_path()
    run_passive_roster_sweep_opens_only_after_background_badge_path()
    run_passive_roster_sweep_queues_whitelist_while_pending_path()
    run_passive_roster_sweep_ignores_badge_for_already_queued_contact_path()
    run_claim_persists_pending_before_return_path()
    run_claim_title_ocr_garbage_uses_panel_preview_evidence_path()
    run_claim_selection_failure_persists_one_badge_backed_retry_path()
    run_passive_roster_sweep_ignores_background_non_whitelist_badge_path()
    run_passive_roster_sweep_does_not_clear_non_whitelist_path()
    run_non_whitelist_unread_cleared_path()
    run_active_whitelist_chat_without_unread_badge_skips_path()
    run_active_whitelist_chat_latest_outbound_skips_path()
    run_whitelist_preview_fallback_claim_path()
    run_global_signal_visible_whitelist_without_badge_skips_path()
    run_global_signal_hidden_whitelist_search_skips_path()
    run_unread_whitelist_candidate_latest_outbound_skips_path()
    run_numeric_badge_inbound_bubble_overrides_outbound_text_match_path()
    run_numeric_badge_latest_outbound_text_match_still_skips_path()
    run_empty_queue_persistent_unread_waits_for_signal_change_path()
    run_empty_queue_persistent_unread_suppresses_repeat_claim_path()
    run_same_menu_signal_with_passive_sweep_does_not_reopen_path()
    print("selftest: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
