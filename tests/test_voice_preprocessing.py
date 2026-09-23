import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from jsonschema import ValidationError

from atri_bot.model import ModelError
from scripts import prepare_voices as voices


def ogg_page(payload, *, serial=17, sequence=0, flags=0, granule=0):
    """Small complete Ogg pages, including CRC, for container boundary tests."""
    segments = [255] * (len(payload) // 255) + [len(payload) % 255]
    raw = bytearray(struct.pack("<4sBBQIIIB", b"OggS", 0, flags, granule,
                                serial, sequence, 0, len(segments)))
    raw.extend(bytes(segments) + payload)
    checksum = 0
    for value in raw:
        checksum ^= value << 24
        for _ in range(8):
            checksum = ((checksum << 1) ^ (0x04C11DB7 if checksum & 0x80000000 else 0)) & 0xFFFFFFFF
    struct.pack_into("<I", raw, 22, checksum)
    return bytes(raw)


def opus(*, serial=17, samples=48000, pre_skip=312):
    header = struct.pack("<8sBBHIhB", b"OpusHead", 1, 1, pre_skip, 48000, 0, 0)
    tags = b"OpusTags" + struct.pack("<II", 0, 0)
    return (ogg_page(header, serial=serial, flags=2)
            + ogg_page(tags, serial=serial, sequence=1)
            + ogg_page(b"\xf8\xff\xfe", serial=serial, sequence=2, flags=4,
                       granule=pre_skip + samples))


def annotation(identity="ATR_b101_001", **overrides):
    return {"id": identity, "title": "打招呼", "description": "台词表达问候，可作为一次简短的打招呼补充。",
            "emotions": ["友好"], "intensity": 1,
            "usage": ["补充问候"], "avoid": [], "recommendation": "everyday",
            "needs_review": False, "review_reason": "", **overrides}


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("voice_pairs.jsonl", "missing_audio.jsonl"):
            (self.source / name).write_text("", encoding="utf-8")

    def append(self, filename, row):
        with (self.source / filename).open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def audio(self, identity, raw=None):
        relative = f"audio/{identity}.opus"
        path = self.source / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(opus() if raw is None else raw)
        return relative, hashlib.sha256(path.read_bytes()).hexdigest()

    def original(self, identity="ATR_b101_001", *, raw=None, **overrides):
        relative, digest = self.audio(identity, raw)
        row = {"voice_id": identity, "voice_character": "アトリ", "voices_in_dialogue": 1,
               "audio_path": relative, "audio_sha256": digest,
               "readable_languages": {"ja": {"speaker": "少女", "text": "「こんにちは。」"},
                                      "zh-Hans": {"speaker": "少女", "text": '"你好。"'}},
               "dialogue_id": f"vol1/{identity}/0/dialogue/1", "file": "b101.ks.scn",
               "section": "story", "scene_index": 0, "text_index": 1, **overrides}
        self.append("voice_pairs.jsonl", row)
        return row

    def test_original_bilingual_text_and_display_name_are_preserved(self):
        original = self.original()
        self.append("voice_pairs.jsonl", {"voice_id": "MIN_b101_001"})
        entries = voices.load_entries(self.source)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text_ja"], "「こんにちは。」")
        self.assertEqual(entries[0]["text_zh"], '"你好。"')
        self.assertEqual(entries[0]["text_source"], "original_script")
        self.assertEqual(entries[0]["translation_source"], "original_game_translation")
        self.assertEqual(entries[0]["source"]["dialogue_id"], original["dialogue_id"])
        self.assertEqual(entries[0]["original_file"], original["audio_path"])
        self.assertEqual(entries[0]["file"], "audio/ATR_b101_001.mp3")
        self.assertEqual(entries[0]["fallback_file"], "audio/ATR_b101_001.wav")

    def test_same_subtitle_keeps_distinct_recordings(self):
        self.original("ATR_b101_001", raw=opus(serial=1))
        self.original("ATR_b101_002", raw=opus(serial=2))
        entries = voices.load_entries(self.source)
        self.assertEqual([item["id"] for item in entries], ["ATR_b101_001", "ATR_b101_002"])
        self.assertEqual(entries[0]["text_ja"], entries[1]["text_ja"])
        self.assertNotEqual(entries[0]["original_sha256"], entries[1]["original_sha256"])

    def test_duplicate_id_and_ambiguous_speaker_are_rejected(self):
        for overrides in ({"voice_character": "水菜萌"}, {"voices_in_dialogue": 2}):
            with self.subTest(overrides=overrides):
                (self.source / "voice_pairs.jsonl").write_text("")
                self.original(**overrides)
                with self.assertRaisesRegex(ValueError, "speaker"):
                    voices.load_entries(self.source)
        (self.source / "voice_pairs.jsonl").write_text("")
        row = self.original()
        self.append("voice_pairs.jsonl", row)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            voices.load_entries(self.source)

    def test_unpaired_number_never_reuses_neighbor_subtitle(self):
        self.original("ATR_b114_026")
        relative, digest = self.audio("ATR_b114_026b")
        self.append("unpaired_audio.jsonl", {"voice_id": "ATR_b114_026b", "audio_path": relative,
                                             "sha256": digest, "source_file": "ATR_b114_026b.opus"})
        entries = voices.load_entries(self.source)
        self.assertEqual([item["id"] for item in entries], ["ATR_b114_026"])

    def test_path_escape_and_changed_audio_are_rejected(self):
        outside = self.root / "outside.opus"
        outside.write_bytes(opus())
        (self.source / "linked.opus").symlink_to(outside)
        for path in ("../outside.opus", str(outside), "linked.opus", "missing.opus"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                voices.safe_audio(self.source, path)
        row = self.original()
        (self.source / row["audio_path"]).write_bytes(opus(samples=96000))
        with self.assertRaisesRegex(ValueError, "Audio changed"):
            voices.load_entries(self.source)

    def test_opus_duration_uses_granules_minus_pre_skip(self):
        self.assertEqual(voices.opus_duration(opus(samples=48000)), 1)
        self.assertEqual(voices.opus_duration(opus(samples=96000, pre_skip=720)), 2)
        invalid = [b"", b"RIFFnot-an-ogg", opus()[:-1], opus()[:28],
                   opus() + opus(serial=18), opus(samples=0)]
        for raw in invalid:
            with self.subTest(bytes=len(raw)), self.assertRaises(ValueError):
                voices.opus_duration(raw)

    def test_incomplete_stream_with_complete_ogg_pages_is_rejected(self):
        raw = opus()
        final_page = raw.rfind(b"OggS")
        unfinished = raw[:final_page] + ogg_page(b"\xf8\xff\xfe", sequence=2, granule=24000)
        with self.assertRaises(ValueError):
            voices.opus_duration(unfinished)

    def test_unchanged_preview_is_reused_but_changed_preview_is_rebuilt(self):
        original = self.original()
        item, = voices.load_entries(self.source)
        directory = self.root / "review"
        (directory / "audio").mkdir(parents=True)
        target = directory / item["file"]
        target.write_bytes(b"cached preview")
        prior = {"original_sha256": item["original_sha256"],
                 "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        with patch.object(voices.subprocess, "run") as convert:
            self.assertEqual(voices.prepare_audio(self.source, directory, item, prior), prior["sha256"])
            convert.assert_not_called()
        target.write_bytes(b"damaged preview")
        def convert(command, **kwargs):
            Path(command[-1]).write_bytes(b"rebuilt preview")
        with patch.object(voices.subprocess, "run", side_effect=convert) as conversion:
            digest = voices.prepare_audio(self.source, directory, item, prior)
        conversion.assert_called_once()
        self.assertEqual(digest, hashlib.sha256(b"rebuilt preview").hexdigest())
        self.assertEqual(target.read_bytes(), b"rebuilt preview")
        self.assertFalse(target.with_suffix(".tmp.mp3").exists())
        self.assertEqual(hashlib.sha256((self.source / original["audio_path"]).read_bytes()).hexdigest(),
                         original["audio_sha256"])

    def test_existing_mp3_preview_does_not_skip_missing_wav_fallback(self):
        original = self.original()
        item, = voices.load_entries(self.source)
        directory = self.root / "review"
        (directory / "audio").mkdir(parents=True)
        mp3 = directory / item["file"]
        mp3.write_bytes(b"existing mp3")
        previous = {"original_sha256": item["original_sha256"],
                    "sha256": hashlib.sha256(mp3.read_bytes()).hexdigest()}

        def convert(command, **kwargs):
            self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
            self.assertEqual(command[command.index("-ar") + 1], "22050")
            self.assertEqual(command[command.index("-ac") + 1], "1")
            Path(command[-1]).write_bytes(b"new wav fallback")

        with patch.object(voices.subprocess, "run", side_effect=convert) as conversion:
            self.assertEqual(voices.prepare_audio(self.source, directory, item, previous), previous["sha256"])
            digest = voices.prepare_audio(self.source, directory, item, previous, fallback=True)
        conversion.assert_called_once()
        self.assertEqual(digest, hashlib.sha256(b"new wav fallback").hexdigest())
        self.assertEqual((directory / item["fallback_file"]).read_bytes(), b"new wav fallback")
        self.assertEqual(mp3.read_bytes(), b"existing mp3")
        self.assertFalse(list((directory / "audio").glob("*.tmp.*")))
        self.assertEqual(hashlib.sha256((self.source / original["audio_path"]).read_bytes()).hexdigest(),
                         original["audio_sha256"])

    def test_damage_to_one_preview_does_not_rebuild_the_other_format(self):
        original = self.original()
        item, = voices.load_entries(self.source)
        directory = self.root / "review"
        (directory / "audio").mkdir(parents=True)
        paths = {False: directory / item["file"], True: directory / item["fallback_file"]}
        cached = {False: b"cached mp3", True: b"cached wav"}
        digests = {fallback: hashlib.sha256(content).hexdigest() for fallback, content in cached.items()}
        previous = {"original_sha256": item["original_sha256"],
                    "sha256": digests[False], "fallback_sha256": digests[True]}

        for damaged in (False, True):
            with self.subTest(damaged_format="wav" if damaged else "mp3"):
                for fallback, path in paths.items():
                    path.write_bytes(cached[fallback])
                with patch.object(voices.subprocess, "run") as conversion:
                    for fallback in (False, True):
                        self.assertEqual(voices.prepare_audio(self.source, directory, item, previous,
                                                              fallback=fallback), digests[fallback])
                    conversion.assert_not_called()

                paths[damaged].write_bytes(b"corrupt preview")
                def convert(command, **kwargs):
                    Path(command[-1]).write_bytes(b"rebuilt preview")
                with patch.object(voices.subprocess, "run", side_effect=convert) as conversion:
                    self.assertEqual(voices.prepare_audio(self.source, directory, item, previous,
                                                          fallback=not damaged), digests[not damaged])
                    digest = voices.prepare_audio(self.source, directory, item, previous, fallback=damaged)
                conversion.assert_called_once()
                self.assertEqual(digest, hashlib.sha256(b"rebuilt preview").hexdigest())
                self.assertEqual(paths[damaged].read_bytes(), b"rebuilt preview")
                self.assertEqual(paths[not damaged].read_bytes(), cached[not damaged])
                self.assertFalse(list((directory / "audio").glob("*.tmp.*")))
                self.assertEqual(hashlib.sha256((self.source / original["audio_path"]).read_bytes()).hexdigest(),
                                 original["audio_sha256"])


class VoiceDescriptionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.batch = [{"id": "ATR_b101_001", "text_zh": "原译文"},
                      {"id": "ATR_b101_002", "text_zh": "你好。"}]
        self.result = [annotation(), annotation("ATR_b101_002")]

    def parse(self, rows):
        return voices.parse_batch(json.dumps({"items": rows}, ensure_ascii=False), self.batch)

    def test_order_is_irrelevant_but_ids_are_preserved(self):
        parsed = self.parse(list(reversed(self.result)))
        self.assertEqual(set(parsed), {row["id"] for row in self.batch})
        self.assertEqual(parsed["ATR_b101_002"]["description"], self.result[1]["description"])

    def test_missing_duplicate_and_unknown_ids_are_rejected(self):
        invalid = [self.result[:1], self.result + [self.result[0]],
                   [self.result[0], annotation("ATR_b101_999")]]
        for rows in invalid:
            with self.subTest(ids=[r["id"] for r in rows]), self.assertRaises(ValueError):
                self.parse(rows)

    def test_model_cannot_rewrite_original_subtitles_or_audio_source(self):
        for field in ("text_ja", "text_zh", "translation_zh", "file", "original_file"):
            rows = deepcopy(self.result)
            rows[0][field] = "模型改写"
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.parse(rows)

    def test_duplicate_json_keys_invalid_outer_shape_and_schema_fail(self):
        valid = json.dumps({"items": self.result})
        invalid = [valid.replace('"title":', '"title": "duplicate", "title":', 1),
                   '{"items": [], "items": []}', '[]', '{"items": null}',
                   '{"items": [], "comment": "unexpected"}']
        for text in invalid:
            with self.subTest(text=text[:80]), self.assertRaises(ValueError):
                voices.parse_batch(text, self.batch)
        for changes in ({"needs_review": "true"}, {"intensity": 5}, {"description": ""},
                        {"emotions": ["高兴", "高兴"]}):
            rows = deepcopy(self.result)
            rows[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.parse(rows)

    def test_review_reason_must_explain_uncertainty(self):
        for needs_review, reason in ((True, " "), (False, "有疑问")):
            rows = deepcopy(self.result)
            rows[0].update(needs_review=needs_review, review_reason=reason)
            with self.subTest(needs_review=needs_review), self.assertRaises(ValueError):
                self.parse(rows)
        notice = voices.punctuation_annotation()
        self.assertTrue(notice["needs_review"])
        self.assertEqual(notice["emotions"], [])
        self.assertEqual(notice["usage"], [])
        self.assertIn("试听", notice["description"])


class VoicePreprocessingResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "source"
        source.mkdir()
        (source / "missing_audio.jsonl").write_text("")
        config = self.root / "config.toml"
        config.write_text('[llm]\nmodel="test-annotation"\napi_key="test-key"\n'
                          'base_url="https://never-contact.invalid/v1"\n')
        self.args = argparse.Namespace(source=source, directory=self.root / "review", config=config,
                                       ids=None, limit=0, batch_size=12, concurrency=1,
                                       prepare_only=False, force=False)
        self.item = {"id": "ATR_b101_001", "original_file": "audio/ATR_b101_001.opus",
                     "original_sha256": "source-hash", "file": "audio/ATR_b101_001.mp3",
                     "fallback_file": "audio/ATR_b101_001.wav",
                     "duration_seconds": 1, "text_ja": "こんにちは。", "text_zh": "你好。",
                     "text_source": "original_script", "translation_source": "original_game_translation",
                     "source": {"section": "story"}, "annotation_basis": "original_script"}
        self.model = AsyncMock()
        self.model.complete.return_value = json.dumps({"items": [annotation(self.item["id"])]})
        for name, kwargs in (("load_entries", {"side_effect": lambda *_: [deepcopy(self.item)]}),
                             ("prepare_audio", {"side_effect": lambda *_, fallback=False:
                                                "fallback-hash" if fallback else "preview-hash"}),
                             ("ChatModel", {"return_value": self.model})):
            patcher = patch.object(voices, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads((self.args.directory / "catalog.json").read_text())

    async def test_resume_keeps_description_without_another_model_call(self):
        self.assertTrue(await voices.run(self.args))
        first, = self.saved()["items"]
        self.assertEqual(first["text_zh"], "你好。")
        self.assertEqual(first["file"], self.item["file"])
        self.assertEqual(first["sha256"], "preview-hash")
        self.assertEqual(first["fallback_file"], self.item["fallback_file"])
        self.assertEqual(first["fallback_sha256"], "fallback-hash")
        self.model.complete.reset_mock()
        self.assertTrue(await voices.run(self.args))
        second, = self.saved()["items"]
        self.model.complete.assert_not_awaited()
        self.assertEqual(second["text_zh"], "你好。")
        self.assertEqual(second["translation_source"], "original_game_translation")
        self.assertEqual(second["description"], first["description"])
        self.assertEqual(second["annotation_signature"], first["annotation_signature"])
        self.assertFalse(self.saved()["sending_enabled"])

    async def test_original_translation_and_text_survive_save_and_resume(self):
        self.item.update(text_source="original_script", translation_source="original_game_translation",
                         text_ja="「おや……？」", text_zh='"哎呀？"')
        self.model.complete.return_value = json.dumps({"items": [annotation(self.item["id"])]})
        await voices.run(self.args)
        self.model.complete.reset_mock()
        self.args.prepare_only = True
        await voices.run(self.args)
        item, = self.saved()["items"]
        self.assertEqual(item["text_ja"], "「おや……？」")
        self.assertEqual(item["text_zh"], '"哎呀？"')
        self.assertEqual(item["translation_source"], "original_game_translation")
        self.model.complete.assert_not_awaited()

    async def test_punctuation_never_triggers_fake_model_description(self):
        self.item.update(text_ja="「…………」", text_zh='"……"')
        await voices.run(self.args)
        item, = self.saved()["items"]
        self.assertTrue(item["needs_review"])
        self.assertEqual(item["emotions"], [])
        self.assertEqual(item["usage"], [])
        self.assertIn("试听", item["review_reason"] + item["description"])
        self.model.complete.assert_not_awaited()

    async def test_changed_source_does_not_keep_completed_description_on_model_failure(self):
        await voices.run(self.args)
        self.item.update(text_ja="さようなら。", text_zh="再见。")
        self.model.complete.side_effect = ModelError("unavailable", "model_network_error")
        self.assertFalse(await voices.run(self.args))
        item, = self.saved()["items"]
        self.assertEqual(item["text_ja"], "さようなら。")
        self.assertEqual(item["text_zh"], "再见。")
        self.assertEqual(item["translation_source"], "original_game_translation")
        self.assertEqual(item["annotation_status"], "failed")
        self.assertNotIn("问候", item["description"])
        self.assertTrue(self.saved()["failures"])

    async def test_failed_batch_preserves_successes_and_resume_only_retries_failure(self):
        items = [deepcopy(self.item), {**self.item, "id": "ATR_b101_002", "file": "audio/ATR_b101_002.mp3",
                                     "fallback_file": "audio/ATR_b101_002.wav"}]
        self.args.batch_size = 1
        should_fail = True
        seen = []
        async def complete(messages, **kwargs):
            identity = json.loads(messages[1]["content"])["items"][0]["id"]
            seen.append(identity)
            if should_fail and identity == "ATR_b101_002":
                raise ModelError("unavailable", "model_network_error")
            return json.dumps({"items": [annotation(identity)]})
        self.model.complete.side_effect = complete
        with patch.object(voices, "load_entries", return_value=items):
            self.assertFalse(await voices.run(self.args))
            statuses = {item["id"]: item["annotation_status"] for item in self.saved()["items"]}
            self.assertEqual(statuses, {"ATR_b101_001": "complete", "ATR_b101_002": "failed"})
            seen.clear()
            should_fail = False
            self.assertTrue(await voices.run(self.args))
        self.assertEqual(seen, ["ATR_b101_002"])
        self.assertEqual(self.saved()["completed"], 2)
        self.assertEqual(self.saved()["failures"], [])


if __name__ == "__main__":
    unittest.main()
