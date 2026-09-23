import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from atri_bot.tools import ToolError
from atri_bot.voices import VoiceConfig, VoiceLibrary, semantic_group


def mp3():
    # Three complete MPEG-1 Layer III, 64 kbps / 44.1 kHz frames.
    frame = bytes.fromhex("fffb5000") + bytes(204)
    return frame * 3


class VoiceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "voices"
        self.directory.mkdir()
        self.config = VoiceConfig(enabled=True, catalog="voices/catalog.json", selection="voices/selection.json")
        self.rows = []
        self.selected = None

    def add(self, identity="ATR_1", *, raw=None, **changes):
        raw = mp3() if raw is None else raw
        filename = identity + ".mp3"
        (self.directory / filename).write_bytes(raw)
        row = {"id": identity, "file": filename, "sha256": hashlib.sha256(raw).hexdigest(),
               "title": "谢谢", "text_ja": f"「ありがとう{identity}！」", "text_zh": "谢谢你！",
               "duration_seconds": 2, "description": "感谢对方的帮助。", "emotions": ["开心"],
               "intensity": 2, "usage": ["感谢"], "avoid": ["严肃争执"],
               "context_requirements": ["刚得到帮助"], "annotation_status": "complete",
               "needs_review": False, "private_note": "/private/do-not-expose"}
        row.update(changes)
        self.rows.append(row)
        return row

    def library(self):
        (self.directory / "catalog.json").write_text(json.dumps({"items": self.rows}, ensure_ascii=False))
        selected = self.selected if self.selected is not None else {"keep_ids": [row["id"] for row in self.rows]}
        (self.directory / "selection.json").write_text(json.dumps(selected))
        return VoiceLibrary(self.root, self.config)

    def test_only_kept_annotated_lines_are_exposed_without_deleting_files(self):
        self.add("V1")
        self.add("V2", annotation_status="pending", needs_review=True)
        self.add("V3", needs_review=True)
        self.add("V4", text_ja="「……！」", text_zh="……")
        self.add("V5")
        self.selected = {"keep_ids": ["V1", "V2", "V3", "V4"], "delete_ids": ["V5"]}
        library = self.library()
        self.assertEqual(library.stats, {"total": 5, "retained": 4, "available": 1,
                                         "pending": 1, "needs_review": 1, "punctuation": 1, "unselected": 1})
        self.assertEqual([item["id"] for item in library.search("感谢")], ["V1"])
        self.assertTrue((self.directory / "V5.mp3").exists())
        for identity in ("V2", "V3", "V4", "V5"):
            with self.subTest(identity=identity), self.assertRaises(ToolError):
                library.get(identity)
        item = library.get("V1")
        for private in ("file", "sha256", "private_note", "needs_review", "annotation_status"):
            self.assertNotIn(private, item)
        item["usage"].clear()
        self.assertEqual(library.get("V1")["usage"], ["感谢"])
        self.assertEqual(library.get("V1")["context_requirements"], ["刚得到帮助"])

    def test_zero_available_is_valid_and_disabled_does_not_read_files(self):
        self.assertEqual(len(VoiceLibrary(self.root, VoiceConfig())), 0)
        self.add(annotation_status="pending", needs_review=True)
        library = self.library()
        self.assertEqual(len(library), 0)
        self.assertEqual(library.stats["pending"], 1)
        self.assertEqual(library.search("谢谢"), [])

    def test_short_preference_and_no_match(self):
        self.add("V1", duration_seconds=14)
        self.add("V2", duration_seconds=2)
        library = self.library()
        self.assertEqual([row["id"] for row in library.search("谢谢")], ["V2", "V1"])
        self.assertEqual(library.search("天文学宇宙"), [])

    def test_semantic_groups_ignore_punctuation_and_normalize_width(self):
        first = self.add("V1", text_ja="「ＡＴＲＩ、ありがとう！」", duration_seconds=2)
        self.add("V2", text_ja="ATRI ありがとう。", duration_seconds=3, semantic_group="untrusted")
        self.add("V3", text_ja="「助かりました！」", duration_seconds=2)
        library = self.library()
        group = library.get("V1")["semantic_group"]
        self.assertEqual(group, semantic_group(first["text_ja"]))
        self.assertEqual(group, library.get("V2")["semantic_group"])
        self.assertRegex(group, r"^[a-f0-9]{64}$")
        result = library.search("谢谢")
        self.assertEqual([row["id"] for row in result], ["V1", "V3"])
        self.assertEqual(library.search("谢谢", recent_ids=["V1"])[0]["id"], "V3")
        self.assertEqual(library.search("谢谢", recent_groups=[group])[0]["id"], "V3")
        self.assertEqual(library.search("谢谢")[0]["id"], "V1")

    def test_avoid_and_context_are_returned_for_model_to_judge(self):
        self.add("V1", text_ja="「おやすみ、夏生さん」", text_zh="晚安，夏生先生。",
                 title="晚安", description="向夏生道晚安。", avoid=["普通群友"],
                 context_requirements=["对方确实是夏生"], usage=["晚安"], emotions=["平静"])
        library = self.library()
        self.assertEqual(library.search("晚安")[0]["avoid"], ["普通群友"])
        self.assertEqual(library.search("普通群友"), [])

    def test_matching_only_an_avoid_condition_does_not_retrieve_a_voice(self):
        self.add("V1", text_ja="「おはよう」", text_zh="早上好。", title="早安",
                 description="早晨问候。", usage=["起床打招呼"], emotions=["平静"],
                 context_requirements=[], avoid=["感谢"])
        library = self.library()
        self.assertEqual(library.search("感谢"), [])
        self.assertEqual(library.search("早安")[0]["avoid"], ["感谢"])

    def test_prepare_preserves_original_bytes_and_returns_record(self):
        row = self.add()
        library = self.library()
        segment, metadata = library.prepare(row["id"])
        self.assertEqual(segment["type"], "record")
        self.assertEqual(base64.b64decode(segment["data"]["file"].removeprefix("base64://")), mp3())
        self.assertEqual(metadata["text_zh"], row["text_zh"])
        self.assertEqual(metadata["sha256"], row["sha256"])
        self.assertNotIn("file", metadata)
        self.assertNotIn("base64", json.dumps(metadata))
        self.assertEqual((self.directory / row["file"]).read_bytes(), mp3())

    def test_prepare_rechecks_changed_missing_large_and_invalid_media(self):
        row = self.add()
        library = self.library()
        file = self.directory / row["file"]
        file.write_bytes(b"tampered")
        with self.assertRaises(ToolError) as error:
            library.prepare(row["id"])
        self.assertEqual(error.exception.code, "voice_changed")
        file.unlink()
        with self.assertRaises(ToolError) as error:
            library.prepare(row["id"])
        self.assertEqual(error.exception.code, "voice_file_unavailable")
        file.write_bytes(mp3() * 3)
        row["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        self.config.max_audio_bytes = 1024
        library = self.library()
        with self.assertRaises(ToolError) as error:
            library.prepare(row["id"])
        self.assertEqual(error.exception.code, "voice_too_large")

    def test_prepare_rejects_header_only_disguised_and_truncated_audio(self):
        for raw in (b"RIFF" + bytes(200), b"ID3" + bytes(20), bytes.fromhex("fffb5000"), mp3()[:250]):
            self.rows.clear()
            self.add(raw=raw)
            with self.subTest(raw=raw[:8]), self.assertRaises(ToolError) as error:
                self.library().prepare("ATR_1")
            self.assertEqual(error.exception.code, "voice_invalid_audio")

    def test_valid_id3_tag_is_skipped(self):
        raw = b"ID3\x04\x00\x00\x00\x00\x00\x04" + b"test" + mp3()
        self.add(raw=raw)
        segment, _ = self.library().prepare("ATR_1")
        self.assertEqual(base64.b64decode(segment["data"]["file"][9:]), raw)

    def test_path_traversal_remote_absolute_and_symlink_paths_are_rejected(self):
        row = self.add()
        outside = self.root / "outside.mp3"
        outside.write_bytes(mp3())
        (self.directory / "escaped.mp3").symlink_to(outside)
        for name in ("../outside.mp3", "https://example.test/audio.mp3", str(outside),
                     "audio\\x.mp3", "escaped.mp3", "file:\0x.mp3", "wrong.wav"):
            row["file"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.library()

    def test_prepare_rechecks_symlink_targets(self):
        row = self.add()
        library = self.library()
        outside = self.root / "outside.mp3"
        outside.write_bytes(mp3())
        file = self.directory / row["file"]
        file.unlink()
        file.symlink_to(outside)
        with self.assertRaises(ToolError) as error:
            library.prepare(row["id"])
        self.assertEqual(error.exception.code, "voice_invalid_path")

    def test_catalog_and_selection_cannot_escape_root(self):
        for field in ("catalog", "selection"):
            for value in ("../voice.json", "/tmp/catalog.json", "https://example.test/catalog.json"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    VoiceConfig(**{field: value}).validate()
        with tempfile.TemporaryDirectory() as directory:
            (self.root / "external").symlink_to(directory, target_is_directory=True)
            self.config.catalog = "external/catalog.json"
            with self.assertRaises(ValueError):
                VoiceLibrary(self.root, self.config)

    def test_invalid_catalog_fields_and_duplicate_ids_fail(self):
        row = self.add()
        original = deepcopy(row)
        changes = ({"id": "../x"}, {"sha256": "bad"}, {"duration_seconds": True},
                   {"duration_seconds": 0}, {"duration_seconds": 601}, {"duration_seconds": float("nan")},
                   {"text_ja": ""}, {"text_zh": "x" * 1501}, {"title": "x" * 101},
                   {"description": "x" * 1001}, {"usage": "wrong"}, {"avoid": [""]},
                   {"intensity": True}, {"annotation_status": "maybe"}, {"needs_review": 1},
                   {"context_requirements": "wrong"})
        for change in changes:
            row.clear()
            row.update(original, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.library()
        row.clear()
        row.update(original)
        self.rows.append(deepcopy(row))
        with self.assertRaises(ValueError):
            self.library()

    def test_unknown_duplicate_and_conflicting_selections_fail(self):
        self.add()
        for selected in ({}, {"keep_ids": "all"}, {"keep_ids": ["missing"]},
                         {"keep_ids": ["ATR_1", "ATR_1"]}, {"keep_ids": ["ATR_1"], "delete_ids": ["ATR_1"]},
                         {"keep_ids": [], "delete_ids": ["missing"]}):
            self.selected = selected
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.library()

    def test_invalid_query_and_limits_fail(self):
        self.add()
        library = self.library()
        for query in ("", " ", 1, "x" * 501):
            with self.subTest(query=query), self.assertRaises(ToolError):
                library.search(query)
        for limit in (0, True, 21, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ToolError):
                library.search("谢谢", limit)

    def test_config_values_fail_closed(self):
        for kwargs in ({"enabled": 1}, {"search_limit": True}, {"catalog": ""},
                       {"selection": ""}, {"target_turns_min": 11},
                       {"max_audio_bytes": 512}, {"preferred_max_seconds": True},
                       {"preferred_max_seconds": float("nan")}, {"preferred_max_seconds": float("inf")},
                       {"preferred_max_seconds": 0}, {"preferred_max_seconds": 61}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                VoiceConfig(**kwargs).validate()
