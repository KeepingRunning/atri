import io
from pathlib import Path
import tempfile
import unittest
import wave

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from scripts import voice_review


class VoiceReviewServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "review"
        self.directory.mkdir()
        (self.directory / "audio").mkdir()
        self.page = b"<!doctype html><title>Local voice review</title>"
        (self.directory / "index.html").write_bytes(self.page)

        data = io.BytesIO()
        with wave.open(data, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\x00\x00\x01\x00" * 100)
        self.wav = data.getvalue()
        self.mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00test audio response"
        (self.directory / "audio/clip.wav").write_bytes(self.wav)
        (self.directory / "audio/clip.mp3").write_bytes(self.mp3)
        (self.directory / "audio/语音 #1.wav").write_bytes(self.wav)

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.wav").write_bytes(b"private outside content")
        (self.directory / "audio/external.wav").symlink_to(outside / "private.wav")
        (self.directory / "audio/linked").symlink_to(outside, target_is_directory=True)
        (self.directory / "audio/unlisted.wav").write_bytes(b"unlisted")
        (self.directory / "catalog.json").write_text('{"private": true}')
        (self.directory / "config.toml").write_text('api_key = "test-only"')
        (self.directory / "audio/folder.wav").mkdir()

        self.catalog = {"items": [
            {"id": "clip", "file": "audio/clip.mp3", "fallback_file": "audio/clip.wav"},
            {"id": "unicode", "file": "audio/语音 #1.wav"},
            {"id": "external", "file": "audio/external.wav"},
            {"id": "linked", "file": "audio/linked/private.wav"},
            {"id": "missing", "file": "audio/missing.wav"},
            {"id": "directory", "file": "audio/folder.wav"},
        ]}
        app = voice_review.create_review_app(self.directory, self.catalog)
        self.client = TestClient(TestServer(app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def test_root_and_index_serve_the_review_page(self):
        for path in ["/", "/index.html"]:
            with self.subTest(path=path):
                async with self.client.get(path) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("text/html", response.content_type)
                    self.assertEqual(self.page, await response.read())

    async def test_audio_bytes_and_mime_types_are_preserved(self):
        for path, content_type, payload in [
            ("/audio/clip.wav", "audio/wav", self.wav),
            ("/audio/clip.mp3", "audio/mpeg", self.mp3),
            ("/audio/语音 #1.wav", "audio/wav", self.wav),
        ]:
            with self.subTest(path=path):
                url = URL().with_path(path)
                async with self.client.get(url) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(content_type, response.content_type)
                    self.assertEqual(payload, await response.read())

    async def test_head_reports_audio_length_without_a_body(self):
        async with self.client.head("/audio/clip.wav") as response:
            self.assertEqual(200, response.status)
            self.assertEqual("audio/wav", response.content_type)
            self.assertEqual(str(len(self.wav)), response.headers["Content-Length"])
            self.assertEqual(b"", await response.read())

    async def test_range_serves_exact_wav_header_and_rejects_outside_range(self):
        async with self.client.get("/audio/clip.wav", headers={"Range": "bytes=0-43"}) as response:
            self.assertEqual(206, response.status)
            self.assertEqual("audio/wav", response.content_type)
            self.assertEqual(f"bytes 0-43/{len(self.wav)}", response.headers["Content-Range"])
            self.assertEqual("44", response.headers["Content-Length"])
            self.assertEqual(self.wav[:44], await response.read())
        async with self.client.get(
            "/audio/clip.wav", headers={"Range": f"bytes={len(self.wav)}-"}
        ) as response:
            self.assertEqual(416, response.status)
            self.assertEqual(f"bytes */{len(self.wav)}", response.headers["Content-Range"])

    async def test_only_catalog_audio_and_page_are_exposed(self):
        for path in [
            "/audio/", "/audio/unlisted.wav", "/catalog.json", "/config.toml",
            "/audio/missing.wav", "/audio/folder.wav", "/audio/folder.wav/",
        ]:
            with self.subTest(path=path):
                async with self.client.get(path) as response:
                    self.assertEqual(404, response.status)

    async def test_listed_files_cannot_escape_through_symlinks(self):
        for path in ["/audio/external.wav", "/audio/linked/private.wav"]:
            with self.subTest(path=path):
                async with self.client.get(path) as response:
                    self.assertEqual(404, response.status)

    async def test_replacing_an_allowed_file_with_an_external_symlink_is_rejected(self):
        audio = self.directory / "audio/clip.wav"
        audio.unlink()
        audio.symlink_to(self.root / "outside/private.wav")
        async with self.client.get("/audio/clip.wav") as response:
            self.assertEqual(404, response.status)

    async def test_encoded_traversal_never_exposes_files_outside_review(self):
        for path in [
            "/%2e%2e/outside/private.wav",
            "/audio/%2e%2e/%2e%2e/outside/private.wav",
            "/audio/..%2f..%2foutside%2fprivate.wav",
            "/audio/%252e%252e/%252e%252e/outside/private.wav",
            "/audio/..%5c..%5coutside%5cprivate.wav",
        ]:
            with self.subTest(path=path):
                async with self.client.get(URL(path, encoded=True)) as response:
                    self.assertEqual(404, response.status)

    async def test_server_does_not_accept_file_writes(self):
        for method in ["POST", "PUT", "DELETE"]:
            with self.subTest(method=method):
                async with self.client.request(method, "/audio/clip.wav", data=b"replace") as response:
                    self.assertIn(response.status, {404, 405})
        self.assertEqual(self.wav, (self.directory / "audio/clip.wav").read_bytes())


if __name__ == "__main__":
    unittest.main()
