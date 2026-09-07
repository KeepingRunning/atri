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
