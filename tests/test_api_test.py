import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from atri_bot.api_test import run_api_tests
from atri_bot.config import Config


class ApiTestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'personal_info.txt').write_text('你是亚托莉，喜欢帮助别人。')
        self.requests = []
        self.contents = ['OK', '{"score":90,"reason":"直接打招呼"}',
                         '{"score":0,"reason":"要求保持安静"}', '你好，我在呢！']
        self.statuses = [200] * 4
        self.delays = [0] * 4

        async def provider(request):
            self.assertEqual(request.headers.get('Authorization'), 'Bearer fake-api-secret')
            payload = await request.json()
            self.requests.append(payload)
            if payload['model'] == 'judge-model':
                current = json.loads(payload['messages'][1]['content'])['current_message']['text']
                index = 2 if '保持安静' in current else 1
            else:
                index = 0 if '接口连接测试' in payload['messages'][-1]['content'] else 3
            if self.delays[index]:
                await asyncio.sleep(self.delays[index])
            return web.json_response({'choices': [{'message': {'content': self.contents[index]}}]},
                                     status=self.statuses[index])

        app = web.Application()
        app.router.add_post('/v1/chat/completions', provider)
        self.server = TestServer(app)
        await self.server.start_server()
        self.path = self.root / 'config.toml'
        self.path.write_text(
            '[llm]\nbase_url=' + json.dumps(str(self.server.make_url('/v1'))) + '\n'
            'model="reply-model"\napi_key="fake-api-secret"\nthinking="disabled"\n'
            'output_limit_field="max_completion_tokens"\n'
            '[reply]\njudgment_model="judge-model"\n'
            '[logging]\nlevel="ERROR"\nfile=""\ncolor="never"\n')
        self.config = Config.load(self.path)

    async def asyncTearDown(self):
        await self.server.close()
        self.tmp.cleanup()

    async def cli(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'atri_bot.cli', '--config', str(self.path), 'test-api',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return process.returncode, stdout.decode(), stderr.decode()

    async def test_cli_runs_real_client_and_context_without_qq_or_chat_storage(self):
        code, stdout, stderr = await self.cli()
        self.assertEqual(code, 0, stderr)
        self.assertIn('4/4 通过', stdout)
        self.assertIn('score=90', stdout)
        self.assertIn('score=0', stdout)
        self.assertIn('你好，我在呢！', stdout)
        self.assertNotIn('fake-api-secret', stdout + stderr)
        self.assertRegex(stdout, r'\d+\.\d{2} 秒')
        self.assertFalse(self.config.data.exists())
        self.assertEqual([r['model'] for r in self.requests],
                         ['reply-model', 'judge-model', 'judge-model', 'reply-model'])
        self.assertEqual([r['max_completion_tokens'] for r in self.requests], [512, 256, 256, 512])
        self.assertTrue(all(r['thinking'] == {'type': 'disabled'} for r in self.requests))
        for request in self.requests[1:3]:
            data = json.loads(request['messages'][1]['content'])
            self.assertEqual(self.config.read_personal_info(), data['persona_reference'])
        self.assertIn(self.config.read_personal_info(), self.requests[3]['messages'][0]['content'])
        self.assertIn('群聊参与判断器', self.requests[1]['messages'][0]['content'])

    async def test_failures_are_distinguished_and_remaining_cases_still_run(self):
        self.statuses[0] = 401
        self.contents[0] = 'fake-api-secret'
        self.contents[1] = '这不是 JSON'
        self.contents[2] = '{"score":90,"reason":"错误地选择接话"}'
        stream = io.StringIO()
        results = await run_api_tests(self.config, stream=stream)
        self.assertEqual([r.error for r in results],
                         ['model_http_401', 'invalid_reply_assessment', 'unexpected_response', ''])
        self.assertEqual([r.passed for r in results], [False, False, False, True])
        self.assertEqual(len(self.requests), 6)
        self.assertIn('1/4 通过', stream.getvalue())
        self.assertIn('接口已返回结果，但未符合测试预期', stream.getvalue())
        self.assertNotIn('fake-api-secret', stream.getvalue())

    async def test_timeout_is_reported_and_does_not_abort_later_cases(self):
        self.config.llm_timeout = .05
        self.delays[0] = .15
        results = await run_api_tests(self.config, stream=io.StringIO())
        self.assertEqual(results[0].error, 'model_timeout')
        self.assertGreaterEqual(results[0].elapsed_seconds, .04)
        self.assertTrue(all(r.passed for r in results[1:]))

    async def test_cli_returns_failure_for_unexpected_response_and_config_error(self):
        self.contents[0] = '收到'
        code, stdout, stderr = await self.cli()
        self.assertEqual(code, 1, stderr)
        self.assertIn('unexpected_response', stdout)
        self.assertIn('3/4 通过', stdout)
        self.path.write_text(self.path.read_text().replace('api_key="fake-api-secret"', 'api_key=""'))
        code, stdout, stderr = await self.cli()
        self.assertEqual(code, 2)
        self.assertIn('llm.api_key', stderr)
        self.assertEqual(len(self.requests), 4)

    async def test_summary_redacts_credentials_and_respects_hidden_previews(self):
        self.contents[3] = 'fake-api-secret\n\x1b[31m正文'
        stream = io.StringIO()
        await run_api_tests(self.config, stream=stream)
        output = stream.getvalue()
        self.assertNotIn('fake-api-secret', output)
        self.assertNotIn('\x1b', output)
        self.assertIn('[REDACTED]', output)
        self.requests.clear()
        self.config.logging.preview_chars = 0
        stream = io.StringIO()
        await run_api_tests(self.config, stream=stream)
        self.assertNotIn('正文"', stream.getvalue())
        self.assertIn('正文隐藏', stream.getvalue())
