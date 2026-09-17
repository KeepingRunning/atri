"""Exercise link ingestion/reading without QQ or production chat storage."""
import json
import sys
import time

import aiohttp

from .link_tools import LinkReader, register_links
from .documents import DocumentStore
from .document_analysis import DocumentProcessor
from .model import ChatModel
from .cloud_asr import CloudASR
from .video_cache import VideoSourceCache
from .mcp_client import MCPManager
from .tools import ToolContext, ToolRegistry


async def run_link_test(config, url, *, stream=None):
    stream = stream or sys.stdout
    if not config.links.enabled or not config.mcp.enabled or not config.tools.enabled:
        raise ValueError("test-links requires links.enabled, mcp.enabled and tools.enabled")
    manager = MCPManager(config.mcp, config.root)
    context = ToolContext("link-test", "link-test", "link-test", "link-test", time.time(),
                          None, lambda: None, lambda _: None)
    print("链接测试：调用真实解析服务；按配置调用百炼转写和文档模型，不连接 QQ。", file=stream, flush=True)
    try:
        await manager.start()
        async with aiohttp.ClientSession() as session:
            processor = None
            if config.documents.enabled:
                store = DocumentStore(config.data / "diagnostics" / "link-reading",
                    ttl_seconds=config.links.cache_ttl_seconds,
                    max_documents_per_scope=config.links.max_documents_per_group)
                processor = DocumentProcessor(config.documents, ChatModel(config, session), store)
            reader = LinkReader(config.links, manager, max_result_chars=config.tools.max_result_chars,
                                processor=processor, asr=CloudASR(config.asr) if config.asr.enabled else None,
                                video_cache=VideoSourceCache(config.data / "diagnostics" / "video-sources",
                                                            ttl_seconds=config.links.cache_ttl_seconds))
            registry = ToolRegistry()
            register_links(registry, reader)
            result = await registry.execute("read_link", json.dumps({"url": url}), context, config.tools)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), file=stream, flush=True)
        if result.ok and result.meta.get("partial"):
            print("读取成功，但只取得部分内容；请查看 partial / warning / read_sections。", file=stream)
        return result.ok
    finally:
        await manager.close()
