"""AI连接本地验证；使用模拟响应，禁止真实付费调用。"""
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from flask import Flask
from ecommerce.infrastructure.ai.config import AISettings, load_config
from ecommerce.infrastructure.ai.client import AIError, create_client
from ecommerce.shared.config import load_settings
from ecommerce.web.routes.ai import ai, ConnectionCheck


class AITests(unittest.TestCase):
    def setUp(self):
        """创建隔离应用；参数：无；返回：无。"""
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = load_settings(root=self.root)
        self.app = Flask('ai-test', template_folder=str(Path(__file__).resolve().parents[2] / 'src/ecommerce/web/templates'))
        self.app.config.update(SETTINGS=self.settings, COLLECTION_TOKEN='test-token')
        self.manager = ConnectionCheck()
        self.addCleanup(self.manager.pool.shutdown, wait=True)
        self.app.extensions['ai_check'] = self.manager
        self.app.register_blueprint(ai)
        self.client = self.app.test_client()
        self.headers = {'X-Collection-Token': 'test-token'}

    def test_configuration_no_secret_and_invalid_config(self):
        """状态不泄密、不联网；拒绝密钥写入TOML及未知配置。"""
        from ecommerce.modules.ai_service import configuration
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'private-test-secret'}):
            value = configuration(self.settings)
            self.assertTrue(value['key_configured'])
            self.assertNotIn('private-test-secret', json.dumps(value))
        (self.root / 'config').mkdir()
        for content in ('[ai]\nunknown_key="secret"', '[ai]\napi_key=123', '[ai]\ntimeout_seconds=true', '[ai]\nmodels="abc"', '[ai]\nmodel="unknown"'):
            (self.root / 'config/ai.toml').write_text(content)
            with self.assertRaises(ValueError): load_config(self.root)

    def test_local_key_priority_refresh_and_no_leak(self):
        """本机密钥优先、动态重读、空白回退；对象和公开状态均不泄密。"""
        from ecommerce.infrastructure.ai.config import api_key
        from ecommerce.modules.ai_service import configuration
        (self.root / 'config').mkdir()
        path = self.root / 'config/ai.toml'
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'environment-secret'}):
            for key in ('local-secret-one', 'local-secret-two', ''):
                path.write_text('[ai]\napi_key="' + key + '"', encoding='utf-8')
                config = load_config(self.root)
                self.assertEqual(api_key(config), key or 'environment-secret')
                self.assertNotIn('local-secret', repr(config))
                self.assertNotIn('local-secret', json.dumps(configuration(self.settings)))
            path.write_text('[ai]\napi_key="local-secret"', encoding='utf-8')
            with patch('ecommerce.infrastructure.ai.client.build_opener') as factory:
                response = factory.return_value.open.return_value.__enter__.return_value
                response.read.return_value = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
                create_client(load_config(self.root)).complete('test')
                self.assertEqual(factory.return_value.open.call_args.args[0].get_header('Authorization'),'Bearer local-secret')

    def test_invalid_config_page_never_leaks_secret(self):
        """错填密钥或损坏TOML时返回可读页面，不500、不泄露配置、不联网。"""
        from ecommerce.web.app import create_app
        app = create_app(self.settings)
        self.addCleanup(app.extensions['ai_check'].pool.shutdown, wait=True)
        client = app.test_client()
        (self.root / 'config').mkdir()
        path = self.root / 'config/ai.toml'
        for content in ('[ai]\napi_key_env="sk-private-secret"', '[ai]\napi_key="sk-private-secret'):
            path.write_text(content, encoding='utf-8')
            with patch('ecommerce.infrastructure.ai.client.build_opener') as network:
                response = client.get('/settings/ai')
                self.assertEqual(response.status_code,200)
                text=response.get_data(as_text=True)
                self.assertIn('AI配置需要修正',text)
                self.assertNotIn('sk-private-secret',text)
                self.assertNotIn('id="ai-check"',text)
                network.assert_not_called()

    def test_client_contract_and_payload(self):
        """限制官方地址、非思考短输出，返回用量和完整结果。"""
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret'}), patch('ecommerce.infrastructure.ai.client.build_opener') as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'choices':[{'message':{'content':'连接正常'}, 'finish_reason':'stop'}],
                                                   'usage':{'total_tokens':9,'extra':'secret'}}).encode()
            result = create_client(AISettings()).complete('测试', max_tokens=32)
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(json.loads(request.data)['thinking'], {'type':'disabled'})
            self.assertEqual(result['usage'], {'total_tokens':9})
            self.assertEqual(factory.return_value.open.call_count, 1)
            for body in (b'not-json', b'{}', b'{"choices":[{"message":{"content":"a"},"finish_reason":"length"}]}'):
                response.read.return_value = body
                with self.assertRaises(AIError): create_client(AISettings()).complete('测试')

    def test_errors_safe_no_retry(self):
        """错误不包含服务商原始响应和密钥，不重试；缺密钥不联网。"""
        with patch.dict(os.environ, {}, clear=True), patch('ecommerce.infrastructure.ai.client.build_opener') as factory:
            with self.assertRaises(AIError): create_client(AISettings()).complete('测试')
            factory.assert_not_called()
        for code in (401,402,403,422,429,500,302):
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'private-test-secret'}), patch('ecommerce.infrastructure.ai.client.build_opener') as factory:
                factory.return_value.open.side_effect = HTTPError('hidden',code,'private-test-secret',{},io.BytesIO(b'private-test-secret'))
                with self.assertRaises(AIError) as caught: create_client(AISettings()).complete('测试')
                self.assertNotIn('private-test-secret', str(caught.exception))
                self.assertEqual(factory.return_value.open.call_count,1)

    def test_web_authorization_validation_and_background(self):
        """GET无调用，POST需令牌、同源、计费勾选和合法模型；轮询返回摘要。"""
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'secret'}), patch('ecommerce.web.routes.ai.check_connection', return_value={'status':'succeeded','message':'完成'}) as check:
            self.assertEqual(self.client.get('/api/ai/check').json['status'], 'idle')
            check.assert_not_called()
            data = {'provider':'deepseek','model':'deepseek-flash','allow_paid':True}
            self.assertEqual(self.client.post('/api/ai/check',json=data).status_code,403)
            self.assertEqual(self.client.post('/api/ai/check',json=data,headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
            for changes in ({'allow_paid':False},{'model':'unknown'},{'model':[]},{'provider':'unknown'}):
                self.assertEqual(self.client.post('/api/ai/check',json={**data,**changes},headers=self.headers).status_code,400)
            check.assert_not_called()
            self.assertEqual(self.client.post('/api/ai/check',json=data,headers=self.headers).status_code,202)
            self.manager.future.result(timeout=2)
            self.assertEqual(self.client.get('/api/ai/check').json['status'],'succeeded')
            self.assertEqual(check.call_count,1)
        pending = MagicMock(); pending.done.return_value=False
        self.manager.future=pending
        self.assertFalse(self.manager.start(self.settings,'deepseek','deepseek-flash'))

    def test_cli_paid_guard(self):
        """CLI缺少明确计费开关不触发调用。"""
        from ecommerce.cli.app import main
        with patch('ecommerce.cli.app.load_settings', return_value=self.settings), patch('ecommerce.cli.app.setup_logging'), patch('ecommerce.modules.ai_service.check_connection') as check:
            self.assertEqual(main(['ai','status']),0)
            self.assertEqual(main(['ai','check']),1)
            check.assert_not_called()
