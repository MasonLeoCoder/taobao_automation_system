"""只读关联千牛智能裁剪的单张输入与输出，不主动发起裁剪请求。"""
import json
import re
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import Error as PlaywrightError

_CROP_PATH = '/h5/mtop.taobao.qn.copilot.node.aigc.img.web.batch.sync.change.size/1.0/'


def _crop_body(text):
    """只解析 JSON 或 JSONP 数据，不执行回调文本。"""
    try:
        return json.loads(text)
    except ValueError:
        wrapper = re.fullmatch(r'\s*[\w$]+\s*\((.*)\)\s*;?\s*', text, re.DOTALL)
        try:
            return json.loads(wrapper[1]) if wrapper else None
        except ValueError:
            return None


def _crop_input(url, post_data):
    """解析已观察的单图裁剪请求；参数：地址、表单；返回：原图地址或空值。"""
    location = urlsplit(url)
    if location.hostname != 'h5api.m.taobao.com' or location.path.lower() != _CROP_PATH:
        return None
    try:
        values = parse_qs(post_data or location.query).get('data', [])
        if len(values) != 1:
            return None
        data = json.loads(values[0])
        params = data.get('paramStrList')
        params = json.loads(params) if isinstance(params, str) else params
        if not isinstance(params, list) or len(params) != 1:
            return None
        param = json.loads(params[0]) if isinstance(params[0], str) else params[0]
        if not isinstance(param, dict):
            return None
        source = param.get('imgUrl')
        return source if isinstance(source, str) else None
    except (ValueError, TypeError, AttributeError):
        return None


def _crop_output(body):
    """解析成功且唯一的裁剪结果；参数：接口 JSON；返回：CDN 图片地址或空值。"""
    try:
        status = body.get('ret')
        if not isinstance(status, list) or len(status) != 1 or not str(status[0]).startswith('SUCCESS::'):
            return None
        results = body['data']['result']['taskResults']
        if not isinstance(results, list) or len(results) != 1:
            return None
        pictures = results[0]['jsPictureDOS']
        if not isinstance(pictures, list) or len(pictures) != 1:
            return None
        source = pictures[0]['fullUrl']
        location = urlsplit(source)
        if location.scheme != 'https' or location.hostname != 'img.alicdn.com':
            return None
        return source
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class _CropEvidence:
    """仅收集本次选图开始后、原图匹配的裁剪响应，退出时移除监听。"""

    def __init__(self, page, source, pattern):
        self.page = page
        self.events = page.context
        self.pattern = pattern
        self.original = pattern(source) if source else None
        self.requests = {}
        self.candidates = set()
        self.failures = {}
        self.results = set()

    def __enter__(self):
        self.events.on('request', self._request)
        self.events.on('response', self._response)
        return self

    def __exit__(self, *error):
        self.events.remove_listener('request', self._request)
        self.events.remove_listener('response', self._response)

    def _request(self, request):
        location = urlsplit(request.url)
        if location.hostname != 'h5api.m.taobao.com' or location.path.lower() != _CROP_PATH:
            return
        try:
            source = _crop_input(request.url, request.post_data)
        except (PlaywrightError, UnicodeError):
            return
        if source and (self.original is None or self.original.fullmatch(source)):
            self.requests[request] = source

    def _response(self, response):
        if response.request not in self.requests:
            return
        original = self.requests[response.request]
        try:
            text = response.text() if response.status == 200 else ''
            source = _crop_output(_crop_body(text)) if len(text) <= 1024**2 else None
            if source:
                self.failures.pop(original, None)
                self.candidates.add((original, source))
                if self.original is not None and self.original.fullmatch(original):
                    self.results.add(source)
            else:
                self.failures[original] = (f'智能裁剪接口返回 HTTP {response.status}' if response.status != 200
                                           else '智能裁剪接口未返回成功且唯一的图片结果')
        except (ValueError, RuntimeError, PlaywrightError):
            # 网络中断或不合法回执不能作为裁剪成功的依据。
            self.failures[original] = '智能裁剪回执读取失败，请检查网络或平台提示'

    def bind(self, source):
        """上传得到原图地址后，只保留该原图的回执，包括上传完成时自动触发的裁剪。"""
        self.original = self.pattern(source)
        self.results = {result for original, result in self.candidates if self.original.fullmatch(original)}

    def match(self, source):
        """按已关联回执匹配可见结果；参数：页面地址；返回：唯一结果或空值。"""
        matches = [result for result in self.results if self.pattern(result).fullmatch(source)] if source else []
        return matches[0] if len(matches) == 1 else None

    def problem(self):
        """返回仅与当前原图有关的失败原因，避免引用其他素材的响应。"""
        return next((reason for source, reason in self.failures.items()
                     if self.original is not None and self.original.fullmatch(source)), None)
