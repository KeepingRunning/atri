import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from atri_bot.stickers import StickerConfig, StickerLibrary, register_stickers
from atri_bot.tools import ToolError, ToolRegistry, ToolsConfig


def picture(fmt="PNG", colors=("red",), durations=None):
    output = BytesIO()
    frames = [Image.new("RGBA", (24, 24), color) for color in colors]
    kwargs = {"save_all": True, "append_images": frames[1:], "duration": durations or [100] * len(frames), "loop": 0} if len(frames) > 1 else {}
    if fmt == "WEBP":
        kwargs["lossless"] = True
    frames[0].save(output, format=fmt, **kwargs)
    return output.getvalue()


class StickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "stickers"
        self.directory.mkdir()
        self.config = StickerConfig(enabled=True, catalog="stickers/catalog.json")
        self.rows = []

    def tearDown(self):
        self.temp.cleanup()

    def add(self, identity, *, raw=None, **changes):
        raw = picture() if raw is None else raw
        file = identity + ".asset"
        (self.directory / file).write_bytes(raw)
        row = {"id": identity, "file": file, "sha256": hashlib.sha256(raw).hexdigest(),
               "title": "欢呼", "description": "少女举起双手，满脸开心。", "visible_text": ["好耶"],
               "emotions": ["高兴"], "intensity": 2, "usage": ["庆祝成功"], "avoid": [],
               "animation_summary": "", "needs_review": False, "private_note": "/private/never-expose"}
        row.update(changes)
        self.rows.append(row)
        return row

    def library(self):
        (self.directory / "catalog.json").write_text(json.dumps({"items": self.rows}, ensure_ascii=False))
        return StickerLibrary(self.root, self.config)

    def test_review_filter_projection_and_no_match_are_not_random(self):
        self.add("S1", avoid=["网络故障"])
        self.add("S2", needs_review=True)
        library = self.library()
        self.assertEqual(len(library), 1)
        self.assertEqual(library.search("网络故障"), [])
        self.assertEqual(library.search("火山喷发"), [])
        result = library.search("高兴 庆祝")
        self.assertEqual([row["id"] for row in result], ["S1"])
        self.assertNotIn("file", result[0])
        self.assertNotIn("sha256", result[0])
        self.assertNotIn("private_note", result[0])
        result[0]["visible_text"].clear()
        self.assertEqual(library.get("S1")["visible_text"], ["好耶"])
        with self.assertRaises(ToolError):
            library.get("S2")

    def test_chinese_phrase_and_visible_text_matching(self):
        self.add("S1", visible_text=["谢谢你"])
        self.add("S2", title="安静休息", description="少女闭眼打盹。", visible_text=[], emotions=["困倦"], usage=["准备睡觉"])
        library = self.library()
        self.assertEqual(library.search("感谢 谢谢你")[0]["id"], "S1")
        self.assertEqual(library.search("准备睡觉")[0]["id"], "S2")

    def test_recent_ranking_is_scoped_to_call_and_preserves_only_match(self):
        self.add("S1")
        self.add("S2")
        library = self.library()
        first = library.search("高兴", recent_ids=["S1"])
        second = library.search("高兴", recent_ids=["S2"])
        self.assertEqual(first[0]["id"], "S2")
        self.assertEqual(second[0]["id"], "S1")
        self.assertEqual(library.search("高兴")[0]["id"], "S1")

    def test_config_and_bad_catalog_fail_explicitly(self):
        for kwargs in ({"enabled": 1}, {"search_limit": True}, {"catalog": ""},
                       {"target_turns_min": 6, "target_turns_max": 5}, {"recent_window": 0},
                       {"max_age_seconds": True}, {"max_age_seconds": 0}, {"max_age_seconds": 61},
                       {"max_age_seconds": float("nan")}, {"max_age_seconds": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                StickerConfig(**kwargs).validate()
        self.assertEqual(len(StickerLibrary(self.root, StickerConfig())), 0)
        with self.assertRaises(ValueError):
            StickerLibrary(self.root, self.config)
        self.add("S1")
        self.add("S1")
        with self.assertRaises(ValueError):
            self.library()

    def test_prepare_checks_path_and_hash_even_after_cached_send(self):
        self.add("S1")
        library = self.library()
        part, metadata = library.prepare("S1")
        self.assertEqual(part["type"], "image")
        self.assertTrue(part["data"]["file"].startswith("base64://"))
        self.assertNotIn("base64", json.dumps(metadata))
        (self.directory / "S1.asset").write_bytes(picture(colors=("blue",)))
        with self.assertRaises(ToolError) as caught:
            library.prepare("S1")
        self.assertEqual(caught.exception.code, "sticker_changed")
        self.rows[0]["file"] = "../outside.png"
        (self.root / "outside.png").write_bytes(picture())
        with self.assertRaises(ToolError) as caught:
            self.library().prepare("S1")
        self.assertEqual(caught.exception.code, "sticker_invalid_path")

    def test_symlink_escape_and_non_images_are_rejected(self):
        raw = picture()
        (self.root / "outside.png").write_bytes(raw)
        self.add("S1", file="escape.png")
        (self.directory / "escape.png").symlink_to(self.root / "outside.png")
        with self.assertRaises(ToolError) as caught:
            self.library().prepare("S1")
        self.assertEqual(caught.exception.code, "sticker_invalid_path")
        self.rows.clear()
        self.add("S2", raw=b"not an image")
        with self.assertRaises(ToolError) as caught:
            self.library().prepare("S2")
        self.assertEqual(caught.exception.code, "sticker_invalid_image")

    def test_webp_animation_preserves_frames_timing_and_source(self):
        raw = picture("WEBP", ("red", "green", "blue"), [90, 130, 210])
        self.add("S1", raw=raw)
        library = self.library()
        part, metadata = library.prepare("S1")
        converted = base64.b64decode(part["data"]["file"].removeprefix("base64://"))
        with Image.open(BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertEqual(gif.n_frames, 3)
            durations = []
            colors = []
            for index in range(gif.n_frames):
                gif.seek(index)
                durations.append(gif.info["duration"])
                colors.append(gif.convert("RGB").getpixel((10, 10)))
        self.assertEqual(durations, [90, 130, 210])
        self.assertEqual(colors, [(255, 0, 0), (0, 128, 0), (0, 0, 255)])
        self.assertTrue(metadata["animated"])
        self.assertEqual(metadata["frames"], 3)
        self.assertEqual((self.directory / "S1.asset").read_bytes(), raw)

    def test_static_webp_is_png_and_gif_bytes_are_preserved(self):
        self.add("S1", raw=picture("WEBP"))
        original_gif = picture("GIF", ("red", "blue"), [100, 200])
        self.add("S2", raw=original_gif)
        library = self.library()
        part, metadata = library.prepare("S1")
        with Image.open(BytesIO(base64.b64decode(part["data"]["file"][9:]))) as image:
            self.assertEqual(image.format, "PNG")
        part, metadata = library.prepare("S2")
        self.assertEqual(base64.b64decode(part["data"]["file"][9:]), original_gif)
        self.assertTrue(metadata["animated"])

    def test_transparent_animation_keeps_a_transparent_background(self):
        frames = []
        for color in ("red", "blue"):
            frame = Image.new("RGBA", (24, 24), (255, 255, 255, 0))
            frame.paste(Image.new("RGBA", (8, 8), color), (8, 8))
            frames.append(frame)
        output = BytesIO()
        frames[0].save(output, format="WEBP", lossless=True, save_all=True,
                       append_images=frames[1:], duration=[100, 100], loop=0)
        self.add("S1", raw=output.getvalue())
        part, _ = self.library().prepare("S1")
        with Image.open(BytesIO(base64.b64decode(part["data"]["file"][9:]))) as gif:
            for index in range(2):
                gif.seek(index)
                rgba = gif.convert("RGBA")
                self.assertEqual(rgba.getpixel((0, 0))[3], 0)
                self.assertEqual(rgba.getpixel((10, 10))[3], 255)

    def test_opaque_black_pixels_do_not_disappear_between_animation_frames(self):
        frames = []
        for color in ("red", "blue"):
            frame = Image.new("RGBA", (24, 24), "black")
            frame.paste(Image.new("RGBA", (8, 8), color), (8, 8))
            frames.append(frame)
        output = BytesIO()
        frames[0].save(output, format="WEBP", lossless=True, save_all=True,
                       append_images=frames[1:], duration=[100, 100], loop=0)
        self.add("S1", raw=output.getvalue())
        part, _ = self.library().prepare("S1")
        with Image.open(BytesIO(base64.b64decode(part["data"]["file"][9:]))) as gif:
            for index in range(2):
                gif.seek(index)
                self.assertEqual(gif.convert("RGBA").getpixel((0, 0)), (0, 0, 0, 255))

    def test_conversion_cache_evicts_old_entries(self):
        for index in range(17):
            self.add(f"S{index}", raw=picture(colors=((index, 0, 0),)))
        library = self.library()
        with patch.object(library, "_encode", wraps=library._encode) as convert:
            for index in range(17):
                library.prepare(f"S{index}")
            self.assertEqual(convert.call_count, 17)
            library.prepare("S16")
            self.assertEqual(convert.call_count, 17)
            library.prepare("S0")
            self.assertEqual(convert.call_count, 18)

    def test_original_and_encoded_size_limits(self):
        self.config.max_image_bytes = 1024
        self.add("S1", raw=b"x" * 1025)
        with self.assertRaises(ToolError) as caught:
            self.library().prepare("S1")
        self.assertEqual(caught.exception.code, "sticker_too_large")
        self.rows.clear()
        gradient = Image.linear_gradient("L").resize((128, 128)).convert("RGB")
        output = BytesIO()
        gradient.save(output, format="WEBP", quality=20, save_all=True,
                      append_images=[gradient.rotate(90)], duration=[100, 100], loop=0)
        self.assertLess(len(output.getvalue()), self.config.max_image_bytes)
        self.add("S2", raw=output.getvalue())
        with self.assertRaises(ToolError) as caught:
            self.library().prepare("S2")
        self.assertEqual(caught.exception.code, "sticker_too_large")


class StickerToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_is_read_only_scoped_and_does_not_accept_file_paths(self):
        class Library:
            def __init__(self):
                self.recent = []
            def search(self, query, limit=6, recent_ids=()):
                self.recent.append(list(recent_ids))
                return [{"id": "S1", "title": "欢呼"}]
        library = Library()
        registry = ToolRegistry()
        register_stickers(registry, library, StickerConfig(enabled=True))
        for recent in (["S1"], ["S2"]):
            context = SimpleNamespace(check_active=lambda: None, sticker_state={"recent_sticker_ids": recent})
            result = await registry.execute("search_stickers", '{"query":"庆祝"}', context, ToolsConfig())
            self.assertTrue(result.ok)
            self.assertTrue(result.meta["read_only"])
        self.assertEqual(library.recent, [["S1"], ["S2"]])
        result = await registry.execute("search_stickers", '{"query":"庆祝","file":"/etc/passwd"}', context, ToolsConfig())
        self.assertEqual(result.error["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
