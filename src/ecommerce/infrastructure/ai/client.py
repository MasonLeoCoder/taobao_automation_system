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
    def complete(self, text: str, *, max_tokens: int = 64, require_complete: bool = True,
                 json_mode: bool = False, timeout_seconds: int | None = None) -> dict:
        """提交文字及输出上限；返回服务商、模型、文字、用量与完成原因。"""
        ...

    def complete_messages(self, messages: list[dict], *, max_tokens: int, capability: dict,
                          timeout_seconds: int = 180) -> dict:
        """提交已确认的真实消息；参数：消息、输出上限、模型能力及超时；返回：文字、用量及完成原因。"""
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

    def complete(self, text, *, max_tokens=64, require_complete=True, json_mode=False, timeout_seconds=None):
        """单次发送文本；参数：文字和输出上限；返回脱敏结果或AIError。"""
        key = api_key(self.config)
        if not key:
            raise AIError('尚未配置密钥，请在本机config/ai.toml的[ai]中填写api_key。')
        if not key.isascii() or any(c.isspace() for c in key):
            raise AIError('密钥格式无效，请检查本机AI配置。')
        if not isinstance(text, str) or not text.strip() or len(text) > 60000:
            raise AIError('文字须为1～60000个字符。')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise AIError('输出上限须为1～65536。')
        timeout = self.config.timeout_seconds if timeout_seconds is None else timeout_seconds
        if type(json_mode) is not bool or type(timeout) is not int or not 1 <= timeout <= 600:
            raise AIError('请求格式或等待上限无效。')
        payload = {'model': self.model, 'messages': [{'role': 'user', 'content': text}],
                   'thinking': {'type': 'disabled'}, 'stream': False, 'max_tokens': max_tokens}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        return self._send(payload,key,timeout,require_complete)

    def complete_messages(self,messages,*,max_tokens,capability,timeout_seconds=180,**options):
        """新流程使用真实消息和模型容量，旧文字接口保持原规则；参数：消息、输出、冻结能力；返回：响应。"""
        key=api_key(self.config)
        if not key or not key.isascii() or any(c.isspace() for c in key):raise AIError('密钥未配置或格式无效。')
        if capability.get('model')!=self.model or capability.get('provider')!='deepseek':raise AIError('模型能力与实际模型不匹配。')
        if type(max_tokens) is not int or not 1<=max_tokens<=capability['max_output_tokens']:raise AIError('输出上限超过当前模型能力。')
        if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or set(m)!={'role','content'} or m['role'] not in ('system','user') or not isinstance(m['content'],str) for m in messages):raise AIError('消息结构无效。')
        if type(timeout_seconds) is not int or not 1<=timeout_seconds<=600:raise AIError('等待上限无效。')
        payload={'model':self.model,'messages':messages,'thinking':{'type':'disabled'},'stream':False,'max_tokens':max_tokens}
        if len(json.dumps(payload,ensure_ascii=False).encode())>16*1024*1024:raise AIError('请求超过本机16MiB网络保护，未发送；这不是模型Token限制。')
        return self._send(payload,key,timeout_seconds,False)

    def complete_images(self, messages, *, max_tokens, capability, timeout_seconds=180):
        """发送显式视觉消息；原文本入口不变，禁远程URL、重试和重定向。"""
        import base64
        key = api_key(self.config)
        if not key or not key.isascii() or any(c.isspace() for c in key): raise AIError('密钥未配置或格式无效。')
        if self.model != 'deepseek-flash' or capability.get('model') != self.model or capability.get('provider') != 'deepseek' or capability.get('vision') is not True:
            raise AIError('当前模型未获准执行图片分析。')
        if type(max_tokens) is not int or not 1 <= max_tokens <= capability['max_output_tokens']: raise AIError('输出上限无效。')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600: raise AIError('等待上限无效。')
        if not isinstance(messages, list) or not messages: raise AIError('图片消息无效。')
        count = 0
        for message in messages:
            if not isinstance(message, dict) or set(message) != {'role','content'} or message['role'] not in ('system','user'): raise AIError('图片消息结构无效。')
            content = message['content']
            if isinstance(content,str): continue
            if message['role'] != 'user' or not isinstance(content,list) or not content: raise AIError('图片只能放在用户消息中。')
            for part in content:
                if not isinstance(part,dict): raise AIError('图片内容无效。')
                if set(part)=={'type','text'} and part['type']=='text' and isinstance(part['text'],str): continue
                if set(part)!={'type','image_url'} or part['type']!='image_url' or not isinstance(part['image_url'],dict): raise AIError('图片内容结构无效。')
                image = part['image_url']; uri=image.get('url')
                if set(image)!={'url','detail'} or image['detail']!='original' or not isinstance(uri,str) or not uri.startswith('data:image/png;base64,'): raise AIError('仅允许已预检本地PNG内联图片。')
                encoded=uri.split(',',1)[1]
                if len(encoded)>4*((32*1024*1024+2)//3): raise AIError('单图超过32MiB。')
                try: raw=base64.b64decode(encoded,validate=True)
                except ValueError: raise AIError('图片编码无效。') from None
                if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw)>32*1024*1024: raise AIError('图片格式或大小无效。')
                count+=1
        if count>600: raise AIError('单请求图片数超过600张。')
        payload=dict(model=self.model,messages=messages,thinking={'type':'disabled'},stream=False,max_tokens=max_tokens)
        if len(json.dumps(payload,ensure_ascii=False).encode())>48*1024*1024: raise AIError('图片请求超过48MiB，未发送。')
        return self._send(payload,key,timeout_seconds,False)

    def _send(self,payload,key,timeout,require_complete):
        """统一单次HTTP发送，无重试；参数：请求体、认证、超时；返回：脱敏响应。"""
        body = json.dumps(payload,ensure_ascii=False).encode()
        request = Request('https://api.deepseek.com/chat/completions', data=body,
                          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect(), HTTPSHandler()).open(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise AIError('接口响应超过安全大小限制。')
            data = json.loads(raw)
            choice = data['choices'][0]
            content = choice['message']['content']
            if not isinstance(content, str) or (require_complete and (not content.strip() or choice.get('finish_reason') != 'stop')):
                raise AIError('模型未完整返回文字，连接检查未通过。')
            usage = data.get('usage', {})
            if not isinstance(usage, dict):
                raise AIError('接口用量格式无效。')
            return {'provider': 'deepseek', 'model': self.model, 'text': content.replace(key, '[已隐藏]'),
                    'finish_reason': choice.get('finish_reason'),
                    'response_id': str(data.get('id', ''))[:200], 'returned_model': str(data.get('model', self.model))[:100],
                    'usage': {name: value for name in
                    ('prompt_tokens', 'completion_tokens', 'total_tokens')
                    if type(value := usage.get(name)) is int and value >= 0}}
        except HTTPError as error:
            code = error.code
            error.close()
            messages = {400:'请求参数错误，请核对模型与发送设置。',401: '密钥无效或已失效。', 402: '余额不足，请检查账户额度。',
                        403: '没有调用权限。', 404: '接口或模型不存在。',
                        422: '模型或请求参数不受支持。', 429: '调用频率或额度受限，请稍后手动重试。',
                        500:'AI服务内部异常。',503:'AI服务暂时不可用。'}
            raise AIError(f'DeepSeek返回HTTP {code}：'+messages.get(code, 'AI服务请求失败，请稍后检查；未自动重试。')) from None
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            reason=getattr(error,'reason',error)
            message='连接或等待响应超时，请检查网络或稍后手动重试。' if isinstance(reason,(TimeoutError,socket.timeout)) else '网络连接失败，请检查网络和代理设置。'
            raise AIError(message+' 未自动重试。') from None
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
