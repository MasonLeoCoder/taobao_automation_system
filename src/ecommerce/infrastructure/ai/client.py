"""AI统一文本客户端及DeepSeek适配；无自动重试、无商品读取和原始响应日志。"""
import json
from ecommerce.infrastructure.ai.config import api_key
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


class AIError(Exception):
    """仅包含可展示信息的调用错误。"""


class TextClient(Protocol):
    """统一文本调用契约，后续服务商实现相同方法。"""
    def complete(self, text: str, *, max_tokens: int = 64) -> dict:
        """提交文字及输出上限；返回服务商、模型、文字、用量与完成原因。"""
        ...


class NoRedirect(HTTPRedirectHandler):
    """拒绝重定向，防止授权头跟随到其他地址。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """拒绝跳转；参数：HTTP重定向上下文；返回：空。"""
        return None


class DeepSeekClient:
    """固定官方地址的DeepSeek文本适配。"""
    def __init__(self, config, model):
        """初始化；参数：校验后的配置及所选模型；返回：无。"""
        self.config, self.model = config, model

    def complete(self, text, *, max_tokens=64):
        """单次发送文本；参数：文字和输出上限；返回脱敏结果或AIError。"""
        key = api_key(self.config)
        if not key:
            raise AIError('尚未配置密钥，请在本机config/ai.toml的[ai]中填写api_key。')
        if not key.isascii() or any(c.isspace() for c in key):
            raise AIError('密钥格式无效，请检查本机AI配置。')
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise AIError('文字须为1～20000个字符。')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise AIError('输出上限须为1～4096。')
        body = json.dumps({'model': self.model, 'messages': [{'role': 'user', 'content': text}],
                           'thinking': {'type': 'disabled'}, 'stream': False, 'max_tokens': max_tokens}).encode()
        request = Request('https://api.deepseek.com/chat/completions', data=body,
                          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect(), HTTPSHandler()).open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise AIError('接口响应超过安全大小限制。')
            data = json.loads(raw)
            choice = data['choices'][0]
            content = choice['message']['content']
            if not isinstance(content, str) or not content.strip() or choice.get('finish_reason') != 'stop':
                raise AIError('模型未完整返回文字，连接检查未通过。')
            usage = data.get('usage', {})
            if not isinstance(usage, dict):
                raise AIError('接口用量格式无效。')
            return {'provider': 'deepseek', 'model': self.model, 'text': content.replace(key, '[已隐藏]'),
                    'finish_reason': 'stop', 'usage': {name: value for name in
                    ('prompt_tokens', 'completion_tokens', 'total_tokens')
                    if type(value := usage.get(name)) is int and value >= 0}}
        except HTTPError as error:
            code = error.code
            error.close()
            messages = {401: '密钥无效或已失效。', 402: '余额不足，请检查账户额度。',
                        403: '没有调用权限。', 404: '接口或模型不存在。',
                        422: '模型或请求参数不受支持。', 429: '调用频率或额度受限，请稍后手动重试。'}
            raise AIError(messages.get(code, 'AI服务请求失败，请稍后检查；未自动重试。')) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise AIError('连接失败或超时，请检查网络；未自动重试。') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError('AI返回格式异常，连接检查未通过。') from None


def create_client(config, *, provider=None, model=None) -> TextClient:
    """统一工厂；参数：配置及可选服务商/模型；返回：对应客户端。"""
    selected = provider or config.provider
    model = model or config.model
    if selected != 'deepseek':
        raise AIError('当前仅接入DeepSeek。')
    if model not in config.models:
        raise AIError('模型不在已配置列表中。')
    return DeepSeekClient(config, model)
