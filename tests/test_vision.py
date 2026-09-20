import asyncio
import base64
from datetime import timedelta
from io import BytesIO, StringIO
import json
from pathlib import Path
import socket
import tempfile
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from atri_bot.api_test import run_vision_test
from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ChatModel
from atri_bot.storage import read_jsonl
from atri_bot.tools import ToolError, ToolSpec, ToolResult, ToolsConfig
from atri_bot.types import Event, Receipt, image_references
from atri_bot.vision import ImageAccess, VisionConfig, normalize_image
from atri_bot.willingness import ReplyConfig
from test_bot import ROOT, raw, daytime


def png(size=(128, 128), color='red'):
    buffer = BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


def picture_event(mid=1, gid=1, text='这张图片里写了什么？', mention=True, url='http://gchat.qpic.cn/pic?secret=private-link'):
    data = raw(mid=mid, gid=gid, text=text, mention=mention)
    data['message'].append({'type': 'image', 'data': {'url': url, 'file': 'onebot-file-hash'}})
    return Event.parse(data)


class ImageTests(unittest.TestCase):
    def test_decode_resize_strip_metadata_and_reject_invalid_files(self):
        config = VisionConfig()
        data, metadata = normalize_image(png((4096, 64)), config)
        self.assertEqual((metadata['width'], metadata['height']), (2048, 32))
        self.assertTrue(metadata['resized'])
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
        for body in (b'not an image', b'<svg></svg>'):
            with self.assertRaises(ToolError):
                normalize_image(body, config)
        config.max_pixels = 1024
        with self.assertRaises(ToolError) as error:
            normalize_image(png((40, 40)), config)
        self.assertEqual(error.exception.code, 'image_too_large')
        config.max_image_bytes = 10
        with self.assertRaises(ToolError):
            normalize_image(png(), config)

    def test_gif_uses_first_frame_and_alpha_is_composited_on_white(self):
        buffer = BytesIO()
        Image.new('RGB', (50, 50), 'red').save(buffer, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (50, 50), 'blue')], duration=100, loop=0)
        data, metadata = normalize_image(buffer.getvalue(), VisionConfig())
        self.assertTrue(metadata['first_frame_only'])
        with Image.open(BytesIO(data)) as image:
            r, g, b = image.getpixel((25, 25))
            self.assertGreater(r, 240)
            self.assertLess(b, 10)
        buffer = BytesIO()
        Image.new('RGBA', (20, 20), (0, 0, 0, 0)).save(buffer, format='PNG')
        data, _ = normalize_image(buffer.getvalue(), VisionConfig())
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.getpixel((10, 10)), (255, 255, 255))

    def test_image_scope_uses_group_and_original_time_and_hides_urls(self):
        event = picture_event()
        now = daytime().timestamp()
        rows = []
        for mid, group, age in ((2, 1, 3600), (3, 1, 3601), (4, 2, 100), (5, 1, -1)):
            rows.append({'kind': 'incoming', 'key': f'99:{group}:{mid}', 'time': now,
                         'timestamp': now - age, 'message_id': str(mid), 'parts': event.parts})
        access = ImageAccess(VisionConfig(), None, event, rows, now=now, history_seconds=3600)
        self.assertEqual(set(access.sources), {'img_1_1', 'img_2_1'})
        refs = image_references(event.parts, event.message_id)
        self.assertEqual(refs, [{'image_id': 'img_1_1', 'position': 1}])
        self.assertNotIn('private-link', str(refs))

    def test_urls_cannot_read_local_files_or_unconfigured_hosts(self):
        access = ImageAccess(VisionConfig(), None, picture_event(), [], now=0, history_seconds=3600)
        for url in ('file:///etc/passwd', '/tmp/image.png', 'base64://AAAA', 'http://127.0.0.1/a',
                    'https://gchat.qpic.cn.evil.invalid/a', 'https://name:password@gchat.qpic.cn/a',
                    'https://gchat.qpic.cn:9999/a', 'https://gchat.qpic.cn/a#fragment', None):
            with self.subTest(url=url), self.assertRaises(ToolError):
                access._allowed_url(url)
        self.assertEqual(access._allowed_url('https://gchat.qpic.cn/image')[1], 'gchat.qpic.cn')

    def test_vision_config_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'
            path.write_text('')
            self.assertFalse(Config.load(path).vision.enabled)
            path.write_text('[vision]\nenabled=true\nmodel="vision-model"\ntimeout=40\n')
            self.assertEqual(Config.load(path).vision.model, 'vision-model')
            for field in ('enabled="yes"', 'model=3', 'timeout=0', 'timeout=nan', 'download_timeout=true',
                          'max_pixels=0', 'max_image_bytes=true', 'max_output_tokens=3000',
                          'allowed_hosts=["*"]', 'allowed_hosts=[]', 'typo=1'):
                path.write_text('[vision]\n' + field)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    Config.load(path)
            path.write_text('[tools]\nenabled=false\n[vision]\nenabled=true\n')
            with self.assertRaisesRegex(ValueError, 'requires tools'):
                Config.load(path)


class VisionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = daytime()
        self.requests, self.downloads, self.sent = [], [], []
        self.picture = png()
        self.picture_status = 200
        self.picture_delay = 0
        self.on_download = self.on_vision = None
        self.vision_status = 200
        self.judgment_score = 90
        self.synthetic = False

        async def image_endpoint(request):
            self.assertIsNone(request.headers.get('Authorization'))
            self.downloads.append(request.path)
            if self.on_download:
                self.on_download()
            if self.picture_delay:
                await asyncio.sleep(self.picture_delay)
            return web.Response(body=self.picture, status=self.picture_status,
                                headers={'Location': 'http://unconfigured.invalid/pic'})

        async def provider(request):
            self.assertEqual(request.headers.get('Authorization'), 'Bearer fake-vision-key')
            payload = await request.json()
            self.requests.append(payload)
            blocks = [b for m in payload['messages'] if isinstance(m.get('content'), list) for b in m['content']]
            if any(b.get('type') == 'image_url' for b in blocks):
                if self.on_vision:
                    self.on_vision()
                self.assertEqual(payload['model'], 'vision-model')
                self.assertNotIn('tools', payload)
                url = next(b['image_url']['url'] for b in blocks if b.get('type') == 'image_url')
                self.assertTrue(url.startswith('data:image/jpeg;base64,'))
                answer = '画面中有红色方块。'
                if self.synthetic:
                    with Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
                        palette = {'red': (255, 0, 0), 'blue': (0, 0, 255), 'green': (0, 180, 0), 'yellow': (255, 255, 0)}
                        colors = []
                        for point in ((100, 100), (400, 100)):
                            pixel = image.getpixel(point)
                            colors.append(min(palette, key=lambda name: sum((a-b)**2 for a, b in zip(pixel, palette[name]))))
                        answer = ','.join(colors)
                return web.json_response({'choices': [{'message': {'content': answer}}]}, status=self.vision_status)
            if '群聊参与判断器' in payload['messages'][0]['content']:
                return web.json_response({'choices': [{'message': {'content': json.dumps({
                    'score': self.judgment_score, 'reason': '判断是否有交流意图'})}}]})
            if payload['messages'][-1]['role'] == 'tool':
                tool_result = json.loads(payload['messages'][-1]['content'])
                answer = '我看到了红色方块。' if tool_result['ok'] else '这张图暂时没能读取。'
                return web.json_response({'choices': [{'message': {'content': answer}}]})
            refs = []
            for message in payload['messages']:
                if message['role'] == 'user':
                    refs.extend(json.loads(message['content']).get('images', []))
            if refs:
                return web.json_response({'choices': [{'message': {'content': None, 'tool_calls': [{
                    'id': 'see_1', 'type': 'function', 'function': {'name': 'inspect_image',
                    'arguments': json.dumps({'image_id': refs[-1]['image_id'], 'question': '描述画面'})}}]}}]})
            return web.json_response({'choices': [{'message': {'content': '普通聊天回复'}}]})

        app = web.Application()
        app.router.add_get('/pic', image_endpoint)
        app.router.add_post('/v1/chat/completions', provider)
        self.server = TestServer(app)
        await self.server.start_server()
        server_port = self.server.port
        class LocalImageResolver(aiohttp.abc.AbstractResolver):
            async def resolve(self, host, port=0, family=socket.AF_INET):
                if host != 'gchat.qpic.cn':
                    raise OSError('Test attempted an external connection')
                return [{'hostname': host, 'host': '127.0.0.1', 'port': server_port,
                         'family': socket.AF_INET, 'proto': 0, 'flags': socket.AI_NUMERICHOST}]
            async def close(self):
                pass
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=LocalImageResolver()))
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({'1', '2'}), self_id='99',
            api_key='fake-vision-key', base_url=str(self.server.make_url('/v1')), model='reply-model',
            reply=ReplyConfig(mode='at_only'), vision=VisionConfig(enabled=True, model='vision-model'))
        self.model = ChatModel(self.config, self.session)
        self.bot = Bot(self.config, self.model, now=lambda: self.now)

    async def asyncTearDown(self):
        await self.bot.close(timeout=.1)
        await self.session.close()
        await self.server.close()
        self.tmp.cleanup()

    async def sender(self, gid, parts):
        self.sent.append(parts[0]['data']['text'])
        return Receipt('sent', str(500 + len(self.sent)))

    async def test_complete_picture_flow_and_followup_keep_urls_and_bytes_out_of_model_history(self):
        result = await self.bot.enqueue(picture_event(), self.sender)
        self.assertEqual(result.status, 'sent')
        self.assertEqual(self.sent, ['我看到了红色方块)'])
        self.assertEqual([r['model'] for r in self.requests], ['reply-model', 'vision-model', 'reply-model'])
        self.assertEqual(self.downloads, ['/pic'])
        first = json.loads(self.requests[0]['messages'][-1]['content'])
        self.assertEqual(first['images'][0]['image_id'], 'img_1_1')
        self.assertNotIn('private-link', str(self.requests))
        result = json.loads(self.requests[-1]['messages'][-1]['content'])
        self.assertEqual(result['data']['observation'], '画面中有红色方块。')
        records = list(read_jsonl(self.bot.group('1').path))
        self.assertEqual([r['tool'] for r in records if r['kind'] == 'tool'], ['inspect_image'])
        self.assertNotIn('base64', str(records))
        self.assertNotIn('画面中有红色方块。', str(self.bot.group('1').history))
        # The next message can refer to the previous picture within the history window.
        await self.bot.enqueue(Event.parse(raw(mid=2, text='再看看刚才那张图')), self.sender)
        self.assertEqual(len(self.downloads), 2)  # Per-reply cache, not a persistent download cache.

    async def test_silent_or_declined_pictures_never_download_or_reach_vision_model(self):
        self.assertEqual((await self.bot.enqueue(picture_event(mention=False, text=''), self.sender)).status, 'ignored')
        self.assertEqual(self.requests, [])
        self.config.reply.mode = 'willingness'
        self.judgment_score = 10
        result = await self.bot.enqueue(picture_event(mid=2, text=''), self.sender)
        self.assertEqual(result.reason, 'model_wait')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('tools', self.requests[0])
        self.assertIn('images', json.loads(self.requests[0]['messages'][1]['content'])['current_message'])
        self.assertEqual(self.downloads, [])

    async def test_at_picture_can_pass_judgment_without_exposing_image_to_judgment_model(self):
        self.config.reply.mode = 'willingness'
        result = await self.bot.enqueue(picture_event(text=''), self.sender)
        self.assertEqual(result.status, 'sent')
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn('base64', str(self.requests[0]))
        self.assertIn('尚未看见图片内容', self.requests[0]['messages'][0]['content'])

    async def test_download_errors_redirects_invalid_files_and_model_failure_are_tool_errors(self):
        cases = [(403, png(), 200, 'image_download_failed'), (302, png(), 200, 'image_download_failed'),
                 (200, b'not-image', 200, 'invalid_image'), (200, png(), 503, 'vision_model_failed')]
        for mid, (status, body, model_status, code) in enumerate(cases, 1):
            self.picture_status, self.picture, self.vision_status = status, body, model_status
            await self.bot.enqueue(picture_event(mid=mid), self.sender)
            result = json.loads(self.requests[-1]['messages'][-1]['content'])
            self.assertEqual(result['error']['code'], code)
            self.assertEqual(self.sent[-1], '这张图暂时没能读取)')
        self.assertTrue(all(path == '/pic' for path in self.downloads))

    async def test_download_timeout_and_byte_cap_are_enforced(self):
        self.picture_delay = .05
        self.config.vision.download_timeout = .01
        await self.bot.enqueue(picture_event(), self.sender)
        error = json.loads(self.requests[-1]['messages'][-1]['content'])['error']['code']
        self.assertEqual(error, 'image_download_timeout')
        self.picture_delay = 0
        self.picture = png() + b'x' * 3000
        self.config.vision.max_image_bytes = 1024
        await self.bot.enqueue(picture_event(mid=2), self.sender)
        error = json.loads(self.requests[-1]['messages'][-1]['content'])['error']['code']
        self.assertEqual(error, 'image_too_large')

    async def test_image_download_is_reused_for_different_questions_within_one_reply(self):
        event = picture_event()
        access = ImageAccess(self.config.vision, self.model, event, [], now=self.now.timestamp(), history_seconds=3600)
        await access.inspect('img_1_1', '颜色')
        await access.inspect('img_1_1', '形状')
        self.assertEqual(self.downloads, ['/pic'])
        self.assertEqual(len(self.requests), 2)
        with self.assertRaises(ToolError) as error:
            await access.inspect('img_999_1', '看看')
        self.assertEqual(error.exception.code, 'image_not_available')

    async def test_midnight_at_receipt_download_or_vision_response_stops_work(self):
        self.now = daytime().replace(hour=0, minute=0)
        result = await self.bot.enqueue(picture_event(), self.sender)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.downloads, [])
        def midnight():
            self.now += timedelta(seconds=1)
        self.now = daytime().replace(hour=23, minute=59, second=59)
        self.on_download = midnight
        result = await self.bot.enqueue(picture_event(mid=2), self.sender)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(len(self.requests), 1)  # Reply requested tool; vision HTTP was never submitted.
        self.on_download = None
        self.now = daytime().replace(hour=23, minute=59, second=59)
        self.on_vision = midnight
        result = await self.bot.enqueue(picture_event(mid=3), self.sender)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(len(self.requests), 3)  # No final reply HTTP after vision returns.
        self.assertEqual(self.sent, [])

    async def test_synthetic_vision_diagnostic_calls_real_client_once_without_group_storage(self):
        self.synthetic = True
        stream = StringIO()
        result = await run_vision_test(self.config, stream=stream)
        self.assertTrue(result.passed, stream.getvalue())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.downloads, [])
        self.assertFalse(self.bot.groups)
        self.assertNotIn('fake-vision-key', stream.getvalue())

    async def test_per_tool_timeout_can_override_default_without_lifting_other_limits(self):
        async def slow(ctx, args):
            await asyncio.sleep(.02)
            return ToolResult(True, {'finished': True})
        registry = self.bot.tool_registry
        registry.register(ToolSpec('test_timeout', '测试', {'type': 'object', 'additionalProperties': False}, slow, timeout=.1))
        from atri_bot.tools import ToolContext
        context = ToolContext('1', '2', '99', '99:1:1', self.now.timestamp(), None, lambda: None, lambda row: None)
        result = await registry.execute('test_timeout', '{}', context, ToolsConfig(timeout=.001))
        self.assertTrue(result.ok)
