import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from .api_test import run_api_tests
from .bot import Bot
from .config import Config
from .model import ChatModel
from .logging_setup import configure_logging
from .onebot import create_app
from .storage import single_instance
from .schedule import ScheduleService


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
    log.info("[启动] 群数量=%d 模式=%s 意愿阈值=%d 频率=%.2f 模型并发=%d 日志级别=%s",
             len(config.groups), config.reply.mode, config.reply.threshold,
             config.reply.frequency, config.parallel, config.logging.level)
    with single_instance(config.data):
        async with aiohttp.ClientSession() as session:
            bot = Bot(config, ChatModel(config, session))
            runner = web.AppRunner(create_app(config, bot), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, config.host, config.port).start()
                log.info("[就绪] 监听 %s:%s%s，等待 OneBot 连接", config.host, config.port, config.ws_path)
                await asyncio.Event().wait()
            finally:
                log.info("[停止] 正在关闭连接并等待消息队列结束")
                await runner.cleanup()
                log.info("[停止] 服务已退出")


def main(argv=None):
    parser = argparse.ArgumentParser(description="ATRI QQ chatbot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    parser.add_argument("--log-color", choices=("auto", "always", "never"))
    parser.add_argument("command", nargs="?", choices=("serve", "check", "test-api", "test-schedule"), default="serve",
                        help="serve 启动服务；check 检查配置；test-api 实测模型接口；test-schedule 预览日程")
    parser.add_argument("--at", help="仅供test-schedule：预览时刻，如2026-09-11T18:35:00+08:00")
    args = parser.parse_args(argv)
    if args.at and args.command != 'test-schedule':
        parser.error('--at only applies to test-schedule')
    try:
        config = Config.load(args.config)
        if args.log_level:
            config.logging.level = args.log_level
        if args.log_color:
            config.logging.color = args.log_color
        configure_logging(config.logging, config.root, secrets=(config.api_key, config.token))
        if args.command == "check":
            config.require_serve()
            print("配置检查通过。")
        elif args.command == "test-api":
            results = asyncio.run(run_api_tests(config))
            if not all(result.passed for result in results):
                parser.exit(1)
        elif args.command == 'test-schedule':
            if not asyncio.run(preview_schedule(config, args.at)):
                parser.exit(1)
        else:
            asyncio.run(serve(config))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"配置或启动失败：{exc}\n")
    except KeyboardInterrupt:
        if args.command in ("test-api", "test-schedule"):
            parser.exit(130, "测试已中断。\n")


if __name__ == "__main__":
    main()
