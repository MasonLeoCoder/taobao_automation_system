"""流式HTTP下载；仅在来源实体可核验且Range严格一致时追加文件。"""
import hashlib
import json
import re
import shutil
import time
import urllib.error
import urllib.request

from ecommerce.infrastructure.tasks.ownership import atomic_json


def fingerprint(path):
    """计算大小与摘要；参数：路径；返回：字节数和SHA256。"""
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def ensure_space(directory, required, reserve=256 * 1024 ** 2):
    """核对空间；参数：目录、还需字节和保留量；返回：剩余空间，不设置视频大小上限。"""
    free = shutil.disk_usage(directory).free
    if free < max(0, required) + reserve:
        raise ValueError('临时磁盘空间不足，请清理空间或更换临时目录后重新准备')
    return free


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """每次重定向均调用平台来源白名单。"""
    def __init__(self, allowed):
        """保存校验器；参数：地址检查函数；返回：无。"""
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """检查跳转；参数：HTTP上下文；返回：新请求。"""
        if not self.allowed(newurl):
            raise ValueError('资源跳转到了未允许的来源')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(sources, target, *, identity, allowed, checkpoint, progress, referer,
             reserve=256 * 1024 ** 2, max_bytes=None, opener=None, request_headers=None, request_timeout=20):
    """校验后下载或续传；参数：候选源、固定身份和回调；返回：文件大小与摘要。"""
    opener = opener or urllib.request.build_opener(CheckedRedirect(allowed))
    state_path = target.with_suffix(target.suffix + '.json')
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    if state.get('identity') != identity:
        target.unlink(missing_ok=True)
        state = {}
    source_list = list(dict.fromkeys(s for s in sources if allowed(s)))[:3]
    if not source_list:
        raise ValueError('平台没有提供允许下载的资源地址')
    last_error = '资源暂不可用，请重新解析'
    for source in source_list:
        checkpoint()
        offset = target.stat().st_size if target.is_file() else 0
        strong_etag = state.get('etag') if not str(state.get('etag') or '').startswith('W/') else None
        validator = strong_etag or state.get('modified')
        # 相同URL不能证明实体未变；没有HTTP实体校验值时重新下载，弱ETag不用于If-Range。
        resume = bool(offset and validator and state.get('identity') == identity)
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': referer, 'Accept-Encoding': 'identity'}
        if request_headers:
            headers.update({k: v for k, v in request_headers.items() if k.lower() in {'user-agent', 'referer', 'origin', 'accept-language'}})
        if resume:
            headers['Range'] = f'bytes={offset}-'
            if validator:
                headers['If-Range'] = validator
        elif offset:
            target.unlink(missing_ok=True)
            offset = 0
            progress(0, 0, 0, '来源已刷新，不能确认旧分段属于相同文件，正在重新下载')
        try:
            request = urllib.request.Request(source, headers=headers)
            with opener.open(request, timeout=request_timeout) as response:
                if not allowed(response.url):
                    raise ValueError('最终资源地址未通过来源校验')
                status = response.status
                content_range = response.headers.get('Content-Range', '')
                etag = response.headers.get('ETag')
                modified = response.headers.get('Last-Modified')
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                    if not resume or not match or int(match[1]) != offset or int(match[2]) < offset:
                        raise ValueError('服务器返回的续传范围不一致，请重新准备')
                    if validator and validator != (etag if strong_etag else modified):
                        target.unlink(missing_ok=True)
                        raise ValueError('来源文件已变化，已停止续传，请重试')
                    total = int(match[3])
                    if int(match[2]) != total - 1:
                        raise ValueError('服务器没有返回完整的剩余范围')
                elif status == 200:
                    if resume:
                        progress(0, 0, 0, '来源不支持本次续传，正在从头下载')
                    offset = 0
                    total = int(response.headers.get('Content-Length') or 0)
                else:
                    raise ValueError('资源响应状态无效')
                if total and total <= offset:
                    raise ValueError('资源长度与已下载分段冲突')
                if max_bytes and total > max_bytes:
                    raise ValueError('该图片超过允许的大小')
                ensure_space(target.parent, max(0, total - offset), reserve)
                state = {'identity': identity, 'url': source, 'etag': etag, 'modified': modified, 'total': total}
                atomic_json(state_path, state)
                loaded, start_loaded, start, last = offset, offset, time.monotonic(), 0
                with target.open('ab' if offset else 'wb') as output:
                    while True:
                        checkpoint()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        ensure_space(target.parent, len(chunk), reserve)
                        output.write(chunk)
                        loaded += len(chunk)
                        if max_bytes and loaded > max_bytes or total and loaded > total:
                            raise ValueError('资源长度超过预期')
                        now = time.monotonic()
                        if now - last >= .5:
                            progress(loaded, total, (loaded - start_loaded) / max(.01, now - start), '')
                            last = now
                checkpoint()
                if not loaded or total and loaded != total:
                    raise ValueError('文件下载不完整，保留分段以供重试')
                progress(loaded, total, 0, '')
                return fingerprint(target)
        except (OSError, ValueError) as error:
            # 不输出可能携带签名参数的异常URL；停止与暂停异常必须原样向上传播。
            last_error = str(error) if isinstance(error, ValueError) else '网络中断或来源地址已失效，已保留可核验分段'
            if isinstance(error, urllib.error.HTTPError) and error.code == 416:
                target.unlink(missing_ok=True)
            checkpoint()
    raise ValueError(last_error)
