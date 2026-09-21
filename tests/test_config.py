import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.config import Config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_stickers_require_planner_and_tools_and_templates_match(self):
        import tomllib
        ordinary = tomllib.loads((ROOT / 'config.toml.template').read_text())['stickers']
        container = tomllib.loads((ROOT / 'deploy/config.toml.template').read_text())['stickers']
        self.assertEqual(ordinary, container)
        self.assertFalse(Config.load(ROOT / 'config.toml.template').stickers.enabled)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for prefix in ('', '[reply]\nmode="willingness"\n',
                           '[reply]\nmode="planner"\n[tools]\nenabled=false\n'):
                path.write_text(prefix + '[stickers]\nenabled=true\n')
                with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'stickers.enabled'):
                    Config.load(path)
            path.write_text('[reply]\nmode="planner"\n[stickers]\nenabled=true\n')
            configured = Config.load(path)
            self.assertTrue(configured.stickers.enabled)
            self.assertEqual(configured.stickers.target_turns_min, 3)
            self.assertEqual(configured.stickers.target_turns_max, 5)
            self.assertEqual(configured.stickers.max_age_seconds, 20)
            path.write_text('[stickers]\nunrecognized=true\n')
            with self.assertRaisesRegex(ValueError, 'Invalid \\[stickers\\]'):
                Config.load(path)

    def test_video_retention_defaults_and_cloud_enable_requires_key(self):
        config = Config.load(ROOT / "config.toml.template")
        self.assertEqual(config.links.cache_ttl_seconds, 86400)
        self.assertEqual(config.history_seconds, 3600)
        self.assertFalse(config.asr.enabled)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[asr]\nenabled=true\n')
            with self.assertRaisesRegex(ValueError, "asr.api_key"):
                Config.load(path)
            path.write_text('[asr]\nenabled=true\napi_key="test-cloud-key"\n')
            config = Config.load(path)
            self.assertTrue(config.asr.enabled)
            self.assertEqual(config.asr.timeout, 300)
            self.assertNotIn("test-cloud-key", repr(config))

    def test_context_uses_seconds_and_ignores_retired_count_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for text, expected in (('', 3600), ('[context]\nrecent_messages=1\n', 3600),
                                   ('[context]\nhistory_seconds=1800\nrecent_messages=1\n', 1800)):
                path.write_text(text)
                config = Config.load(path)
                self.assertEqual(config.history_seconds, expected)
                self.assertFalse(hasattr(config, 'recent_messages'))
            for value in ('0', '-1', 'true', '"3600"', '3600.5', 'inf', 'nan'):
                path.write_text('[context]\nhistory_seconds=' + value)
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'context.history_seconds'):
                    Config.load(path)

    def test_toml_is_the_only_source_even_when_environment_is_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = (ROOT / "config.toml.template").read_text()
            for old, new in (
                ('allowed_groups = []', 'allowed_groups = [123]'),
                ('self_id = ""', 'self_id = "456"'),
                ('token = ""', 'token = "test-qq-token"'),
                ('base_url = ""', 'base_url = "https://example.com/v1"'),
                ('model = ""', 'model = "test-model"'),
                ('api_key = ""', 'api_key = "test-api-key"'),
            ):
                text = text.replace(old, new)
            path = root / "config.toml"
            path.write_text(text)
            (root / "personal_info.txt").write_text("测试人设")
            env = {name: "conflicting-value" for name in (
                "ATRI_SELF_ID", "ATRI_ONEBOT_TOKEN", "ATRI_API_BASE", "ATRI_MODEL", "ATRI_API_KEY")}
            with patch.dict(os.environ, env):
                config = Config.load(path)
                config.require_serve()
            self.assertEqual(config.self_id, "456")
            self.assertEqual(config.groups, frozenset({"123"}))
            self.assertEqual(config.token, "test-qq-token")
            self.assertEqual(config.api_key, "test-api-key")
            self.assertEqual(config.base_url, "https://example.com/v1")
            self.assertEqual(config.model, "test-model")
            self.assertEqual(config.data, (root / "data").resolve())
            self.assertEqual(config.read_personal_info(), "测试人设")
            self.assertNotIn("test-api-key", repr(config))
            self.assertNotIn("test-qq-token", repr(config))

    def test_template_requires_credentials_and_cannot_fall_back_to_environment(self):
        with patch.dict(os.environ, {"ATRI_API_KEY": "test-secret"}):
            config = Config.load(ROOT / "config.toml.template")
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.token, "")
        self.assertEqual(config.self_id, "")
        self.assertFalse(config.groups)
        with self.assertRaisesRegex(ValueError, "llm.api_key in config.toml"):
            config.require_live()

    def test_reply_configuration_and_legacy_mode(self):
        config = Config.load(ROOT / 'config.toml.template')
        self.assertEqual(config.reply.mode, 'planner')
        self.assertEqual(config.reply.threshold, 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[reply]\nmode="at_only"\nfrequency=0\nnames=["小亚"]\njudgment_model="fast"\n')
            config = Config.load(path)
            self.assertEqual(config.reply.mode, 'at_only')
            self.assertEqual(config.reply.names, ['小亚'])
            self.assertEqual(config.reply.judgment_model, 'fast')

    def test_optional_thinking_configuration(self):
        self.assertEqual(Config.load(ROOT / 'config.toml.template').thinking, '')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for mode in ('enabled', 'disabled', ''):
                path.write_text(f'[llm]\nthinking="{mode}"\n')
                self.assertEqual(Config.load(path).thinking, mode)
            for value in ('true', '42', '"auto"', '[]'):
                path.write_text(f'[llm]\nthinking={value}\n')
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'llm.thinking'):
                    Config.load(path)

    def test_invalid_planner_limits_are_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for value in ('max_waits=true', 'max_replans=-1', 'max_batch_messages=0',
                          'max_batch_seconds=1', 'debounce_seconds=nan', 'max_wait_seconds=inf',
                          'max_snapshot_chars=100', 'max_output_tokens=1', 'temperature=nan', 'temperature=3', 'typo=1'):
                path.write_text('[planner]\n' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)

    def test_invalid_reply_configuration_is_rejected_at_load(self):
        invalid = ['frequency=nan', 'frequency=inf', 'frequency=-0.1', 'frequency=1.1',
                   'frequency=true', 'threshold=0', 'threshold=60.5', 'threshold=true',
                   'cooldown_seconds=-1', 'continuation_seconds=nan', 'max_message_age_seconds=0',
                   'mode="random"', 'names="ATRI"', 'names=[""]', 'judgment_model=42', 'typo=1']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for value in invalid:
                path.write_text('[reply]\n' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)

    def test_logging_defaults_and_per_module_configuration(self):
        config = Config.load(ROOT / 'config.toml.template')
        self.assertEqual(config.logging.level, 'DEBUG')
        self.assertEqual(config.logging.color, 'auto')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[logging]\nlevel="INFO"\ncolor="always"\nfile=""\n[logging.modules]\nwillingness="DEBUG"\n')
            config = Config.load(path)
            self.assertEqual(config.logging.modules, {'willingness': 'DEBUG'})
            self.assertEqual(config.logging.file, '')

    def test_invalid_logging_options_fail_during_load(self):
        invalid = ['level="TRACE"', 'color="rainbow"', 'max_bytes=0', 'backup_count=-1',
                   'preview_chars=-1', 'preview_chars=true', 'file=42', 'modules=[]',
                   'modules={willingness=["DEBUG"]}', 'modules={""="DEBUG"}', 'typo=1']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for value in invalid:
                path.write_text('[logging]\n' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)

    def test_short_link_dns_configuration_is_independent_and_boolean(self):
        config = Config.load(ROOT / 'config.toml.template')
        self.assertFalse(config.links.dns_over_https)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[links]\ndns_over_https=true\n[asr]\nenabled=false\ndns_over_https=false\n')
            config = Config.load(path)
            self.assertTrue(config.links.dns_over_https)
            self.assertFalse(config.asr.enabled)
            self.assertFalse(config.asr.dns_over_https)
            for value in ('1', '"true"', '[]'):
                path.write_text('[links]\ndns_over_https=' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)

    def test_document_defaults_and_independent_model_configuration(self):
        config = Config.load(ROOT / 'config.toml.template')
        self.assertTrue(config.documents.enabled)
        self.assertEqual(config.documents.chunk_chars, 1200)
        self.assertEqual(config.documents.input_token_budget, 48000)
        self.assertEqual(config.documents.timeout, 60)
        self.assertEqual(config.documents.max_model_calls, 8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[llm]\nmodel="chat-model"\n[documents]\nenabled=false\nmodel="reader-model"\n')
            config = Config.load(path)
            self.assertFalse(config.documents.enabled)
            self.assertEqual(config.documents.model, 'reader-model')
            self.assertEqual(config.model, 'chat-model')

    def test_invalid_document_configuration_is_rejected_at_load(self):
        invalid = ['enabled=1', 'chunk_chars=true', 'chunk_chars=199', 'chunk_chars=8001',
                   'overview_min_chars=-1', 'input_token_budget=3999', 'input_token_budget=inf',
                   'max_output_tokens=255', 'timeout=nan', 'timeout=inf', 'timeout=0',
                   'timeout=true', 'timeout=121', 'max_model_calls=0', 'max_model_calls=33',
                   'max_model_calls=1.5', 'model=1', 'model="bad\\nname"', 'typo=1']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for value in invalid:
                path.write_text('[documents]\n' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)
