import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.voice_review import REVIEW_STATE_JS, VOICE_PLAYER_JS, export_review


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.tags = []
        self.current_script = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.current_script = {"attrs": dict(attrs), "text": ""}
            self.scripts.append(self.current_script)

    def handle_endtag(self, tag):
        if tag == "script":
            self.current_script = None

    def handle_data(self, data):
        if self.current_script is not None:
            self.current_script["text"] += data


def voice(identity="ATR_b101_001", **overrides):
    return {"id": identity, "file": f"audio/{identity}.mp3", "text_ja": "こんにちは。",
            "text_zh": "你好。", "description": "用于打招呼。", "recommendation": "everyday",
            "annotation_status": "complete", **overrides}


class VoiceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def generate(self, *items):
        path = export_review(self.directory, {"schema_version": 1, "items": list(items)})
        page = PageParser()
        page.feed(path.read_text(encoding="utf-8"))
        return page

    def test_untrusted_dialogue_cannot_escape_json_script(self):
        text = '</script><img src=x onerror="alert(1)"><!--&\u2028\u2029'
        page = self.generate(voice(text_ja=text, description="__REVIEW_STATE_JS__"))
        self.assertEqual(2, len(page.scripts))
        self.assertFalse(any(tag == "img" for tag, _ in page.tags))
        payload = json.loads(page.scripts[0]["text"])
        self.assertEqual(text, payload["items"][0]["text_ja"])
        self.assertEqual("__REVIEW_STATE_JS__", payload["items"][0]["description"])
        self.assertNotIn("<", page.scripts[0]["text"])
        self.assertFalse(any(attrs.get("src") for tag, attrs in page.tags if tag == "script"))

    def test_audio_url_stays_local_and_encodes_filename(self):
        page = self.generate(voice(file="audio/语音 #1?.mp3"))
        item = json.loads(page.scripts[0]["text"])["items"][0]
        self.assertEqual("audio/%E8%AF%AD%E9%9F%B3%20%231%3F.mp3", item["audio_url"])
        for unsafe in ["../outside.mp3", "/tmp/outside.mp3", "https://example.com/audio.mp3",
                       "//example.com/audio.mp3", "audio/../../outside.mp3", r"..\outside.mp3",
                       "data:audio/mp3;base64,AAA", "audio/voice.html"]:
            with self.subTest(file=unsafe), self.assertRaises(ValueError):
                self.generate(voice(file=unsafe))

    def test_fallback_url_is_checked_and_never_accepts_embedded_unvalidated_url(self):
        page = self.generate(voice(fallback_file="audio/语音 #1.wav"))
        item = json.loads(page.scripts[0]["text"])["items"][0]
        self.assertEqual("audio/%E8%AF%AD%E9%9F%B3%20%231.wav", item["fallback_audio_url"])
        for unsafe in ["../outside.wav", "/tmp/outside.wav", "https://example.com/audio.wav",
                       "//example.com/audio.wav", r"audio\outside.wav"]:
            with self.subTest(fallback=unsafe), self.assertRaises(ValueError):
                self.generate(voice(fallback_file=unsafe))
        page = self.generate(voice(fallback_audio_url="https://example.com/unvalidated.wav"))
        self.assertNotIn("fallback_audio_url", json.loads(page.scripts[0]["text"])["items"][0])

    def test_duplicate_ids_rejected_without_overwriting_existing_page(self):
        path = self.directory / "index.html"
        path.write_text("existing review", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.generate(voice(), voice())
        self.assertEqual("existing review", path.read_text(encoding="utf-8"))

    def test_full_catalog_uses_one_shared_audio_without_a_source(self):
        page = self.generate(*(voice(f"ATR_{i:04d}") for i in range(2209)))
        self.assertEqual(2209, len(json.loads(page.scripts[0]["text"])["items"]))
        audios = [attrs for tag, attrs in page.tags if tag == "audio"]
        self.assertEqual(1, len(audios))
        self.assertEqual("voice-player", audios[0]["id"])
        self.assertEqual("none", audios[0]["preload"])
        self.assertIn("controls", audios[0])
        self.assertNotIn("src", audios[0])
        self.assertNotIn("autoplay", audios[0])
        self.assertFalse(any(tag in {"source", "iframe"} for tag, _ in page.tags))

    def test_selection_stage_preserves_all_items_and_identifies_selection_controls(self):
        items = [voice(), voice("ATR_b101_002", annotation_method="subtitle_notice")]
        path = export_review(self.directory, {"review_stage": "selection_first", "items": items})
        page = PageParser()
        page.feed(path.read_text(encoding="utf-8"))
        payload = json.loads(page.scripts[0]["text"])
        self.assertEqual("selection_first", payload["review_stage"])
        self.assertEqual(2, len(payload["items"]))
        self.assertIn(("body", {"class": "selection-first"}), page.tags)
        controls = {attrs.get("id") for tag, attrs in page.tags if tag in {"select", "button"}}
        self.assertTrue({"text-kind", "max-duration", "sort-order", "export-keep"} <= controls)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; used to verify browser JavaScript")
    def test_exported_decisions_never_delete_unreviewed_or_unknown_ids(self):
        program = REVIEW_STATE_JS + r"""
const assert = require('node:assert/strict');
const ids = ['a', 'b', 'c', '__proto__'];
const raw = JSON.parse('{"a":"exclude","b":"keep","c":"invalid","removed":"exclude","__proto__":"keep"}');
const clean = cleanDecisions(raw, ids);
assert.equal(Object.getPrototypeOf(clean), null);
assert.deepEqual(selectionExport(ids, clean), {
  schema_version: 1, delete_ids: ['a'], keep_ids: ['b', '__proto__'], unreviewed_ids: ['c']
});
assert.deepEqual(selectionExport(ids, Object.create({a: 'exclude'})), {
  schema_version: 1, delete_ids: [], keep_ids: [], unreviewed_ids: ids
});
for (const input of [null, [], 'invalid', 42]) {
  assert.deepEqual(selectionExport(ids, input).unreviewed_ids, ids);
}
delete clean.a;
assert.deepEqual(selectionExport(ids, clean).delete_ids, []);
assert.deepEqual(selectionExport(ids, clean).unreviewed_ids, ['a', 'c']);
"""
        result = subprocess.run([shutil.which("node"), "--eval", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)


    @unittest.skipUnless(shutil.which("node"), "Node is optional; used to verify browser JavaScript")
    def test_generated_browser_script_parses(self):
        page = self.generate(voice())
        result = subprocess.run([shutil.which("node"), "--check"], input=page.scripts[1]["text"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; used to verify browser JavaScript")
    def test_duration_and_subtitle_filters_preserve_original_order_and_unfiltered_catalog(self):
        program = REVIEW_STATE_JS + r"""
const assert = require('node:assert/strict');
const items = [
  {id: 'five', duration_seconds: 5},
  {id: 'unknown'},
  {id: 'short', duration_seconds: 1, annotation_method: 'subtitle_notice'},
  {id: 'three', duration_seconds: 3},
  {id: 'same-length', duration_seconds: 3},
  {id: 'ten', duration_seconds: 10},
  {id: 'long', duration_seconds: 10.01}
];
const ids = values => values.map(value => value.id);
const original = ids(items);
assert.deepEqual(ids(filterForListening(items, 3, 'original')), ['short', 'three', 'same-length']);
assert.deepEqual(ids(filterForListening(items, 5, 'original', 'text')), ['five', 'three', 'same-length']);
assert.deepEqual(ids(filterForListening(items, 10, 'shortest')), ['short', 'three', 'same-length', 'five', 'ten']);
assert.deepEqual(ids(filterForListening(items, 0, 'shortest')), ['short', 'three', 'same-length', 'five', 'ten', 'long', 'unknown']);
assert.deepEqual(ids(filterForListening(items, 0, 'original', 'punctuation')), ['short']);
assert.deepEqual(ids(filterForListening(items, 0, 'original')), original);
assert.deepEqual(ids(items), original);
assert.equal(selectionNotice({annotation_status: 'pending', needs_review: true, review_reason: '尚未标注'}), '');
assert.ok(selectionNotice({annotation_method: 'subtitle_notice'}).includes('试听'));
"""
        result = subprocess.run([shutil.which("node"), "--eval", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)


FAKE_AUDIO_JS = r"""
const assert = require('node:assert/strict');
class FakeAudio {
  constructor() {
    this._src = ''; this.currentSrc = ''; this.error = null; this.paused = true; this.ended = false;
    this.listeners = new Map(); this.requests = []; this.operations = [];
  }
  get src() { return this._src; }
  set src(value) { this._src = new URL(value, 'file:///review/index.html').href; this.operations.push('src:' + value); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.listeners.get(type).delete(fn); }
  emit(type) { for (const fn of [...(this.listeners.get(type) || [])]) fn(); }
  pause() { this.operations.push('pause'); this.paused = true; this.emit('pause'); }
  removeAttribute(name) { assert.equal(name, 'src'); this.operations.push('remove:src'); this._src = ''; }
  load() {
    this.operations.push('load'); this.error = null; this.currentSrc = this.src; this.paused = true; this.ended = false;
  }
  play() {
    this.operations.push('play'); this.paused = false;
    return new Promise((resolve, reject) => this.requests.push({src: this.src, resolve, reject}));
  }
}
const item = id => ({id, audio_url: 'audio/' + id + '.mp3', fallback_audio_url: 'audio/' + id + '.wav'});
const mediaException = (name, message) => Object.assign(new Error(message), {name});
const tick = () => Promise.resolve();
"""


@unittest.skipUnless(shutil.which("node"), "Node is optional; used to execute audio lifecycle tests")
class VoicePlayerTests(unittest.TestCase):
    def run_script(self, script):
        program = VOICE_PLAYER_JS + FAKE_AUDIO_JS + "\n(async () => {\n" + script + "\n})().catch(error => {console.error(error); process.exitCode = 1;});\n"
        result = subprocess.run([shutil.which("node"), "--eval", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_play_is_synchronous_and_stop_releases_source_and_ignores_pending_result(self):
        self.run_script(r"""
const audio = new FakeAudio(), states = [], player = createVoicePlayer(audio, value => states.push(value));
assert.equal(audio.requests.length, 0);
player.play(item('a'));
assert.equal(audio.requests.length, 1, 'play() must run inside the click, before any await');
assert.deepEqual(audio.operations, ['pause', 'remove:src', 'load', 'src:audio/a.mp3', 'load', 'play']);
assert.equal(player.currentItem().id, 'a');
player.stop();
assert.equal(audio.src, '');
assert.equal(audio.currentSrc, '');
assert.equal(audio.paused, true);
assert.equal(player.currentItem(), null);
assert.deepEqual(audio.operations.slice(-3), ['pause', 'remove:src', 'load']);
assert.ok([...audio.listeners.values()].every(listeners => listeners.size === 0));
const count = states.length;
audio.requests[0].reject(mediaException('NotSupportedError', 'late failure'));
await tick();
assert.equal(audio.requests.length, 1, 'stopping must prevent late fallback');
assert.equal(states.length, count);
assert.equal(states.at(-1).phase, 'idle');
""")

    def test_current_media_failure_switches_to_wav_once_and_preserves_diagnostics(self):
        self.run_script(r"""
const audio = new FakeAudio(), states = [], player = createVoicePlayer(audio, value => states.push(value));
player.play(item('a'));
audio.error = {code: 4, message: 'MP3 unsupported in preview'};
audio.emit('error');
assert.equal(audio.requests.length, 2);
assert.ok(audio.src.endsWith('/a.wav'));
assert.equal(states.at(-1).format, 'WAV');
assert.equal(states.at(-1).failures[0].code, 4);
audio.requests[0].reject(mediaException('NotSupportedError', 'same failure, delivered later'));
await tick();
assert.equal(audio.requests.length, 2);
audio.error = {code: 3, message: 'WAV decoder failed'};
audio.emit('error');
audio.emit('error');
audio.requests[1].reject(mediaException('NotSupportedError', 'late WAV rejection'));
await tick();
assert.equal(audio.requests.length, 2, 'WAV failure must not loop');
const state = states.at(-1);
assert.equal(state.phase, 'error');
assert.equal(state.failures.length, 2);
assert.equal(state.failures[1].format, 'WAV');
assert.equal(state.failures[1].code, 3);
assert.match(playbackFailureText(state.failures[0]), /a · MP3 · MediaError.code=4/);
assert.match(playbackFailureText(state.failures[1]), /message=WAV decoder failed/);
""")

    def test_rapid_selection_ignores_old_promises_old_listeners_and_empty_error_events(self):
        self.run_script(r"""
const audio = new FakeAudio(), states = [], player = createVoicePlayer(audio, value => states.push(value));
player.play(item('a'));
const oldErrorListener = [...audio.listeners.get('error')][0], oldSrc = audio.src;
player.play(item('b'));
const count = states.length;
audio.requests[0].reject(mediaException('NotSupportedError', 'a failed late'));
await tick();
assert.equal(states.length, count);
audio.error = {code: 4, message: 'old resource'};
audio.currentSrc = oldSrc;
oldErrorListener();
audio.emit('error');
audio.currentSrc = audio.src; audio.error = null;
audio.emit('error');
assert.equal(audio.requests.length, 2);
assert.equal(states.at(-1).item.id, 'b');
audio.requests[1].resolve();
await tick();
assert.equal(states.at(-1).phase, 'playing');
assert.equal(states.at(-1).item.id, 'b');
player.play(item('c'));
audio.requests[1].resolve();
await tick();
assert.equal(states.at(-1).item.id, 'c');
assert.equal(audio.requests.length, 3);
player.play(item('d'));
const afterSelection = states.length;
audio.requests[2].resolve();
await tick();
assert.equal(states.length, afterSelection, 'late successful play must not rewrite the newly selected item');
assert.equal(states.at(-1).item.id, 'd');
""")

    def test_manual_wav_uses_user_gesture_and_play_permission_failure_does_not_auto_restart(self):
        self.run_script(r"""
const audio = new FakeAudio(), states = [], player = createVoicePlayer(audio, value => states.push(value));
player.play(item('a'));
audio.requests[0].reject(mediaException('NotAllowedError', 'user activation required'));
await tick();
assert.equal(audio.requests.length, 1);
assert.equal(states.at(-1).phase, 'error');
assert.match(playbackFailureText(states.at(-1).failures[0]), /NotAllowedError: user activation required/);
audio.paused = false; audio.emit('playing');
assert.equal(states.at(-1).phase, 'playing', 'native controls can recover a user-activation error');
assert.equal(audio.requests.length, 1);
player.play(item('a'), true);
assert.equal(audio.requests.length, 2);
assert.ok(audio.requests[1].src.endsWith('/a.wav'));
assert.equal(states.at(-1).failures.length, 0);
audio.requests[1].resolve();
await tick();
assert.equal(states.at(-1).phase, 'playing');
audio.pause();
assert.equal(states.at(-1).phase, 'paused');
audio.paused = false; audio.emit('playing');
assert.equal(states.at(-1).phase, 'playing');
audio.ended = true; audio.emit('ended');
assert.equal(states.at(-1).phase, 'ended');
assert.equal(audio.requests.length, 2, 'ending must never start another item');
""")

    def test_unsupported_play_rejection_falls_back_but_abort_does_not(self):
        self.run_script(r"""
const audio = new FakeAudio(), states = [], player = createVoicePlayer(audio, value => states.push(value));
player.play(item('a'));
audio.requests[0].reject(mediaException('NotSupportedError', 'codec unavailable'));
await tick();
assert.equal(audio.requests.length, 2);
assert.ok(audio.src.endsWith('/a.wav'));
player.play(item('b'));
audio.requests[2].reject(mediaException('AbortError', 'user paused'));
await tick();
assert.equal(audio.requests.length, 3);
assert.equal(states.at(-1).phase, 'paused');
assert.equal(states.at(-1).item.id, 'b');
""")
