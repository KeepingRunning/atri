import json
from pathlib import Path
import tempfile
import threading
import unittest

from atri_bot.history_tools import ChatArchive, history_registry
from atri_bot.storage import GroupLog, delivery_text, voice_metadata


VOICE = {"id": "ATR_b101_013", "text_ja": "もちろん、高性能ですから！",
         "text_zh": "当然，我是高性能的嘛！", "duration_seconds": 2.7,
         "semantic_group": "same-recorded-line", "title": "高性能"}
STICKER = {"id": "happy", "description": "亚托莉高兴地挥手。"}


class VoiceMetadataTests(unittest.TestCase):
    def test_history_uses_verified_subtitles_and_excludes_generated_claims_and_audio_data(self):
        supplied = {**VOICE, "description": "我正在游泳", "usage": "向夏生告白", "reason": "需要亲密互动",
                    "file": "/private/audio.mp3", "data": "base64://abc", "sha256": "secret-hash"}
        metadata = voice_metadata(supplied)
        self.assertEqual(metadata, VOICE)
        rendered = delivery_text({"text": "", "voice": supplied})
        self.assertEqual(rendered, "[日语语音 ATR_b101_013，2.7秒；台词原文：もちろん、高性能ですから！；"
                                  "台词译文：当然，我是高性能的嘛！]")
        for text in (json.dumps(metadata, ensure_ascii=False), rendered):
            for excluded in ("我正在游泳", "向夏生告白", "需要亲密互动", "/private", "base64", "secret-hash"):
                self.assertNotIn(excluded, text)
        self.assertEqual(delivery_text({"text": "之前的文字", "sticker": STICKER}),
                         "之前的文字\n[表情 happy：亚托莉高兴地挥手。]")

    def test_metadata_rejects_invalid_recording_identity_duration_and_missing_subtitles(self):
        invalid = [{"id": "../audio"}, {"id": "x" * 81}, {"text_ja": ""}, {"text_zh": None},
                   {"semantic_group": ""}, {"semantic_group": "x" * 121}, {"duration_seconds": True},
                   {"duration_seconds": float("nan")}, {"duration_seconds": float("inf")},
                   {"duration_seconds": 0}, {"duration_seconds": 601}]
        for change in invalid:
            with self.subTest(change=change):
                self.assertIsNone(voice_metadata({**VOICE, **change}))
        self.assertIsNone(voice_metadata(None))
        bounded = voice_metadata({**VOICE, "text_ja": "あ" * 2000, "text_zh": "好" * 2000,
                                  "title": "测试" * 100})
        self.assertEqual(len(bounded["text_ja"]), 1500)
        self.assertEqual(len(bounded["text_zh"]), 1500)
        self.assertEqual(len(bounded["title"]), 120)


class VoiceStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = 1000
        self.group = GroupLog(self.root, "1", now=lambda: self.clock)

    def append(self, row, group=None):
        self.clock += 1
        (group or self.group).append(row)

    def text(self, number, *, group=None, **extra):
        group = group or self.group
        gid = group.directory.name
        row = {"kind": "delivery", "key": f"99:{gid}:{number}", "status": "sent",
               "text": "普通聊天", "message_id": f"out-{number}", "turn_id": f"turn-{number}", **extra}
        self.append(row, group)
        return row

    def supplement(self, number, kind="voice", *, status="sent", suffix="", **extra):
        metadata = VOICE if kind == "voice" else STICKER
        row = {"kind": "delivery", "key": f"99:1:{number}:{kind}{suffix}", "status": status,
               "text": "", "message_id": f"{kind}-{number}{suffix}", "turn_id": f"turn-{number}",
               "parent_message_id": f"out-{number}", "delivery_origin": f"{kind}_supplement",
               kind: metadata, **extra}
        self.append(row)
        return row

    def test_voice_is_an_independent_history_message_without_new_turn_or_cooldown(self):
        self.text(1)
        last_sent = self.group.last_sent
        activity = list(self.group.activity)
        self.supplement(1)
        self.assertIs(self.group.last_sent, last_sent)
        self.assertEqual(list(self.group.activity), activity)
        self.assertEqual(len(self.group.history), 2)
        self.assertEqual(self.group.history[-1]["voice"], VOICE)
        self.assertIn("台词译文：当然", self.group.history[-1]["text"])
        self.assertIn("voice-1", self.group.sent_message_ids)
        state = self.group.supplement_state()
        self.assertEqual((state["recent_turns"], state["recent_supplement_count"],
                          state["turns_since_last_supplement"], state["turns_since_last_voice"]), (1, 1, 0, 0))

    def test_failed_unknown_pending_and_restarted_pending_do_not_enter_history_or_frequency(self):
        self.text(1)
        expected = self.group.supplement_state()
        for status in ("failed", "unknown", "pending"):
            self.supplement(1, status=status, suffix=status)
        self.assertEqual(len(self.group.history), 1)
        self.assertEqual(self.group.supplement_state(), expected)
        restored = GroupLog(self.root, "1", now=lambda: self.clock)
        self.assertEqual(len(restored.history), 1)
        self.assertEqual(restored.supplement_state(), expected)
        self.assertEqual(restored.last_receipts["99:1:1:voicepending"]["status"], "unknown")

    def test_sticker_and_voice_share_frequency_but_voice_has_its_own_spacing(self):
        self.text(1)
        self.supplement(1)
        self.text(2)
        self.supplement(2, "sticker")
        self.text(3)
        state = self.group.supplement_state()
        self.assertEqual(state["turns_since_last_supplement"], 1)
        self.assertEqual(state["turns_since_last_voice"], 2)
        self.assertEqual(state["recent_supplement_count"], 2)
        self.assertEqual(state["recent_sticker_ids"], ["happy"])
        self.assertEqual(state["recent_voice_ids"], [VOICE["id"]])
        self.assertEqual(state["recent_voice_groups"], [VOICE["semantic_group"]])
        self.assertEqual(self.group.sticker_state()["turns_since_last_sticker"], 1)

    def test_late_acknowledgements_use_parent_order_and_replay_is_identical(self):
        self.text(1)
        self.text(2)
        self.text(3)
        self.supplement(3)
        late = self.supplement(1, "sticker")
        self.append({**late, "key": "99:1:duplicate-receipt"})
        state = self.group.supplement_state()
        self.assertEqual(state["recent_turns"], 3)
        self.assertEqual(state["recent_supplement_count"], 2)
        self.assertEqual(state["turns_since_last_supplement"], 0)
        self.assertEqual(state["turns_since_last_voice"], 0)
        self.assertEqual(self.group.sticker_state()["turns_since_last_sticker"], 2)
        self.assertEqual(self.group.last_sent["message_id"], "out-3")
        self.assertEqual(len(self.group.history), 5)
        restored = GroupLog(self.root, "1", now=lambda: self.clock)
        self.assertEqual(restored.supplement_state(), state)
        self.assertEqual(restored.sticker_state(), self.group.sticker_state())
        self.assertEqual(list(restored.history), list(self.group.history))

    def test_same_subtitle_variants_and_recent_window_remain_group_scoped(self):
        self.text(1)
        self.supplement(1)
        self.text(2)
        self.supplement(2, voice={**VOICE, "id": "ATR_variant_001"})
        self.text(3)
        state = self.group.supplement_state(window=2, target_min=2, target_max=4)
        self.assertEqual(state["recent_voice_ids"], ["ATR_variant_001"])
        self.assertEqual(state["recent_voice_groups"], [VOICE["semantic_group"]])
        self.assertEqual(state["recent_turns"], 2)
        self.assertEqual((state["target_turns_min"], state["target_turns_max"]), (2, 4))
        other = GroupLog(self.root, "2", now=lambda: self.clock)
        self.text(1, group=other)
        self.assertEqual(other.supplement_state()["recent_voice_ids"], [])
        self.assertEqual(other.supplement_state()["turns_since_last_supplement"], 1)

    def test_late_receipt_outside_recent_window_keeps_exact_total_spacing(self):
        for number in range(1, 126):
            self.text(number)
        self.assertEqual(self.group.supplement_state()["turns_since_last_supplement"], 125)
        self.supplement(1)
        state = self.group.supplement_state(window=100)
        self.assertEqual(len(self.group._chat_turns), 100)
        self.assertEqual(state["turns_since_last_supplement"], 124)
        self.assertEqual(state["turns_since_last_voice"], 124)
        self.assertEqual(state["recent_voice_ids"], [])
        self.assertEqual(state["last_voice_id"], VOICE["id"])
        restored = GroupLog(self.root, "1", now=lambda: self.clock)
        self.assertEqual(restored.supplement_state(window=100), state)
        for window in (0, 101, True):
            with self.assertRaises(ValueError):
                self.group.supplement_state(window=window)

    def test_legacy_inline_stickers_count_and_health_repetition_do_not(self):
        self.text(1, sticker=STICKER)
        self.text(2)
        self.text(3, delivery_origin="repetition")
        self.append({"kind": "command", "key": "99:1:health", "status": "sent", "text": "/health"})
        state = self.group.supplement_state()
        self.assertEqual(state["recent_turns"], 2)
        self.assertEqual(state["recent_supplement_count"], 1)
        self.assertEqual(state["turns_since_last_supplement"], 1)
        self.assertEqual(state["turns_since_last_voice"], 2)
        self.assertEqual(state["recent_sticker_ids"], ["happy"])

    def test_supplement_requires_both_parent_identifiers_and_counts_at_most_once(self):
        self.text(1)
        self.supplement(1, suffix="wrong-turn", turn_id="unrelated")
        self.supplement(1, suffix="wrong-parent", parent_message_id="other")
        self.assertEqual(self.group.supplement_state()["recent_supplement_count"], 0)
        self.supplement(1)
        self.supplement(1, "sticker")
        self.assertEqual(self.group.supplement_state()["recent_supplement_count"], 1)
        self.assertEqual(self.group.supplement_state()["recent_voice_count"], 1)
        # Archive the actual confirmed messages even if a malformed sender sent
        # two supplements; never pretend only one reached the group.
        self.assertEqual(len(self.group.history), 5)

    def test_archive_search_and_events_include_only_confirmed_safe_voice_metadata(self):
        self.text(1)
        self.supplement(1, voice={**VOICE, "description": "误把用途当成说过的话", "file": "/private/audio",
                                  "base64": "secret-audio"})
        self.supplement(1, status="failed", suffix="failed", voice={**VOICE, "text_zh": "未发送的内容"})
        self.append({"kind": "supplement_plan", "key": "99:1:1", "parent_message_id": "out-1",
                     "status": "voice", "voice_id": VOICE["id"], "file": "/private/audio"})
        archive = ChatArchive(self.group.path, group_id="1", self_id="99", now=self.clock, exclude_key="")
        result = archive.search({"query": "高性能"}, threading.Event())
        self.assertEqual(result.data["matched_total"], 1)
        self.assertEqual(result.data["items"][0]["voice"], VOICE)
        self.assertEqual(archive.search({"query": "未发送的内容"}, threading.Event()).data["matched_total"], 0)
        events = archive.events({"message_id": "out-1"}, threading.Event())
        confirmed = [item for item in events.data["items"] if item.get("status") == "sent"]
        self.assertEqual(confirmed[1]["voice"], VOICE)
        self.assertTrue(any(item["kind"] == "supplement_plan" for item in events.data["items"]))
        self.assertTrue(all("voice" not in item for item in events.data["items"] if item.get("status") != "sent"))
        serialized = result.to_json() + events.to_json()
        for excluded in ("误把用途", "/private", "secret-audio", "未发送的内容"):
            self.assertNotIn(excluded, serialized)
        self.assertIn("supplement_plan", str(history_registry().definitions()))
