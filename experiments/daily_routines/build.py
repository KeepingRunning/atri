"""Read the full local corpus and build independently deletable two-hour routine JSON files."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import time

import aiohttp

from atri_bot.config import Config
from atri_bot.model import ChatModel
from prompts import EXTRACT, GENERATE, REVIEW

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORPUS = ROOT.parent / 'ATRI_dialogue/dialogue.jsonl'
OUT = ROOT / 'resources/daily_routines'
CACHE = HERE / 'cache'


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(dump(value) + '\n', encoding='utf-8')
    temp.replace(path)


def read_source():
    rows = []
    for n, line in enumerate(CORPUS.read_text().splitlines(), 1):
        row = json.loads(line)
        langs = row['readable_languages']
        text = next((langs[k] for k in ('zh-Hans', 'ja', 'zh-Hant', 'en') if (langs.get(k) or {}).get('text')), None)
        assert text is not None
        rows.append({'n': n, 'id': row['id'], 'file': row['file'], 'scene': row['scene_index'],
                     'type': row['type'], 'speaker': text.get('speaker') or '旁白（通常是夏生视角）',
                     'text': text['text'].replace('\\n', ' ').strip('"'), 'character': row.get('character')})
    return rows


def slim(rows):
    return [{'n': r['n'], 'scene': r['scene'], 'type': r['type'], 'speaker': r['speaker'], 'text': r['text']} for r in rows]


class Client:
    def __init__(self, model, concurrency):
        self.model, self.gate = model, asyncio.Semaphore(concurrency)

    async def ask(self, key, prompt, content, check, limit=7000):
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': dump(content)}]
        digest = hashlib.sha256(dump({'messages': messages, 'model': self.model.config.model,
                                     'thinking': self.model.config.thinking, 'limit': limit}).encode()).hexdigest()
        cached = CACHE / f'{key}.json'
        if cached.exists():
            old = json.loads(cached.read_text())
            if old.get('sha256') == digest and old.get('status') == 'accepted':
                result = json.loads(old['response'])
                normalize_routine(result)
                check(result)
                return result
        # A schema-only relaxation can reuse the exact saved response after revalidation.
        # Keep the original failed file so the reason for recovery remains inspectable.
        for failed in sorted(CACHE.glob(f'{key}.failed-*.json')):
            old = json.loads(failed.read_text())
            if old.get('sha256') != digest:
                continue
            try:
                result = json.loads(old['response'])
                normalize_routine(result)
                check(result)
            except (ValueError, KeyError, TypeError, AssertionError):
                continue
            save(cached, {**old, 'status': 'accepted', 'model': self.model.config.model,
                          'revalidated_from': failed.name, 'note': 'Original response preserved; current schema revalidated.'})
            print(f'[重新校验通过] {key}', flush=True)
            return result
        errors = []
        for attempt in range(1, 4):
            request_messages = [dict(m) for m in messages]
            if errors:
                request_messages[-1]['content'] += '\n上次输出的结构问题：' + errors[-1] + '。请重新输出完整JSON。'
            async with self.gate:
                print(f'[请求] {key} attempt={attempt}', flush=True)
                started = time.perf_counter()
                response = ''
                try:
                    response = await self.model.complete(request_messages, purpose=f'routine.{key}',
                                                         json_mode=True, max_output_tokens=limit)
                    value = json.loads(response)
                    normalize_routine(value)
                    check(value)
                except Exception as exc:
                    errors.append(f'{type(exc).__name__}: {exc}')
                    save(CACHE / f'{key}.failed-{attempt}.json', {'sha256': digest, 'response': response, 'error': errors[-1]})
                    print(f'[重试] {key} {errors[-1]}', flush=True)
                    continue
                save(cached, {'sha256': digest, 'status': 'accepted', 'response': response,
                              'model': self.model.config.model, 'seconds': round(time.perf_counter()-started, 3),
                              'attempt': attempt, 'errors': errors})
                print(f'[完成] {key} {time.perf_counter()-started:.1f}s', flush=True)
                return value
        raise RuntimeError(f'{key}: {errors[-1]}')


def validate_extraction(result, rows):
    assert isinstance(result['chapter_note'], str)
    assert isinstance(result['candidates'], list)
    lookup = {r['n']: r for r in rows}
    for c in result['candidates']:
        for field in ['title', 'category', 'core', 'character_hook', 'adaptation']:
            assert isinstance(c[field], str) and c[field].strip(), field
        assert 2 <= len(c['evidence']) <= 64, 'evidence needs 2–64 lines'
        assert all(type(n) is int and n in lookup for n in c['evidence']), 'evidence outside chapter'
        assert len(set(c['evidence'])) == len(c['evidence']), 'duplicate evidence'
        assert isinstance(c['canon_facts'], list) and len(c['canon_facts']) >= 2
        assert c['strength'] in ('direct', 'adjacent')
        assert isinstance(c['cast'], list) and '亚托莉' in c['cast']
        assert type(c['spoiler_context']) is bool


async def extract(client, rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['file']].append(row)

    async def chapter(name, records):
        key = 'extract_' + re.sub(r'\W+', '_', name)
        result = await client.ask(key, EXTRACT, {'chapter': name, 'records': slim(records)},
                                  lambda v: validate_extraction(v, records), limit=10000)
        return name, records, result

    results = await asyncio.gather(*(chapter(k, v) for k, v in groups.items()), return_exceptions=True)
    failures = [str(r) for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError('\n'.join(failures))
    candidates, coverage = [], []
    for name, records, result in results:
        for i, candidate in enumerate(result['candidates'], 1):
            key = re.sub(r'\.ks\.scn$', '', name).replace('/', '_') + f'-{i:02}'
            candidates.append({'id': key, 'source_chapter': name, **candidate})
        coverage.append({'chapter': name, 'rows': len(records), 'source_lines': [records[0]['n'], records[-1]['n']],
                         'candidate_count': len(result['candidates']), 'note': result['chapter_note']})
    save(HERE / 'candidates.json', {'source_sha256': hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
                                   'record_count': len(rows), 'chapter_count': len(groups),
                                   'coverage': coverage, 'candidates': candidates})
    print(f'[提取汇总] {len(rows)} records / {len(groups)} chapters / {len(candidates)} candidates', flush=True)


def context(candidate, rows):
    evidence = set(candidate['evidence'])
    nearby = set(n+d for n in evidence for d in range(-5, 6))
    return [r for r in rows if r['file'] == candidate['source_chapter'] and r['n'] in nearby]


def normalize_routine(value):
    """Align macro boundaries to the actual twelve micro slots; never edit prose."""
    if not isinstance(value, dict) or not isinstance(value.get('micro'), list) or not isinstance(value.get('macro'), list):
        return
    micro, macro = value['micro'], value['macro']
    if len(micro) != 12 or any(s.get('start_minute') != i*10 or s.get('end_minute') != (i+1)*10 for i, s in enumerate(micro)):
        return  # Validation supplies the error; never invent missing slots.
    groups = []
    for slot in micro:
        if not groups or groups[-1]['activity'] != slot.get('activity'):
            groups.append({'activity': slot.get('activity'), 'start_minute': slot['start_minute'], 'end_minute': slot['end_minute']})
        else:
            groups[-1]['end_minute'] = slot['end_minute']
    if [g['activity'] for g in groups] != [g.get('activity') for g in macro]:
        return
    changes = []
    for block, group in zip(macro, groups, strict=True):
        previous = [block.get('start_minute'), block.get('end_minute')]
        following = [group['start_minute'], group['end_minute']]
        if previous != following:
            changes.append({'activity': block['activity'], 'from_minutes': previous, 'to_minutes': following})
            block.update(group)
    if changes:
        value['normalization'] = {'method': 'macro_times_aligned_to_existing_micro_groups', 'changes': changes,
                                  'note': '只将大阶段边界对齐已有十分钟格，原始文字与微格未改写。'}


def validate_routine(x):
    for key, limit in [('title', 30), ('summary', 160), ('scene', 120)]:
        assert isinstance(x[key], str) and 0 < len(x[key]) <= limit, key
    for key in ['preconditions', 'adaptation_notes', 'suggested_time_of_day', 'participants']:
        assert isinstance(x[key], list) and x[key] and all(isinstance(s, str) and s for s in x[key]), key
    assert '亚托莉' in x['participants'], 'ATRI missing'
    assert 2 <= len(x['macro']) <= 5, 'macro block count'
    end = 0
    for block in x['macro']:
        assert block['start_minute'] == end, 'macro overlap/gap'
        assert type(block['end_minute']) is int and end < block['end_minute'] <= 120 and block['end_minute'] % 10 == 0, f'macro endpoint must increase on ten-minute grid: {block}'
        assert isinstance(block['activity'], str) and 0 < len(block['activity']) <= 24, 'macro activity needs 1–24 characters'
        assert isinstance(block['intent'], str) and block['intent']
        end = block['end_minute']
    assert end == 120, 'macro must span 120 minutes'
    assert len(x['micro']) == 12, 'micro must contain 12 slots'
    for i, slot in enumerate(x['micro']):
        assert slot['start_minute'] == i*10 and slot['end_minute'] == (i+1)*10, f'slot {i} time'
        parent = next(b for b in x['macro'] if b['start_minute'] <= i*10 < b['end_minute'])
        assert slot['activity'] == parent['activity'], f'slot {i} activity mismatch'
        assert isinstance(slot['detail'], str) and 8 <= len(slot['detail']) <= 100, f'slot {i} detail'
        assert isinstance(slot['mood'], str) and 0 < len(slot['mood']) <= 24


def chosen_candidates():
    source = json.loads((HERE / 'candidates.json').read_text())
    assert source['source_sha256'] == hashlib.sha256(CORPUS.read_bytes()).hexdigest(), 'corpus changed'
    decisions_path = HERE / 'selection.json'
    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    candidates = []
    extra_path = HERE / 'manual_candidates.json'
    extras = json.loads(extra_path.read_text()) if extra_path.exists() else []
    for candidate in source['candidates'] + extras:
        decision = decisions.get(candidate['id'], {})
        if decision.get('status') in ('duplicate', 'exclude'):
            continue
        candidates.append({**candidate, **decision.get('override', {}), 'editor_note': decision.get('note', '')})
    return candidates


def artifact(candidate, generated, rows, review=None):
    validate_routine(generated)
    evidence = [r for r in rows if r['n'] in candidate['evidence']]
    return {'schema_version': 1, 'id': candidate['id'], 'category': candidate['category'],
            'duration_minutes': 120, 'time_basis': 'relative_minutes', 'status': 'pending_user_review',
            'source_strength': candidate['strength'], 'source_contains_spoilers': candidate['spoiler_context'],
            **generated, 'source': {'chapter': candidate['source_chapter'],
                'file': '../../../ATRI_dialogue/dialogue.jsonl',
                'canon_facts': candidate['canon_facts'], 'character_hook': candidate['character_hook'],
                'adaptation': candidate['adaptation'], 'editor_note': candidate.get('editor_note', ''),
                'evidence': [{'line': r['n'], 'id': r['id'], 'speaker': r['speaker'], 'text': r['text']} for r in evidence]},
            'review': review or {'verdict': 'unreviewed', 'issues': []}}


def output_path(candidate):
    title = re.sub(r'[\\/:*?"<>|\n]', '', candidate['title'])
    return OUT / f"{candidate['id']}_{title}.json"


async def generate(client, rows, persona, selected_ids):
    candidates = chosen_candidates()
    if selected_ids:
        candidates = [c for c in candidates if c['id'] in selected_ids]

    async def one(c):
        path = output_path(c)
        if path.exists():
            existing = json.loads(path.read_text())
            validate_routine(existing)
            return
        published_path = CACHE / 'published.json'
        published = json.loads(published_path.read_text()) if published_path.exists() else {}
        if c['id'] in published:
            print(f"[尊重删除] {c['id']} 已发布过，文件缺失时不自动补回", flush=True)
            return
        data = {'persona': persona, 'candidate': c, 'original_context': slim(context(c, rows))}
        generated = await client.ask('generate_' + c['id'], GENERATE, data, validate_routine, limit=5000)
        save(path, artifact(c, generated, rows))
        published = json.loads(published_path.read_text()) if published_path.exists() else {}
        published[c['id']] = str(path.relative_to(ROOT))
        save(published_path, published)
    results = await asyncio.gather(*(one(c) for c in candidates), return_exceptions=True)
    failures = [str(r) for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(f'[生成汇总] checked {len(candidates)} routines', flush=True)


def validate_review(value):
    assert value['verdict'] in ('pass', 'revise')
    assert isinstance(value['issues'], list) and isinstance(value['note'], str)
    for issue in value['issues']:
        assert all(isinstance(issue[k], str) and issue[k] for k in ['kind', 'location', 'detail'])
    assert bool(value['issues']) == (value['verdict'] == 'revise')


def generated_fields(document):
    fields = {k: document[k] for k in ['title', 'summary', 'suggested_time_of_day', 'scene', 'participants',
                                     'preconditions', 'adaptation_notes', 'macro', 'micro']}
    if 'normalization' in document:
        fields['normalization'] = document['normalization']
    return fields


async def review(client, rows, persona, selected_ids, max_revisions=0):
    candidates = chosen_candidates()
    if selected_ids:
        candidates = [c for c in candidates if c['id'] in selected_ids]

    async def one(c):
        path = output_path(c)
        if not path.exists():
            return
        document = json.loads(path.read_text())
        generated = generated_fields(document)
        content_hash = hashlib.sha256(dump(generated).encode()).hexdigest()
        prompt_hash = hashlib.sha256((REVIEW + GENERATE).encode()).hexdigest()
        reference_hash = hashlib.sha256(dump({'candidate': c, 'context': slim(context(c, rows)), 'persona': persona}).encode()).hexdigest()
        old_review = document.get('review', {})
        if (old_review.get('content_sha256') == content_hash and old_review.get('prompts_sha256') == prompt_hash
                and old_review.get('reference_sha256') == reference_hash):
            return
        history = []
        for revision in range(max_revisions + 1):
            assessment = await client.ask(f'review_{c["id"]}_{revision}', REVIEW,
                {'candidate': c, 'original_context': slim(context(c, rows)), 'routine': generated},
                validate_review, limit=1800)
            history.append(assessment)
            if assessment['verdict'] == 'pass' or revision == max_revisions:
                assessment = {**assessment, 'type': 'model_editorial_review', 'revision_count': revision,
                              'history': history, 'user_approval': False,
                              'advisory_only': True,
                              'content_sha256': hashlib.sha256(dump(generated).encode()).hexdigest(),
                              'prompts_sha256': prompt_hash, 'reference_sha256': reference_hash}
                save(path, artifact(c, generated, rows, assessment))
                return
            generated = await client.ask(f'repair_{c["id"]}_{revision + 1}', GENERATE + '\n本次根据编辑审阅修订。保持原主题、与原文一致的角色行为和已经合理的日程。只修具体问题，不增加无关话题。',
                {'persona': persona, 'candidate': c, 'original_context': slim(context(c, rows)),
                 'previous_draft': generated, 'editorial_issues': assessment['issues']},
                validate_routine, limit=5000)
    results = await asyncio.gather(*(one(c) for c in candidates), return_exceptions=True)
    failures = [str(r) for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(f'[审阅汇总] checked {len(candidates)} candidates', flush=True)


async def refine(client, rows, persona, selected_ids):
    """Apply concrete source-checked notes, never the raw reviewer's suggestions."""
    notes = json.loads((HERE / 'editorial_fixes.json').read_text())
    candidates = [c for c in chosen_candidates() if c['id'] in notes and (not selected_ids or c['id'] in selected_ids)]

    async def one(c):
        path = output_path(c)
        if not path.exists():
            return  # Respect manual deletion here as well.
        document = json.loads(path.read_text())
        note = notes[c['id']]
        fingerprint = hashlib.sha256(dump({'candidate': c, 'note': note, 'prompt': GENERATE}).encode()).hexdigest()
        old_review = document.get('review', {})
        content_hash = hashlib.sha256(dump(generated_fields(document)).encode()).hexdigest()
        if old_review.get('editorial_sha256') == fingerprint and old_review.get('content_sha256') == content_hash:
            return
        generated = await client.ask('refine_' + c['id'], GENERATE + '\n本次只按人工核实的修订要求改写，不采用未经核实的自动审阅意见。每个大阶段的起止分钟必须是10的整数倍，微格activity逐字沿用对应大阶段名。所有十分钟格只各写一条当前关注，不必反复强调禁止事项。',
            {'persona': persona, 'candidate': c, 'original_context': slim(context(c, rows)),
             'previous_draft': generated_fields(document), 'verified_editorial_instruction': note}, validate_routine, limit=5000)
        save(path, artifact(c, generated, rows, {'verdict': 'revised', 'issues': [],
            'type': 'source_guided_revision', 'note': note, 'user_approval': False,
            'editorial_sha256': fingerprint,
            'content_sha256': hashlib.sha256(dump(generated).encode()).hexdigest()}))

    results = await asyncio.gather(*(one(c) for c in candidates), return_exceptions=True)
    failures = [str(r) for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(f'[定向修订汇总] checked {len(candidates)} routines', flush=True)


def index_and_check(rows):
    lookup = {r['n']: r for r in rows}
    candidates = {c['id']: c for c in chosen_candidates()}
    extracted = json.loads((HERE / 'candidates.json').read_text())
    decisions = json.loads((HERE / 'selection.json').read_text())
    extras = json.loads((HERE / 'manual_candidates.json').read_text())
    all_candidates = extracted['candidates'] + extras
    assert len({c['id'] for c in all_candidates}) == len(all_candidates), 'duplicate candidate IDs'
    assert set(decisions) == {c['id'] for c in all_candidates}, 'candidate without explicit selection'
    assert all(d['status'] in ('keep', 'exclude', 'duplicate') for d in decisions.values()), 'selection status'
    chapters = defaultdict(list)
    for r in rows:
        chapters[r['file']].append(r)
    assert extracted['record_count'] == len(rows) and extracted['chapter_count'] == len(chapters)
    assert {c['chapter'] for c in extracted['coverage']} == set(chapters), 'chapter coverage missing'
    assert len(extracted['coverage']) == len(chapters), 'duplicate chapter coverage'
    for coverage in extracted['coverage']:
        chapter_rows = chapters[coverage['chapter']]
        assert coverage['rows'] == len(chapter_rows)
        assert coverage['source_lines'] == [chapter_rows[0]['n'], chapter_rows[-1]['n']]
        assert coverage['candidate_count'] == sum(c['source_chapter'] == coverage['chapter'] for c in extracted['candidates'])
    files = sorted(OUT.glob('*.json'))
    documents = []
    for path in files:
        document = json.loads(path.read_text())
        validate_routine(document)
        assert document['id'] in candidates, f'{path.name} is not a selected candidate'
        candidate = candidates[document['id']]
        assert document['status'] == 'pending_user_review'
        assert document['source_strength'] == candidate['strength']
        assert document['source']['chapter'] == candidate['source_chapter']
        assert document['source']['canon_facts'] == candidate['canon_facts']
        assert document['source']['character_hook'] == candidate['character_hook']
        assert document['source']['adaptation'] == candidate['adaptation']
        assert sorted(e['line'] for e in document['source']['evidence']) == sorted(candidate['evidence'])
        assert document['duration_minutes'] == 120 and document['time_basis'] == 'relative_minutes'
        for e in document['source']['evidence']:
            source = lookup[e['line']]
            assert source['file'] == candidate['source_chapter'], f'{path.name} evidence from wrong chapter'
            assert all(e[k] == source[k] for k in ['id', 'speaker', 'text']), f'{path.name} wrong evidence'
        assert (OUT / document['source']['file']).resolve() == CORPUS.resolve()
        documents.append((path, document))
    assert len({r['id'] for p, r in documents}) == len(files), 'duplicate IDs'
    assert len({dump(r['micro']) for p, r in documents}) == len(files), 'identical routines'
    categories = Counter(r['category'] for p, r in documents)
    output_ids = {r['id'] for p, r in documents}
    published_path = CACHE / 'published.json'
    published = json.loads(published_path.read_text()) if published_path.exists() else {}
    never_published = set(candidates) - output_ids - set(published)
    assert not never_published, f'selected routines not generated: {sorted(never_published)}'
    lines = ['# ATRI 两小时日常素材库', '',
             f'当前目录共有 **{len(files)}份独立JSON**，每份两小时大日程＋十二格十分钟小日程，当前批次已获用户认可，可继续手动删改。', '',
             '不喜欢的直接删除对应JSON即可。索引从当前存在的文件生成；生成器也会尊重已发布后手动删除的文件。文件名按原文脚本排序，编号不是发生顺序或推荐程度。', '',
             '所有时间都从活动开始计时：`start_minute: 0` 表示开始，`end_minute: 120` 表示两小时结束。`suggested_time_of_day`给出适合的时段，不把某一天的钟点固定进可复用素材。每格是这段活动的关注点，不要求所提到的每个小动作持续十分钟。', '',
             '这是依原作改编的虚构日常，不是官方作息。保留必要的学校、船、海边和原作伙伴；群友不被代入原作身份。改编原则是剥离亲密、事故、倒计时和结局结果，具体处理见各份说明；包含晚期出处的文件有剧透标记。诗菜相关的过去场景、愿望改编等需要先检查`preconditions`，各窗按独立虚构场景使用；当前排班只按时段筛选，不追踪跨窗口的地点、人物或剧情连续性。', '',
             '每份包含`macro`、`micro`、`scene`、`participants`、`preconditions`、`adaptation_notes`和`source.evidence`。引文与行号由原始文件直接提取，模型不负责编造引用。`source_strength=adjacent`表示需做额外迁移的方向，建议优先检查。', '',
             '编辑记录在`review`中：`revised`表示按已核实的问题定向修订；`pass`/`revise`仅是可能误判的模型建议，不是原作事实或你的认可。`status`始终为`pending_user_review`。出处属实不代表两小时的每个动作都出自原作，创作补足见改编说明。', '',
             '少数模型输出的大阶段边界不是十分钟整点，导出时按已有十二格的活动分组对齐。若发生这种结构修正，会在`normalization`保留前后时间；不会因此改写文字或补造小格。', '',
             '本库已接入正式聊天：上海时间每两小时按时段随机选一份，传入整体安排和当前十分钟格；00:00–08:00 固定睡觉、不回复。运行时不会生成或改写素材，也不按历史 `review.status` 筛除已认可条目。不能把未来结果当成已发生经历。', '',
             '## 可以先看的几份', '',
             '- [做汉堡排和热菜沙拉](b204-01_做汉堡排和热菜沙拉.json)：肉馅、洋葱、按次数揉馅，以及不太愿意别人接手的自信。',
             '- [料理部学来的土豆牛肉](b206-04_料理部学来的土豆牛肉.json)：跟水菜萌学菜，保留炖煮与等候。',
             '- [放学路上的鲷鱼烧](b206-05_放学路上的鲷鱼烧.json)：嘴馋、逛摊和回家路。',
             '- [听钢琴时不自觉地哼唱](b203-01_听钢琴时不自觉地哼唱.json)：夏生弹琴，亚托莉听与哼唱。',
             '- [商店街文具店挑选笔记用品](b113-03_商店街文具店挑选笔记用品.json)：面对许多纸笔时的好奇与犹豫。',
             '- [船上手洗和晾晒衣物](b407-06_在船上手洗和晾晒衣物.json)：保留原作没有洗衣机、手洗衣物的条件。', '',
             '## 分类索引', '',
             '| 类别 | 数量 |', '| --- | ---: |']
    lines += [f'| {category} | {count} |' for category, count in sorted(categories.items())]
    for category in sorted(categories):
        lines += ['', f'## {category}', '', '| 文件 | 两小时主题 | 参与者 | 来源/审阅 |', '| --- | --- | --- | --- |']
        for path, r in documents:
            if r['category'] != category:
                continue
            label = '直接依据' if r['source_strength'] == 'direct' else '邻近改编'
            if r['review']['verdict'] == 'revise':
                label += '；有待选问题'
            lines.append(f"| [{path.stem}]({path.name}) | {r['summary'].replace('|', '／')} | {'、'.join(r['participants'])} | {label} |")
    # Starter links should disappear too if the user deletes a suggested routine.
    lines = [line for line in lines if not line.startswith('- [') or
             (OUT / re.search(r'\]\(([^)]+)\)', line).group(1)).exists()]
    lines += ['', '## 来源和生成记录', '',
              '全量来源：[dialogue.jsonl](../../../ATRI_dialogue/dialogue.jsonl)。逐章候选和覆盖记录：[candidates.json](../../experiments/daily_routines/candidates.json)。补充候选：[manual_candidates.json](../../experiments/daily_routines/manual_candidates.json)。候选取舍：[selection.json](../../experiments/daily_routines/selection.json)。已核实的修订要求：[editorial_fixes.json](../../experiments/daily_routines/editorial_fixes.json)。提示词：[prompts.py](../../experiments/daily_routines/prompts.py)。制作过程：[README](../../experiments/daily_routines/README.md)。', '',
              '删除JSON后刷新索引及检查结构：在atri目录运行 `uv run python experiments/daily_routines/build.py index`，不会调用API。', '']
    (OUT / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
    summary = {'files': len(files), 'slots': len(files)*12, 'categories': dict(categories),
               'corpus_records': len(rows), 'corpus_chapters': len(chapters),
               'extracted_candidates': len(extracted['candidates']), 'manual_candidates': len(extras),
               'selection': dict(Counter(d['status'] for d in decisions.values())),
               'deleted_after_publication': sorted(set(candidates) - output_ids),
               'editorial_status': dict(Counter(r['review']['verdict'] for p, r in documents)),
               'source_sha256': hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
               'checks': ['120 minutes', '12 contiguous slots', 'macro ownership', 'exact source excerpts and chapter',
                          'unique IDs and micro schedules', 'every source chapter accounted for',
                          'explicit selection for every candidate', 'selected source metadata preserved',
                          'all kept candidates published or explicitly deleted after publication']}
    save(HERE / 'verification.json', summary)
    print(dump(summary), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['extract', 'generate', 'review', 'refine', 'index'])
    parser.add_argument('--ids', nargs='*')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--max-revisions', type=int, choices=[0, 1, 2], default=0,
                        help='review only by default; explicitly opt into up to two automatic rewrites')
    args = parser.parse_args()
    rows = read_source()
    if args.command == 'index':
        index_and_check(rows)
        return
    config = Config.load(ROOT / 'config.toml')
    config.require_live()
    async with aiohttp.ClientSession() as session:
        client = Client(ChatModel(config, session), args.concurrency)
        if args.command == 'extract':
            await extract(client, rows)
        elif args.command == 'generate':
            await generate(client, rows, config.read_personal_info(), args.ids)
        elif args.command == 'refine':
            await refine(client, rows, config.read_personal_info(), args.ids)
        else:
            await review(client, rows, config.read_personal_info(), args.ids, args.max_revisions)


if __name__ == '__main__':
    asyncio.run(main())
