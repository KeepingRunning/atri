"""Build a standalone, local-only listening and selection page for ATRI voices."""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote
import webbrowser


def _audio_url(value: str) -> str:
    """Only link to a file inside the review directory, never a remote URL."""
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts
            or "\\" in value or ":" in value or value.startswith("//")):
        raise ValueError(f"Audio must use a relative path within the review directory: {value!r}")
    if path.suffix.lower() not in {".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus"}:
        raise ValueError(f"Unsupported audio file: {value!r}")
    return "/".join(quote(part, safe="") for part in path.parts)


# These pure functions are shared by page interaction and export. They deliberately
# ignore unknown IDs so selections left over from an older catalog cannot delete
# files absent from the current review.
REVIEW_STATE_JS = r"""
function cleanDecisions(raw, ids) {
  const result = Object.create(null);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return result;
  for (const id of ids) {
    if (Object.prototype.hasOwnProperty.call(raw, id) && ['keep', 'exclude'].includes(raw[id])) {
      result[id] = raw[id];
    }
  }
  return result;
}
function selectionExport(ids, decisions) {
  const clean = cleanDecisions(decisions, ids);
  const value = {schema_version: 1, delete_ids: [], keep_ids: [], unreviewed_ids: []};
  for (const id of ids) {
    if (clean[id] === 'exclude') value.delete_ids.push(id);
    else if (clean[id] === 'keep') value.keep_ids.push(id);
    else value.unreviewed_ids.push(id);
  }
  return value;
}
function filterForListening(items, maxSeconds, order, textKind = 'all') {
  const knownDuration = item => Number.isFinite(item.duration_seconds) && item.duration_seconds >= 0;
  const matches = items.filter(item => (!maxSeconds || (knownDuration(item) && item.duration_seconds <= maxSeconds))
    && (textKind === 'all' || (item.annotation_method === 'subtitle_notice') === (textKind === 'punctuation')));
  if (order === 'shortest') matches.sort((left, right) =>
    (knownDuration(left) ? left.duration_seconds : Infinity) - (knownDuration(right) ? right.duration_seconds : Infinity));
  return matches;
}
function selectionNotice(item) {
  return item.annotation_method === 'subtitle_notice'
    ? '字幕没有明确台词，具体发声和语气请试听确认。' : '';
}
"""


VOICE_PLAYER_JS = r"""
function createVoicePlayer(audio, onChange = () => {}) {
  let generation = 0, current = null;
  const cleanup = [];
  function notify(phase, attempt = current) {
    onChange(attempt ? {phase, item: attempt.item, format: attempt.format, url: attempt.url,
      failures: attempt.failures.slice()} : {phase: 'idle', item: null, failures: []});
  }
  function release() {
    generation += 1;
    current = null;
    while (cleanup.length) cleanup.pop()();
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
  }
  function isCurrent(attempt) {
    return current === attempt && generation === attempt.generation && audio.src === attempt.expectedSrc;
  }
  function sourceMatches(attempt) {
    return !audio.currentSrc || audio.currentSrc === attempt.expectedSrc;
  }
  function listen(type, callback) {
    audio.addEventListener(type, callback);
    cleanup.push(() => audio.removeEventListener(type, callback));
  }
  function start(item, fallback, failures = []) {
    release();
    const useFallback = Boolean(fallback && item.fallback_audio_url);
    const attempt = {item, generation, format: useFallback ? 'WAV' : 'MP3',
      url: useFallback ? item.fallback_audio_url : item.audio_url,
      fallbackTried: useFallback, failures: failures.slice(), failed: false};
    current = attempt;
    audio.src = attempt.url;
    attempt.expectedSrc = audio.src;
    function fail(exception = null) {
      if (!isCurrent(attempt) || attempt.failed) return;
      const mediaError = sourceMatches(attempt) ? audio.error : null;
      // An aborted play() belongs to an interrupted user operation, not a codec
      // failure. Do not unexpectedly restart it in another format.
      if (!mediaError && exception && exception.name === 'AbortError') {
        notify('paused', attempt);
        return;
      }
      attempt.failed = true;
      attempt.failures.push({id: item.id, format: attempt.format,
        code: mediaError ? mediaError.code : null,
        message: mediaError && mediaError.message ? mediaError.message : '',
        exception: exception ? [exception.name, exception.message].filter(Boolean).join(': ') : ''});
      const mediaFailure = mediaError && [2, 3, 4].includes(mediaError.code);
      const codecFailure = exception && ['NotSupportedError', 'EncodingError'].includes(exception.name);
      if (!attempt.fallbackTried && item.fallback_audio_url && (mediaFailure || codecFailure)) {
        start(item, true, attempt.failures);
      } else {
        notify('error', attempt);
      }
    }
    listen('error', () => {
      // load() clears the live MediaError. An old queued event without a current
      // error must not switch the new item to WAV.
      if (isCurrent(attempt) && sourceMatches(attempt) && audio.error) fail();
    });
    listen('playing', () => {
      if (isCurrent(attempt) && sourceMatches(attempt) && !audio.paused && !audio.error) {
        // Native controls can recover a user-activation rejection without a
        // second card click. Keep its earlier diagnostic, but show real playback.
        attempt.failed = false;
        notify('playing', attempt);
      }
    });
    listen('pause', () => {
      if (isCurrent(attempt) && sourceMatches(attempt) && audio.paused && !attempt.failed) notify('paused', attempt);
    });
    listen('ended', () => {
      if (isCurrent(attempt) && sourceMatches(attempt) && audio.ended) notify('ended', attempt);
    });
    audio.load();
    notify('loading', attempt);
    // Keep this call synchronous with the button's trusted click event. Awaiting
    // metadata/fetch first can lose user activation inside VSCode webviews.
    try {
      const playing = audio.play();
      if (playing && typeof playing.then === 'function') {
        playing.then(() => {
          if (isCurrent(attempt) && !attempt.failed && !audio.paused) notify('playing', attempt);
        }, fail);
      }
    } catch (error) { fail(error); }
  }
  return {
    play(item, fallback = false) { start(item, fallback); },
    stop() { release(); notify('idle'); },
    currentItem() { return current ? current.item : null; }
  };
}
function playbackFailureText(failure) {
  const code = failure.code === null ? '无 MediaError' : 'MediaError.code=' + failure.code;
  return `${failure.id} · ${failure.format} · ${code} · message=${failure.message || '浏览器未提供'}${failure.exception ? ' · ' + failure.exception : ''}`;
}
"""


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<style>
:root{color-scheme:light;--ink:#263745;--muted:#647887;--line:#dbe7eb;--blue:#147e9e;--pale:#eff8fa;--red:#b6454e}
*{box-sizing:border-box}body{margin:0;background:#f6f9fb;color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1190px;margin:auto;padding:30px 22px 235px}h1{font-size:27px;line-height:1.3;margin:0 0 9px}h2{font-size:17px;margin:0}
.intro{max-width:900px;color:var(--muted);margin:0 0 12px}.notice{border-left:3px solid #8dc4d1;padding:8px 12px;background:var(--pale);font-size:13px;margin:12px 0}
.notice.warning{border-color:#d5a255;background:#fff8e8}.toolbar{position:sticky;top:0;z-index:2;background:#f6f9fbf5;border-bottom:1px solid var(--line);padding:15px 0 12px;backdrop-filter:blur(8px)}
.filters,.exports,.pager,.card-head,.decision-row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.filters{align-items:flex-end}.filters label{display:flex;flex-direction:column;font-size:12px;gap:3px;color:var(--muted)}
.search{flex:1;min-width:240px}input,select,button{font:inherit;border:1px solid #c9dade;border-radius:8px;background:white;color:var(--ink);padding:8px 11px}input,select{height:41px}button{cursor:pointer;line-height:1.4}button:hover{border-color:var(--blue);color:var(--blue)}button:disabled{cursor:default;opacity:.4}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #80c5d9;outline-offset:2px}
.exports{margin-top:12px}.exports button{font-size:13px}.summary{font-size:13px;color:var(--muted);flex:1}.pager{justify-content:center;margin:16px 0}.pager span{font-size:13px;min-width:190px;text-align:center;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{background:white;border:1px solid var(--line);border-radius:13px;padding:18px;min-width:0;box-shadow:0 3px 10px #183e5210}.card[data-decision="keep"]{border-color:#73b1a5}.card[data-decision="exclude"]{border-color:#d6a4a9;background:#fffafa}.card-head{justify-content:space-between;align-items:flex-start}.identity{font:12px/1.5 ui-monospace,SFMono-Regular,monospace;color:var(--blue);word-break:break-all}.badge{font-size:11px;white-space:nowrap;border-radius:20px;padding:2px 8px;background:var(--pale);color:var(--blue)}.badge.review{background:#fff1db;color:#95621a}.badge.contextual{background:#f1edf7;color:#7a6294}
.title{margin:9px 0 10px}.spoken{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}.japanese{color:#2d4857;font-size:16px}.translation{color:#687987}.description{font-size:14px;margin:13px 0;color:#425866;white-space:pre-wrap;overflow-wrap:anywhere}.tags{display:flex;flex-wrap:wrap;gap:5px;margin:9px 0}.tag{font-size:12px;background:#f3f7f8;padding:2px 8px;border-radius:5px}.notes{font-size:13px;color:var(--muted);margin:7px 0;overflow-wrap:anywhere}.notes b{font-weight:500;color:#385463}.review-note{color:#916126;background:#fff8eb;padding:8px;border-radius:6px;font-size:13px}.audio-row{display:flex;gap:10px;align-items:center;margin:13px 0}audio{width:100%;height:36px}.duration{font-size:12px;white-space:nowrap;color:var(--muted)}.audio-error{font-size:12px;color:var(--red)}
.decision-row{margin-top:13px;border-top:1px solid var(--line);padding-top:12px}.decision-row button{font-size:13px;padding:6px 10px}.decision-row .selected.keep{background:#e5f4ef;border-color:#6daa9b;color:#1c725e}.decision-row .selected.exclude{background:#fbe9ec;border-color:#bd727c;color:#a13747}.decision-label{margin-left:auto;font-size:12px;color:var(--muted)}details{font-size:12px;color:var(--muted);margin-top:12px}summary{cursor:pointer}.source{white-space:pre-wrap;overflow-wrap:anywhere}.empty{grid-column:1/-1;text-align:center;padding:55px 15px;color:var(--muted)}footer{font-size:12px;color:var(--muted);text-align:center;margin-top:25px}[hidden]{display:none!important}
@media(max-width:730px){main{padding:20px 13px 40px}.cards{grid-template-columns:1fr}.search{min-width:100%}.toolbar{position:static}.filters label:not(.search){flex:1}.summary{flex-basis:100%}.card{padding:15px}}
.selection-first #recommendation-filter{display:none}
.audio-row{flex-wrap:wrap}.audio-row button{font-size:13px;padding:6px 10px}.audio-links{display:flex;gap:9px;font-size:12px}.audio-links a,.playback a{color:var(--blue)}
.playback{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);width:calc(100% - 26px);max-width:1146px;z-index:3;padding:11px 15px;border:1px solid #b8d5df;border-radius:12px;background:#fffffff5;box-shadow:0 3px 25px #183e5233;backdrop-filter:blur(8px)}
.playback-top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:7px}.playback-status{flex:1;font-size:13px;min-width:200px;overflow-wrap:anywhere}.playback button{font-size:12px;padding:5px 9px}.playback a{font-size:12px}.playback .audio-error{margin:6px 0 0;max-height:65px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.playback-hint{font-size:11px;color:var(--muted);margin:4px 0 0}
@media(max-width:730px){main{padding-bottom:260px}.playback{padding:10px}.playback-top{gap:6px}.playback-status{flex-basis:100%}}
</style>
</head>
<body class="__STAGE_CLASS__">
<main>
<h1>__PAGE_TITLE__</h1>
<p class="intro">__INTRO__</p>
<p class="notice">__NOTICE__</p>
<p class="notice warning" id="storage-warning" hidden>浏览器无法保存本地选择；关闭页面前请导出审阅 JSON，以免丢失。</p>
<div class="toolbar">
  <div class="filters">
    <label class="search">__SEARCH_LABEL__<input id="search" type="search" placeholder="例如：开心 / ATR_b101 / ありがとう" autocomplete="off"></label>
    <label id="recommendation-filter">建议用途<select id="recommendation"><option value="">全部语音</option><option value="everyday">推荐日常</option><option value="contextual">依赖语境</option><option value="review">需试听核对</option></select></label>
    <label>我的选择<select id="decision"><option value="">全部状态</option><option value="unreviewed">只看未审</option><option value="keep">只看保留</option><option value="exclude">只看排除</option></select></label>
    <label>台词<select id="text-kind"><option value="all">全部台词</option><option value="text">有文字</option><option value="punctuation">仅标点</option></select></label>
    <label>时长<select id="max-duration"><option value="0">全部时长</option><option value="3">3 秒以内</option><option value="5">5 秒以内</option><option value="10">10 秒以内</option></select></label>
    <label>排序<select id="sort-order"><option value="original">原作顺序</option><option value="shortest">短语音优先</option></select></label>
    <label>每页<select id="page-size"><option value="15">15 条</option><option value="30" selected>30 条</option><option value="60">60 条</option></select></label>
  </div>
  <div class="exports">
    <span class="summary" id="summary" aria-live="polite"></span>
    <button id="export-json" type="button">导出审阅 JSON</button>
    <button id="export-keep" type="button">导出保留编号.txt</button>
    <button id="export-delete" type="button">导出待删编号.txt</button>
    <button id="import-json" type="button">导入审阅 JSON</button>
    <input id="import-file" type="file" accept="application/json,.json" hidden>
  </div>
</div>
<nav class="pager" aria-label="分页"><button data-page="previous" type="button">上一页</button><span data-page-label></span><button data-page="next" type="button">下一页</button></nav>
<section class="cards" id="cards" aria-label="语音列表"></section>
<nav class="pager" aria-label="底部分页"><button data-page="previous" type="button">上一页</button><span data-page-label></span><button data-page="next" type="button">下一页</button></nav>
<footer>选择保存在当前浏览器中。移动目录或更换浏览器前请导出 JSON；可重新导入恢复。每次只播放一条，不会自动播放或预加载音频。</footer>
</main>
<section class="playback" aria-label="共享试听播放器">
  <div class="playback-top">
    <span class="playback-status" id="playback-status" aria-live="polite">点击任意语音的“试听”开始播放</span>
    <button id="playback-stop" type="button" disabled>停止</button>
    <button id="playback-fallback" type="button" disabled>兼容播放（WAV）</button>
    <a id="playback-open" target="_blank" rel="noopener" hidden>独立打开当前音频</a>
  </div>
  <audio id="voice-player" controls preload="none" aria-label="当前语音播放器"></audio>
  <p id="playback-error" class="audio-error" role="status" hidden></p>
  <p class="playback-hint">全页共用一个播放器，切换页面、筛选会释放音频。若 MP3 和 WAV 都报 Format error，请通过本地 HTTP 审阅地址在 Chrome / Safari 打开。</p>
</section>
<script id="voice-catalog" type="application/json">__CATALOG_JSON__</script>
<script>
'use strict';
__REVIEW_STATE_JS__
__VOICE_PLAYER_JS__
const catalog = JSON.parse(document.getElementById('voice-catalog').textContent);
const selectionFirst = catalog.review_stage === 'selection_first';
document.getElementById('text-kind').value = selectionFirst ? 'text' : 'all';
const items = catalog.items;
const ids = items.map(item => item.id);
const storageKey = 'atri.voice-review.v1';
const recommendationLabels = {everyday: '推荐日常', contextual: '依赖语境', review: '需试听核对'};
const decisionLabels = {keep: '已保留', exclude: '已排除', unreviewed: '未审阅'};
const annotationLabels = {complete: '已标注', pending: '待标注', failed: '标注失败'};
let decisions = Object.create(null), page = 1;
try { decisions = cleanDecisions(JSON.parse(localStorage.getItem(storageKey) || '{}'), ids); }
catch (_) { document.getElementById('storage-warning').hidden = false; }
const searchIndex = new Map(items.map(item => [item.id, [item.id, item.text_ja, item.text_zh,
  ...(selectionFirst ? [] : [item.title, item.description, ...(item.emotions || []), ...(item.usage || []), ...(item.avoid || [])])].join(' ').toLocaleLowerCase()]));
const voicePlayer = createVoicePlayer(document.getElementById('voice-player'), renderPlayback);

function renderPlayback(state) {
  const phase = {loading: '正在加载', playing: '正在播放', paused: '已暂停', ended: '播放结束', error: '播放失败'};
  document.getElementById('playback-status').textContent = state.item
    ? `${state.item.id} · ${state.format} · ${phase[state.phase] || state.phase}` : '点击任意语音的“试听”开始播放';
  document.getElementById('playback-stop').disabled = !state.item;
  document.getElementById('playback-fallback').disabled = !state.item || !state.item.fallback_audio_url;
  const link = document.getElementById('playback-open');
  link.hidden = !state.item;
  if (state.item) link.href = state.url; else link.removeAttribute('href');
  const error = document.getElementById('playback-error');
  error.hidden = !state.failures.length;
  error.textContent = state.failures.map(playbackFailureText).join('\n');
}
document.getElementById('playback-stop').addEventListener('click', () => voicePlayer.stop());
document.getElementById('playback-fallback').addEventListener('click', () => {
  const item = voicePlayer.currentItem();
  if (item && item.fallback_audio_url) voicePlayer.play(item, true);
});
window.addEventListener('pagehide', () => voicePlayer.stop());

function save() {
  try { localStorage.setItem(storageKey, JSON.stringify(decisions)); }
  catch (_) { document.getElementById('storage-warning').hidden = false; }
}
function el(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
}
function decisionOf(id) { return decisions[id] || 'unreviewed'; }
function choose(id, value) {
  if (value === 'unreviewed') delete decisions[id]; else decisions[id] = value;
  save();
  if (document.getElementById('decision').value) render();
  else { updateCard(document.getElementById('card-' + id), id); updateSummary(); }
}
function updateCard(card, id) {
  const chosen = decisionOf(id);
  card.dataset.decision = chosen;
  for (const button of card.querySelectorAll('[data-decision]')) {
    button.classList.toggle('selected', button.dataset.decision === chosen);
    button.setAttribute('aria-pressed', String(button.dataset.decision === chosen));
  }
  card.querySelector('.decision-label').textContent = decisionLabels[chosen];
}
function updateSummary() {
  const value = selectionExport(ids, decisions);
  document.getElementById('summary').textContent = `共 ${items.length} 条 · 保留 ${value.keep_ids.length} · 排除 ${value.delete_ids.length} · 未审 ${value.unreviewed_ids.length}`;
}
function appendNotes(card, label, values) {
  if (!values || !values.length) return;
  const row = el('p', 'notes'); row.append(el('b', '', label + '：'), document.createTextNode(values.join('；'))); card.append(row);
}
function buildCard(item) {
  const card = el('article', 'card'); card.id = 'card-' + item.id;
  const head = el('div', 'card-head');
  head.append(el('div', 'identity', item.id));
  if (!selectionFirst) head.append(el('span', 'badge ' + item.recommendation, recommendationLabels[item.recommendation] || '需试听核对'));
  card.append(head);
  if (!selectionFirst) card.append(el('h2', 'title', item.title || '待标注语音'));
  card.append(el('p', 'spoken japanese', item.text_ja || '暂无日文台词'), el('p', 'spoken translation', item.text_zh || '暂无中文文本'));
  const audioRow = el('div', 'audio-row'), play = el('button', '', '试听');
  play.type = 'button'; play.setAttribute('aria-label', '试听 ' + item.id);
  play.addEventListener('click', () => voicePlayer.play(item));
  audioRow.append(play);
  if (item.fallback_audio_url) {
    const fallback = el('button', '', '兼容播放（WAV）'); fallback.type = 'button';
    fallback.setAttribute('aria-label', '兼容播放 ' + item.id);
    fallback.addEventListener('click', () => voicePlayer.play(item, true)); audioRow.append(fallback);
  }
  const duration = Number.isFinite(item.duration_seconds) ? item.duration_seconds.toFixed(1) + ' 秒' : '';
  audioRow.append(el('span', 'duration', duration)); card.append(audioRow);
  const audioLinks = el('div', 'audio-links');
  for (const [label, url] of [['独立打开 MP3', item.audio_url], ['独立打开 WAV', item.fallback_audio_url]]) {
    if (!url) continue;
    const link = el('a', '', label); link.href = url; link.target = '_blank'; link.rel = 'noopener'; audioLinks.append(link);
  }
  card.append(audioLinks);
  if (selectionFirst) {
    const notice = selectionNotice(item);
    if (notice) card.append(el('p', 'review-note', notice));
  } else {
    if (item.description) card.append(el('p', 'description', item.description));
    const tags = el('div', 'tags');
    for (const word of item.emotions || []) tags.append(el('span', 'tag', word));
    if (item.intensity) tags.append(el('span', 'tag', '表达强度 ' + item.intensity + '/3'));
    if (tags.childElementCount) card.append(tags);
    appendNotes(card, '适合', item.usage); appendNotes(card, '慎用', item.avoid);
    if (item.needs_review || item.annotation_status !== 'complete') {
      card.append(el('p', 'review-note', item.review_reason || annotationLabels[item.annotation_status] || '需要试听核对'));
    }
  }
  const source = item.source || {}, details = el('details'), sourceText = [];
  if (selectionFirst) sourceText.push('台词来源：原游戏日文台词与对应中文译文');
  else {
    sourceText.push('标注状态：' + (annotationLabels[item.annotation_status] || item.annotation_status || '未知'));
    sourceText.push('标注依据：' + (item.annotation_basis === 'original_script' ? '原游戏对应台词（描述未做声学判断）' : item.annotation_basis || '待核对'));
  }
  if (source.file) sourceText.push('脚本：' + source.file);
  if (source.dialogue_id) sourceText.push('对话编号：' + source.dialogue_id);
  if (source.text_index !== undefined) sourceText.push('文本位置：' + source.text_index);
  details.append(el('summary', '', selectionFirst ? '文本出处' : '文本出处与标注依据'), el('p', 'source', sourceText.join('\n'))); card.append(details);
  const row = el('div', 'decision-row');
  for (const [value, label] of [['keep', '保留'], ['exclude', '排除'], ['unreviewed', '撤销选择']]) {
    const button = el('button', value, label); button.type = 'button'; button.dataset.decision = value;
    button.addEventListener('click', () => choose(item.id, value)); row.append(button);
  }
  row.append(el('span', 'decision-label')); card.append(row); updateCard(card, item.id);
  return card;
}
function render() {
  voicePlayer.stop();
  const query = document.getElementById('search').value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const recommendation = document.getElementById('recommendation').value, chosen = document.getElementById('decision').value;
  const size = Number(document.getElementById('page-size').value);
  const filtered = items.filter(item => (selectionFirst || !recommendation || item.recommendation === recommendation)
    && (!chosen || decisionOf(item.id) === chosen) && query.every(word => searchIndex.get(item.id).includes(word)));
  const matches = filterForListening(filtered, Number(document.getElementById('max-duration').value),
    document.getElementById('sort-order').value, document.getElementById('text-kind').value);
  const pages = Math.max(1, Math.ceil(matches.length / size)); page = Math.max(1, Math.min(page, pages));
  const cards = document.getElementById('cards'); cards.replaceChildren();
  for (const item of matches.slice((page - 1) * size, page * size)) cards.append(buildCard(item));
  if (!matches.length) cards.append(el('p', 'empty', '没有符合条件的语音，试试更换搜索词或筛选条件。'));
  for (const label of document.querySelectorAll('[data-page-label]')) label.textContent = `第 ${page} / ${pages} 页 · 筛选出 ${matches.length} 条`;
  for (const button of document.querySelectorAll('[data-page]')) button.disabled = button.dataset.page === 'previous' ? page <= 1 : page >= pages;
  updateSummary();
}
for (const id of ['recommendation', 'decision', 'page-size', 'max-duration', 'sort-order', 'text-kind']) document.getElementById(id).addEventListener('change', () => { page = 1; render(); });
let searchTimer;
document.getElementById('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; render(); }, 150); });
for (const button of document.querySelectorAll('[data-page]')) button.addEventListener('click', () => {
  page += button.dataset.page === 'previous' ? -1 : 1; render(); document.querySelector('.toolbar').scrollIntoView({block: 'start'});
});
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type})), link = el('a');
  link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
document.getElementById('export-json').addEventListener('click', () => {
  const value = selectionExport(ids, decisions); value.exported_at = new Date().toISOString();
  download('atri-voice-review.json', JSON.stringify(value, null, 2) + '\n', 'application/json;charset=utf-8');
});
document.getElementById('export-delete').addEventListener('click', () => {
  const excluded = selectionExport(ids, decisions).delete_ids;
  download('atri-voice-delete-ids.txt', excluded.length ? excluded.join('\n') + '\n' : '', 'text/plain;charset=utf-8');
});
document.getElementById('export-keep').addEventListener('click', () => {
  const kept = selectionExport(ids, decisions).keep_ids;
  download('atri-voice-keep-ids.txt', kept.length ? kept.join('\n') + '\n' : '', 'text/plain;charset=utf-8');
});
document.getElementById('import-json').addEventListener('click', () => document.getElementById('import-file').click());
document.getElementById('import-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  try {
    const value = JSON.parse(await file.text());
    if (value.schema_version !== 1 || !Array.isArray(value.keep_ids) || !Array.isArray(value.delete_ids)) throw new Error('不是有效的语音审阅 JSON');
    const known = new Set(ids), incoming = Object.create(null);
    for (const [key, decision] of [['keep_ids', 'keep'], ['delete_ids', 'exclude']]) {
      for (const id of value[key]) {
        if (typeof id !== 'string') throw new Error('语音编号必须是字符串');
        if (incoming[id] && incoming[id] !== decision) throw new Error('同一语音同时出现在保留和排除列表');
        if (known.has(id)) incoming[id] = decision;
      }
    }
    const count = Object.keys(incoming).length;
    if (confirm(`导入 ${count} 条选择？这些编号的现有选择将被更新，其余保持不变。`)) {
      Object.assign(decisions, incoming); save(); render();
    }
  } catch (error) { alert('导入失败：' + error.message); }
  event.target.value = '';
});
render();
</script>
</body>
</html>
"""


def export_review(directory: Path, catalog: dict) -> Path:
    """Write index.html next to the audio folder without changing any choices."""
    items = []
    identities: set[str] = set()
    for entry in catalog["items"]:
        identity = entry["id"]
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError(f"Voice IDs must be unique, nonempty strings: {identity!r}")
        identities.add(identity)
        item = dict(entry)
        item["audio_url"] = _audio_url(item["file"])
        item.pop("fallback_audio_url", None)
        if item.get("fallback_file"):
            item["fallback_audio_url"] = _audio_url(item["fallback_file"])
        items.append(item)
    stage = catalog.get("review_stage", "annotated")
    payload = json.dumps({"schema_version": 1, "review_stage": stage, "items": items}, ensure_ascii=False, allow_nan=False)
    # A JSON script element is still parsed as raw HTML text: neutralize closing
    # script tags, HTML comments, and JavaScript line separators before embedding.
    for char, escaped in [("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                          ("\u2028", "\\u2028"), ("\u2029", "\\u2029")]:
        payload = payload.replace(char, escaped)
    if stage == "selection_first":
        copy = {"PAGE_TITLE": "ATRI · 原声挑选", "STAGE_CLASS": "selection-first",
                "INTRO": "先试听原声、对照日中台词，选出你喜欢的片段。选好之后，再为保留的语音生成表达用途和描述。可以先筛短语音，也可以按原作顺序慢慢听。",
                "NOTICE": "台词直接使用原游戏日文和对应中文译文，出处可在每条下方展开。默认只看有文字的台词，可切换“全部台词”查看仅标点片段。保留或排除只记录选择，不会直接改动文件。",
                "SEARCH_LABEL": "搜索编号或日中台词"}
    else:
        copy = {"PAGE_TITLE": "ATRI · 语音审阅", "STAGE_CLASS": "annotated",
                "INTRO": "先听，再决定留下哪一句。语音用于文字回复旁的辅助表达；本页只整理素材，选择“排除”不会直接删除文件，也不会发送 QQ 消息。",
                "NOTICE": "日文台词与中文翻译优先对照原游戏对应文本；具体依据见每条的“文本出处”。description、情绪与用途根据台词推断，尚未做声学判断，实际语气请以试听为准。",
                "SEARCH_LABEL": "搜索编号、日中台词、描述或用途"}
    page = PAGE
    for key, value in copy.items():
        page = page.replace(f"__{key}__", value)
    page = (page.replace("__REVIEW_STATE_JS__", REVIEW_STATE_JS)
            .replace("__VOICE_PLAYER_JS__", VOICE_PLAYER_JS).replace("__CATALOG_JSON__", payload))
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "index.html"
    output.write_text(page, encoding="utf-8")
    return output


def create_review_app(directory: Path, catalog: dict):
    """Serve only the review page and its listed audio, with byte-range support."""
    from aiohttp import web

    directory = directory.resolve()
    files = {"/": "index.html", "/index.html": "index.html"}
    for item in catalog["items"]:
        for field in ("file", "fallback_file"):
            if value := item.get(field):
                files["/" + unquote(_audio_url(value))] = value

    async def get_file(request):
        relative = files.get(request.path)
        if relative is None:
            raise web.HTTPNotFound()
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise web.HTTPNotFound()
        content_type = {".html": "text/html; charset=utf-8", ".mp3": "audio/mpeg",
                        ".wav": "audio/wav"}.get(path.suffix.lower())
        content_type = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        return web.FileResponse(path, headers={"Content-Type": content_type,
                                               "Cache-Control": "no-store",
                                               "X-Content-Type-Options": "nosniff"})

    app = web.Application()
    app.router.add_get("/{path:.*}", get_file)
    return app


def serve_review(directory: Path, catalog: dict, *, port: int, open_browser: bool) -> None:
    from aiohttp import web

    url = f"http://127.0.0.1:{port}/"

    def started(_message):
        print(f"语音审阅：{url}\n请在 Chrome / Safari 等浏览器打开。按 Ctrl+C 停止服务。", flush=True)
        if open_browser:
            webbrowser.open(url)

    web.run_app(create_review_app(directory, catalog), host="127.0.0.1", port=port,
                access_log=None, print=started)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="语音 catalog.json 路径")
    parser.add_argument("--output-dir", type=Path, help="页面目录；默认使用 catalog 所在目录")
    parser.add_argument("--serve", action="store_true", help="仅在本机提供 HTTP 试听，避开内嵌预览的文件协议")
    parser.add_argument("--port", type=int, default=8766, help="本机试听端口（默认 8766）")
    parser.add_argument("--open-browser", action="store_true", help="服务启动后在默认浏览器打开（需 --serve）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    if args.open_browser and not args.serve:
        parser.error("--open-browser 需要同时使用 --serve")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    directory = args.output_dir or args.catalog.parent
    print(export_review(directory, catalog))
    if args.serve:
        serve_review(directory, catalog, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    main()
