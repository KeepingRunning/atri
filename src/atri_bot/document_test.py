"""Read an explicitly supplied local document without QQ, MCP or new ASR jobs."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
import stat
import sys
import time

import aiohttp

from .document_analysis import DocumentProcessor
from .documents import DocumentStore, build_document
from .link_tools import LinkReader, validate_link
from .model import ChatModel, ModelError
from .tools import ToolContext, ToolError, ToolResult


MAX_INPUT_BYTES = 16 * 1024 * 1024
DIAGNOSTIC_SCOPE = ("document-test", "document-test")


def load_document_input(document_path: Path, *, max_chars: int, chunk_chars: int,
                        url: str | None = None):
    """Keep source text verbatim; accept only bounded UTF-8 text/Markdown/ASR JSON."""
    path = Path(document_path)
    if path.suffix.lower() not in (".txt", ".md", ".json"):
        raise ValueError("文档须为 UTF-8 的 .txt、.md 或 ASR .json 文件。")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("文档路径须指向普通文件。")
        if info.st_size > MAX_INPUT_BYTES:
            raise ValueError("文档文件超过 16 MiB 读取上限。")
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise ValueError("无法读取指定文档，请检查文件是否存在及读取权限。") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("文档文件超过 16 MiB 读取上限。")
    try:
        contents = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("文档必须使用 UTF-8 编码。") from None

    title = path.stem[:120] or "本地文档"
    source, section, segments = "local_document", "article_text", []
    if path.suffix.lower() == ".json":
        def reject_constant(_):
            raise ValueError("Non-finite JSON value")

        try:
            value = json.loads(contents, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise ValueError("ASR JSON 格式无效。") from None
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise ValueError("ASR JSON 须包含字符串类型的 text 字段。")
        contents = value["text"]
        source, section = "local_asr", "asr_transcript"
        supplied_segments = value.get("segments", [])
        if not isinstance(supplied_segments, list) or len(supplied_segments) > 100000:
            raise ValueError("ASR segments 须为有界的句段数组。")
        previous_end = 0
        for segment in supplied_segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise ValueError("ASR 每个句段须包含字符串类型的 text 字段。")
            begin, end = segment.get("begin_time"), segment.get("end_time")
            if (any(type(item) not in (int, float) or not math.isfinite(item) for item in (begin, end))
                    or not 0 <= begin <= end <= 365 * 24 * 60 * 60 * 1000):
                raise ValueError("ASR 句段须包含有效的毫秒 begin_time/end_time。")
            # Alignment must come from the supplied source; never invent positions.
            text = segment["text"]
            if text.strip():
                position = contents.find(text, previous_end)
                if position == -1:
                    raise ValueError("ASR 句段与 text 原文不对应，无法可靠保留时间戳。")
                previous_end = position + len(text)
                segments.append({"text": text, "begin_time": begin, "end_time": end})
        supplied_title = value.get("title")
        if isinstance(supplied_title, str) and supplied_title.strip():
            title = supplied_title.strip()[:120]
        elif isinstance(value.get("bvid"), str) and re.fullmatch(r"BV[A-Za-z0-9]{10}", value["bvid"]):
            title = value["bvid"] + " 语音转写"

    if not contents.strip():
        raise ValueError("文档正文为空。")
    if len(contents) > max_chars:
        raise ValueError("文档正文超过 links.max_document_chars；本次失败，不截断后冒充全文。")
    if "\x00" in contents:
        raise ValueError("文档包含无效的空字符。")
    canonical = validate_link(url)[1] if url is not None else "local:document-test"
    return build_document(scope=DIAGNOSTIC_SCOPE, url=canonical, title=title, text=contents,
                          source=source, sections=((section, 0, len(contents)),),
                          segments=segments, chunk_chars=chunk_chars)


def _clock(milliseconds):
    seconds = int(milliseconds) // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def render_preview(document, result: ToolResult, *, question: str | None = None):
    """Show source coverage, the complete saved overview, and actual tool evidence."""
    analysis = document.analysis or {}
    chunks = {chunk.id: chunk for chunk in document.chunks}
    covered = analysis.get("covered_chunk_ids", [])
    complete = analysis.get("complete") is True and set(covered) == set(chunks)
    lines = [f"# {document.title}", "", f"来源：{document.url}", "",
             f"已保存原文：{len(document.text)} 字符，{len(chunks)} 个块。",
             f"概览覆盖：{len(covered)}/{len(chunks)} 块；完整覆盖所获原文：{'是' if complete else '否'}。",
             "本预览展示完整存盘概览和目录；机器人单轮实际获得的内容受工具预算限制，见文末阅读范围。",
             "本结果只处理所提供的文字，没有读取原图或视频画面；机器转写未经人工校对。", ""]
    if document.partial or document.truncated:
        lines.extend(["来源内容不完整或已截断，以上覆盖仅指实际取得的文字。", ""])
    if question:
        lines.extend([f"本轮问题：{question}", ""])
    if not result.ok:
        lines.extend(["## 本次读取失败", "", str((result.error or {}).get("code", "document_test_failed")), ""])
        return "\n".join(lines)
    if analysis:
        lines.extend(["## 中立概览", "", analysis.get("summary", ""), "", "## 内容目录", ""])
        for index, item in enumerate(analysis.get("outline", []), 1):
            ids = item.get("chunk_ids", [])
            references = "、".join(ids)
            times = [chunks[item_id] for item_id in ids if item_id in chunks
                     and chunks[item_id].begin_ms is not None and chunks[item_id].end_ms is not None]
            location = (f"，{_clock(min(chunk.begin_ms for chunk in times))}–"
                        f"{_clock(max(chunk.end_ms for chunk in times))}") if times else ""
            lines.extend([f"### {index}. {item.get('title', '')}（{references}{location}）", "",
                          item.get("summary", ""), ""])
        if analysis.get("key_points"):
            lines.extend(["## 主要观点与条件", ""])
            for item in analysis["key_points"]:
                lines.extend([f"- {item.get('text', '')}（{'、'.join(item.get('chunk_ids', []))}）", ""])
    else:
        lines.extend(["正文较短或概览未启用，本次未生成概览。", ""])

    lines.extend(["## 本轮实际返回的原文片段", ""])
    data = result.data or {}
    evidence = data.get("passages", [])
    if isinstance(evidence, list) and evidence:
        for item in evidence:
            if not isinstance(item, dict):
                continue
            location = (f"（原文块时间：{_clock(item['begin_ms'])}–{_clock(item['end_ms'])}）"
                        if item.get("begin_ms") is not None and item.get("end_ms") is not None else "")
            lines.extend([f"### {item.get('chunk_id', '原文片段')}{location}", "",
                          str(item.get("text", "")), ""])
            if item.get("complete") is False:
                lines.extend(["本轮仅返回该块的部分文字；上述时间范围属于原文块。", ""])
    elif isinstance(data.get("text"), str) and data["text"]:
        lines.extend([data["text"], ""])
    else:
        lines.extend(["本轮未展开原文块；目录和概览不等于逐字原文。", ""])
    lines.extend(["## 工具阅读范围", "", "```json",
                  json.dumps({"meta": result.meta, "range": data.get("range"),
                              "read_sections": data.get("read_sections"),
                              "raw_read_chunk_ids": result.meta.get("raw_read_chunk_ids"),
                              "has_more": data.get("has_more")}, ensure_ascii=False, indent=2),
                  "```", ""])
    return "\n".join(lines)


async def run_document_test(config, document_path: Path, *, url=None, question=None, stream=None) -> bool:
    stream = sys.stdout if stream is None else stream
    started = time.monotonic()
    try:
        if question is not None and (not isinstance(question, str) or not question.strip() or len(question) > 2000):
            raise ValueError("问题须为 1 至 2000 字符的文本。")
        config.require_live()
        config.documents.validate()
        document = load_document_input(document_path, max_chars=config.links.max_document_chars,
                                       chunk_chars=config.documents.chunk_chars, url=url)
    except (ValueError, ToolError) as exc:
        print(f"[失败] {exc}", file=stream, flush=True)
        return False

    print("文字阅读测试：仅将指定文档与可选问题提交给大模型；不读取聊天记录或人设，"
          "不连接 QQ/MCP，不重新下载或转写音频。", file=stream, flush=True)
    print(f"输入：{len(document.text)} 字符，{len(document.chunks)} 个块；总超时 "
          f"{config.documents.timeout:g} 秒，失败不重试。", file=stream, flush=True)
    directory = config.data / "diagnostics" / "document-reading"
    store = DocumentStore(directory / "store", ttl_seconds=config.links.cache_ttl_seconds,
                          max_documents_per_scope=config.links.max_documents_per_group)
    context = ToolContext("document-test", "document-test", "document-test", "document-test",
                          time.time(), None, lambda: None, lambda _: None)
    try:
        async with asyncio.timeout(config.documents.timeout):
            cached = await store.get(DIAGNOSTIC_SCOPE, document.id)
            if cached is not None and cached.digest == document.digest:
                document = cached
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.llm_timeout)) as session:
                processor = DocumentProcessor(config.documents, ChatModel(config, session), store)
                reader = LinkReader(config.links, None, max_result_chars=config.tools.max_result_chars,
                                    processor=processor)
                result = await reader.present_document(context, document, question=question,
                                                       cached=cached is not None)
            # prepare may return a new record, while presentation remains bounded.
            saved = await store.get(DIAGNOSTIC_SCOPE, document.id)
            if saved is not None:
                document = saved
    except TimeoutError:
        result = ToolResult.failure("tool_timeout", "文字处理总超时，本次测试失败且不重试。")
    except ToolError as exc:
        result = ToolResult.failure(exc.code, str(exc))
    except ModelError as exc:
        code = exc.code if re.fullmatch(r"[A-Za-z0-9_]{1,80}", exc.code) else "model_error"
        result = ToolResult.failure(code, "模型调用失败，本次测试结束且不重试。")
    except aiohttp.ClientError:
        result = ToolResult.failure("network_error", "模型网络连接失败，本次测试结束且不重试。")
    except OSError:
        result = ToolResult.failure("document_storage_failed", "诊断文档读写失败，请检查目录权限。")
    if not result.ok:
        result.meta["retryable"] = False

    report = {"document_id": document.id, "source_url": document.url,
              "source": document.source, "characters": len(document.text),
              "chunk_count": len(document.chunks), "question": question,
              "analysis": document.analysis, "tool_result": result.as_dict()}
    output_directory = directory / document.id
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                                allow_nan=False) + "\n", encoding="utf-8")
        (output_directory / "preview.md").write_text(render_preview(document, result, question=question),
                                                     encoding="utf-8")
    except OSError:
        print("[失败] 无法写入诊断结果，请检查目录权限。", file=stream, flush=True)
        return False

    display = result.as_dict()
    if len(result.to_json()) > 12000:
        display = {"ok": result.ok, "document_id": document.id, "meta": result.meta,
                   "note": "完整工具结果已写入 result.json。"}
    print(json.dumps(display, ensure_ascii=False, indent=2), file=stream, flush=True)
    print(f"[{'通过' if result.ok else '失败'}] 文字阅读 | {time.monotonic() - started:.2f} 秒", file=stream)
    print(f"预览：{output_directory / 'preview.md'}\n结果：{output_directory / 'result.json'}", file=stream, flush=True)
    return result.ok
