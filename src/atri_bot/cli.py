import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from .bot import Bot
from .config import Config
from .model import ChatModel
from .logging_setup import configure_logging
from .onebot import create_app
from .storage import single_instance


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
    parser.add_argument("command", nargs="?", choices=("serve", "check"), default="serve")
    args = parser.parse_args(argv)
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
        else:
            asyncio.run(serve(config))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"配置或启动失败：{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
