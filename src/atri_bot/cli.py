import argparse
import asyncio
import logging
import signal
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from .api_test import run_api_tests, run_vision_test, run_planner_tests
from .bot import Bot
from .config import Config
from .model import ChatModel
from .logging_setup import configure_logging
from .onebot import create_app
from .storage import single_instance
from .schedule import ScheduleService
from .link_test import run_link_test
from .asr_test import run_asr_test
from .document_test import run_document_test


async def preview_schedule(config, at=None):
    """本地抽选预览；无需API，不启动QQ，不改正式日程缓存。"""
    zone = ZoneInfo(config.schedule.timezone)
    now = datetime.fromisoformat(at) if at else datetime.now(zone)
    now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    with TemporaryDirectory(prefix='atri-schedule-preview-') as directory:
        service = ScheduleService(config.schedule, directory, root=config.root, now=lambda: now)
        plan = service.current_plan()
        print(f"日程预览：{plan['window_start']} 至 {plan['window_end']}")
        print(f"{plan['macro']['title']}（{plan['routine_id']}）")
        print(plan['macro']['summary'])
        if plan['sleeping']:
            print('睡眠时段：不回复任何消息，包括 @ 和引用。')
        elif not config.schedule.enabled:
            print('日程背景已关闭；白天聊天不注入日程。')
        for slot in plan['micro']:
            print(f"{slot['start'][11:16]}–{slot['end'][11:16]} | {slot['activity']} | {slot['detail']} | {slot['mood']}")
        print('\n当前聊天使用的背景：\n' + service.context())
        return True


async def serve(config):
    config.require_serve()
    log = logging.getLogger("atri.core")
    log.info("[启动] 群数量=%d 模式=%s 频率=%.2f 模型并发=%d 日志级别=%s",
             len(config.groups), config.reply.mode,
             config.reply.frequency, config.parallel, config.logging.level)
    if config.reply.mode == "planner":
        log.info("[规划配置] 合批安静间隔=%.1fs 最多收集=%.1fs 等待次数=%d 重规划次数=%d",
                 config.planner.debounce_seconds, config.planner.max_batch_seconds,
                 config.planner.max_waits, config.planner.max_replans)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    sigterm_installed = False
    try:
        # Docker stops the main process with SIGTERM. Keep asyncio.run's SIGINT
        # handling so Ctrl+C still cancels serve and enters the same cleanup.
        try:
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            sigterm_installed = True
        except (NotImplementedError, RuntimeError):
            # Windows and event loops outside the main thread do not support it.
            log.debug("[信号处理] 当前事件循环不支持 SIGTERM，保留系统默认处理")
        with single_instance(config.data):
            async with aiohttp.ClientSession() as session:
                bot = Bot(config, ChatModel(config, session))
                runner = web.AppRunner(create_app(config, bot), access_log=None)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, config.host, config.port).start()
                    log.info("[就绪] 监听 %s:%s%s，等待 OneBot 连接", config.host, config.port, config.ws_path)
                    await stop.wait()
                finally:
                    log.info("[停止] 正在关闭连接并等待消息队列结束")
                    try:
                        await runner.cleanup()
                    finally:
                        # A failure before aiohttp's cleanup context yields may
                        # leave partially started MCP or schedule resources.
                        await bot.close()
                    log.info("[停止] 服务已退出")
    finally:
        if sigterm_installed:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ATRI QQ chatbot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    parser.add_argument("--log-color", choices=("auto", "always", "never"))
    parser.add_argument("command", nargs="?", choices=("serve", "check", "test-api", "test-planner", "test-schedule", "test-vision", "test-links", "test-asr", "test-document"), default="serve",
                        help="serve 启动服务；check 检查配置；test-api 实测模型接口；test-planner 实测规划与回复；test-schedule 预览日程；test-vision 测试看图；test-links 测试链接解析；test-asr 测试百炼语音转写；test-document 测试本地文档概览与证据读取")
    parser.add_argument("--at", help="仅供test-schedule：预览时刻，如2026-09-11T18:35:00+08:00")
    parser.add_argument("--image", type=Path, help="仅供test-vision：要上传测试的本地图片，省略则使用合成图")
    parser.add_argument("--url", help="test-links 的读取链接；test-document 可选的原文来源链接")
    parser.add_argument("--document", type=Path, help="仅供test-document：本地 UTF-8 文本、Markdown 或 ASR JSON")
    parser.add_argument("--question", help="仅供test-document：需要定位原文的问题，省略则生成概览")
    args = parser.parse_args(argv)
    if args.at and args.command != 'test-schedule':
        parser.error('--at only applies to test-schedule')
    if args.image and args.command != 'test-vision':
        parser.error('--image only applies to test-vision')
    if args.url and args.command not in ('test-links', 'test-document'):
        parser.error('--url only applies to test-links or test-document')
    if args.command == 'test-links' and not args.url:
        parser.error('test-links requires --url')
    if args.document is not None and args.command != 'test-document':
        parser.error('--document only applies to test-document')
    if args.question is not None and args.command != 'test-document':
        parser.error('--question only applies to test-document')
    if args.command == 'test-document' and args.document is None:
        parser.error('test-document requires --document')
    try:
        config = Config.load(args.config)
        if args.log_level:
            config.logging.level = args.log_level
        if args.log_color:
            config.logging.color = args.log_color
        # Explicit media/document diagnostics never write the live bot's log.
        if args.command in ("test-asr", "test-document", "test-links"):
            config.logging.file = ""
        configure_logging(config.logging, config.root, secrets=(config.api_key, config.token, config.asr.api_key))
        if args.command == "check":
            config.require_serve()
            print("配置检查通过。")
        elif args.command == "test-api":
            results = asyncio.run(run_api_tests(config))
            if not all(result.passed for result in results):
                parser.exit(1)
        elif args.command == "test-planner":
            if not all(result.passed for result in asyncio.run(run_planner_tests(config))):
                parser.exit(1)
        elif args.command == "test-vision":
            if not asyncio.run(run_vision_test(config, image_path=args.image)).passed:
                parser.exit(1)
        elif args.command == "test-links":
            if not asyncio.run(run_link_test(config, args.url)):
                parser.exit(1)
        elif args.command == "test-asr":
            if not asyncio.run(run_asr_test(config)):
                parser.exit(1)
        elif args.command == "test-document":
            if not asyncio.run(run_document_test(config, args.document, url=args.url, question=args.question)):
                parser.exit(1)
        elif args.command == 'test-schedule':
            if not asyncio.run(preview_schedule(config, args.at)):
                parser.exit(1)
        else:
            asyncio.run(serve(config))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"配置或启动失败：{exc}\n")
    except KeyboardInterrupt:
        if args.command in ("test-api", "test-planner", "test-schedule", "test-vision", "test-links", "test-asr", "test-document"):
            parser.exit(130, "测试已中断。\n")


if __name__ == "__main__":
    main()
