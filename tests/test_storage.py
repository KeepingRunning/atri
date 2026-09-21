from pathlib import Path
import tempfile
import unittest

from atri_bot.storage import read_jsonl


class StorageTests(unittest.TestCase):
    def test_torn_tail_recovered_without_losing_complete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.jsonl"
            path.write_bytes(b'{"kind":"ok"}\n{"torn":')
            self.assertEqual(list(read_jsonl(path)), [{"kind": "ok"}])
            self.assertEqual(len(list(Path(directory).glob('*.torn-*'))), 1)
