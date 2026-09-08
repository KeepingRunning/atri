import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.config import Config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
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
        self.assertEqual(config.reply.mode, 'willingness')
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
