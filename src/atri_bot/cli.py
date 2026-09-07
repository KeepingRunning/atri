import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from .bot import Bot
from .config import Config
from .model import ChatModel
from .onebot import create_app
from .storage import single_instance


async def serve(config):
    config.require_serve()
    with single_instance(config.data):
        async with aiohttp.ClientSession() as session:
            bot = Bot(config, ChatModel(config, session))
            runner = web.AppRunner(create_app(config, bot), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, config.host, config.port).start()
                logging.getLogger("atri").info("ATRI listening on %s:%s%s", config.host, config.port, config.ws_path)
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ATRI QQ chatbot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("command", nargs="?", choices=("serve", "check"), default="serve")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        config = Config.load(args.config)
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
