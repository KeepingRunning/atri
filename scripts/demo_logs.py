"""用本地模拟模型和 OneBot 展示日志，不读取真实配置或连接 QQ。"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import tempfile

import aiohttp
from aiohttp import web

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.logging_setup import LoggingConfig, configure_logging, log_context
from atri_bot.model import ChatModel
from atri_bot.onebot import create_app
from atri_bot.willingness import ReplyConfig

ROOT = Path(__file__).resolve().parents[1]


async def demo(color):
    with tempfile.TemporaryDirectory(prefix='atri-log-demo-') as directory:
        config = Config(ROOT, Path(directory), groups=frozenset({'1'}), self_id='99',
                        token='demo-onebot', api_key='demo-api', model='demo-reply',
                        reply=ReplyConfig(judgment_model='demo-judge'),
                        logging=LoggingConfig(color=color, file=''))
        configure_logging(config.logging, ROOT, secrets=(config.token, config.api_key))
        core = logging.getLogger('atri.core')
        core.info('[演示] 使用本地模拟群和模型，临时数据会自动清理')

        async def provider(request):
            payload = await request.json()
            await asyncio.sleep(.02)
            if payload['model'] == 'demo-judge':
                waiting = '不用回复' in payload['messages'][-1]['content']
                text = json.dumps({'score': 10 if waiting else 88,
                                   'reason': '对方要求安静' if waiting else '对方在寻求帮助，可以参与'}, ensure_ascii=False)
            else:
                text = '把报错那几行贴出来，我看看是哪里出了问题。'
            return web.json_response({'choices': [{'message': {'content': text}}],
                                      'usage': {'prompt_tokens': 180, 'completion_tokens': 24, 'total_tokens': 204}})

        async with aiohttp.ClientSession() as session:
            bot = Bot(config, ChatModel(config, session))
            app = create_app(config, bot)
            app.router.add_post('/v1/chat/completions', provider)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, '127.0.0.1', 0).start()
                base = f'http://127.0.0.1:{runner.addresses[0][1]}'
                config.base_url = base + '/v1'
                async with session.ws_connect(base + config.ws_path, headers={
                    'Authorization': 'Bearer ' + config.token, 'X-Self-ID': config.self_id}) as ws:
                    async def acknowledge():
                        count = 0
                        async for frame in ws:
                            if frame.type == aiohttp.WSMsgType.TEXT:
                                action = json.loads(frame.data)
                                count += 1
                                await ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0,
                                                    'data': {'message_id': 500 + count}})
                    reader = asyncio.create_task(acknowledge())
                    try:
                        for mid, text, at in ((1, '谁能帮我看看这个报错怎么解决？', False),
                                              (2, '另外这个问题怎么处理？', False),
                                              (3, '这条不用回复，我先自己试试', True),
                                              (4, '哈哈哈', False)):
                            parts = [{'type': 'text', 'data': {'text': text}}]
                            if at:
                                parts.insert(0, {'type': 'at', 'data': {'qq': '99'}})
                            await ws.send_json({'post_type': 'message', 'message_type': 'group',
                                                'group_id': 1, 'self_id': 99, 'user_id': 2 if at or mid == 1 else 3,
                                                'message_id': mid, 'sender': {'nickname': '演示群友'}, 'message': parts})
                            async with asyncio.timeout(5):
                                while '1' not in bot.groups or f'99:1:{mid}' not in bot.groups['1'].seen:
                                    await asyncio.sleep(.001)
                                await bot.queues['1'].join()
                        with log_context(group_id='1', message_id='demo', user_id='2'):
                            logging.getLogger('atri.plugins.weather').info('[插件示例] 天气插件使用独立颜色和分类')
                            logging.getLogger('atri.plugins.memory').info('[插件示例] 记忆插件使用独立颜色和分类')
                    finally:
                        await ws.close()
                        await reader
            finally:
                await runner.cleanup()
        core.info('[演示结束] 包含接话、冷却、模型等待、短反应过滤及插件配色')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='always')
    asyncio.run(demo(parser.parse_args().color))
