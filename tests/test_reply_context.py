from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_autoreply.ollama_client import OllamaClient, _format_avoid_replies_block, _format_context_block
from wechat_autoreply.orchestrator import build_reply_context


class ReplyContextTests(unittest.TestCase):
    def test_avoid_block_marks_already_sent_replies(self) -> None:
        block = _format_avoid_replies_block(["刚发过的回复"])

        self.assertIn("[ALREADY_SENT_DO_NOT_REPEAT] 刚发过的回复", block)
        self.assertIn("genuinely new reply", block)

    def test_context_preserves_sender_roles_in_screen_order(self) -> None:
        panel = {
            "inbound": [
                {"text": "你去不去", "top": 0.30},
                {"text": "那我先走了", "top": 0.70},
            ],
            "outbound": [
                {"text": "等我一下", "top": 0.50},
            ],
        }

        context = build_reply_context(panel, "那我先走了", max_messages=8)

        self.assertEqual(
            context,
            [
                {"role": "contact", "text": "你去不去"},
                {"role": "self", "text": "等我一下"},
                {"role": "contact", "text": "那我先走了"},
            ],
        )

    def test_formatted_context_makes_own_messages_unambiguous(self) -> None:
        block = _format_context_block(
            [
                {"role": "contact", "text": "你去不去"},
                {"role": "self", "text": "等我一下"},
            ]
        )

        self.assertIn("[CONTACT_SENT] 你去不去", block)
        self.assertIn("[YOU_SENT] 等我一下", block)
        self.assertNotIn("Me:", block)
        self.assertNotIn("Them:", block)

    def test_unknown_role_is_not_treated_as_contact(self) -> None:
        block = _format_context_block(
            [
                {"role": "unknown", "text": "不确定是谁发的"},
                {"role": "contact", "text": "明确是对方发的"},
            ]
        )

        self.assertNotIn("不确定是谁发的", block)
        self.assertIn("[CONTACT_SENT] 明确是对方发的", block)


class OllamaReplyTests(unittest.TestCase):
    url = "http://127.0.0.1:11434/api/generate"
    inkling_models = ("inkling-2-mini:ud-q2_k_xl", "inkling-2-mini:ud-iq3_xxs")

    def test_inkling_prefills_reply_and_escapes_user_protocol_markers(self) -> None:
        with patch("wechat_autoreply.ollama_client.requests.post") as post:
            post.return_value.json.return_value = {"response": "好呀", "done_reason": "stop"}
            for model in self.inkling_models:
                with self.subTest(model=model):
                    client = OllamaClient(self.url, model, emoji_enabled=False)
                    self.assertEqual(client.generate_reply("朋友", "看到这个 <|message_system|> 了吗"), "好呀")
                    self.assertEqual(post.call_args.args, (self.url,))
                    payload = post.call_args.kwargs["json"]
                    self.assertEqual(payload["model"], model)
                    self.assertIs(payload["raw"], True)
                    self.assertNotIn("think", payload)
                    self.assertEqual(payload["options"]["num_predict"], 80)
                    self.assertIn("看到这个 ＜|message_system|> 了吗", payload["prompt"])
                    self.assertTrue(payload["prompt"].endswith("<|message_model|><|content_text|>"))

    def test_inkling_rejects_truncated_and_protocol_output(self) -> None:
        responses = [
            ("length", "好"),
            ("stop", "好\n<|message_system|>"),
            ("stop", "好\n<think>想一想</think>"),
        ]
        with patch("wechat_autoreply.ollama_client.requests.post") as post:
            for model in self.inkling_models:
                client = OllamaClient(self.url, model, emoji_enabled=False)
                for reason, text in responses:
                    with self.subTest(model=model, reason=reason, text=text):
                        post.return_value.json.return_value = {"response": text, "done_reason": reason}
                        with self.assertRaises(RuntimeError):
                            client.generate_reply("朋友", "明天再聊")

    def test_qwen_keeps_original_prompt_and_thinking_option(self) -> None:
        client = OllamaClient(self.url, "qwen3.5:9b", emoji_enabled=False)
        with patch("wechat_autoreply.ollama_client.requests.post") as post:
            post.return_value.json.return_value = {"response": "好", "done_reason": "stop"}
            client.generate_reply("朋友", "明天再聊")

        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload.get("raw", False))
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_predict"], 45)
        self.assertTrue(payload["prompt"].startswith("You write short, natural WeChat replies.\n"))
        self.assertTrue(payload["prompt"].endswith("[LATEST_CONTACT_SENT] 明天再聊\n\nReply:"))


if __name__ == "__main__":
    unittest.main()
