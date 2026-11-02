"""通过官方yt-dlp发行包读取YouTube当前视频；登录资料与签名源只保留本地。"""
import hashlib
import http.cookiejar
import json
import re
import shutil
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

from ecommerce.infrastructure.browser.session import browser_session, save_session
from ecommerce.infrastructure.media.transfer import CheckedRedirect
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.time import BEIJING


def media_url(url):
    """限制YouTube媒体/封面来源；参数：URL；返回：是否属于允许的HTTPS CDN。"""
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
                and any((p.hostname or '').endswith('.' + d) for d in ('googlevideo.com', 'ytimg.com', 'ggpht.com', 'googleusercontent.com')))
    except (TypeError, ValueError):
        return False


def opener(settings):
    """为图片/媒体复用相同代理与跳转校验；参数：YouTube有效配置；返回：HTTP打开器。"""
    proxy = settings.youtube.get('proxy')
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy else urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler, CheckedRedirect(media_url))


def runtime(settings):
    """检测本地脚本运行环境；参数：配置；返回：yt-dlp环境选项，缺失明确提示。"""
    selected = settings.youtube.get('js_runtime') or shutil.which('deno') or shutil.which('node')
    if not selected:
        raise NeedsAttention('缺少YouTube解析运行环境，请安装Deno或Node 22以上版本，并在高级设置指定路径')
    name = 'deno' if 'deno' in str(selected).lower() else 'node'
    return {name: {'path': str(selected)}}


class QuietLogger:
    """提取阶段保留停止检查，不将带签名地址的原始日志写入任务。"""
    def __init__(self, checkpoint):
        """保存检查点；参数：回调；返回：无。"""
        self.checkpoint = checkpoint

    def debug(self, message):
        """忽略原始调试内容；参数：文本；返回：无。"""
        self.checkpoint()

    info = debug
    warning = debug
    error = debug


def options(settings, checkpoint):
    """构造仅当前视频的解析选项；参数：有效配置与检查点；返回：固定选项。"""
    result = {'noplaylist': True, 'skip_download': True, 'quiet': True, 'no_warnings': True,
              'socket_timeout': 15, 'extractor_retries': 1, 'retries': 2, 'fragment_retries': 2,
              'cachedir': False, 'logger': QuietLogger(checkpoint), 'js_runtimes': runtime(settings),
              'remote_components': set(), 'geo_bypass': False}
    if settings.youtube.get('proxy'):
        result['proxy'] = settings.youtube['proxy']
    return result


def login(settings, checkpoint, url):
    """使用独立窗口等待人工登录；参数：配置、回调、当前视频；返回：无。"""
    proxy = settings.youtube.get('proxy')
    with browser_session(settings, channel='chrome', proxy={'server': proxy} if proxy else None) as (context, page):
        page.goto(url, wait_until='domcontentloaded')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            checkpoint()
            if any(c['name'] in {'SAPISID', '__Secure-3PAPISID'} and c.get('value') for c in context.cookies('https://www.youtube.com/')):
                save_session(context, settings)
                return
            page.wait_for_timeout(700)
        raise NeedsAttention('登录等待结束，请在登录模式窗口完成人工登录或验证后重试')


def _load_cookies(ydl, settings):
    """只使用本工具保存的YouTube会话；参数：解析器和配置；返回：无。"""
    try:
        cookies = json.loads(settings.session_path.read_text(encoding='utf-8')).get('cookies', [])
    except (OSError, ValueError):
        return
    for c in cookies:
        domain = c.get('domain', '')
        if domain.lstrip('.') not in {'youtube.com', 'www.youtube.com'} or c.get('expires', -1) != -1 and c.get('expires', 0) <= time.time():
            continue
        expires = int(c['expires']) if c.get('expires', -1) > 0 else None
        ydl.cookiejar.set_cookie(http.cookiejar.Cookie(0, c['name'], c['value'], None, False, domain, True,
            domain.startswith('.'), c.get('path', '/'), True, bool(c.get('secure', True)), expires, expires is None, None, None, {}))


def extract(settings, video, checkpoint, interactive=False):
    """仅解析当前视频；参数：规范入口与登录模式；返回：公开元数据、规格和私有来源。"""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    checkpoint()
    if interactive:
        login(settings, checkpoint, video.url)
    try:
        with YoutubeDL(options(settings, checkpoint)) as ydl:
            _load_cookies(ydl, settings)
            info = ydl.extract_info(video.url, download=False)
    except DownloadError as error:
        checkpoint()
        reason = str(error).lower()
        if any(s in reason for s in ('sign in', 'login', 'confirm', 'bot', 'age-restricted', 'private', 'members-only')):
            raise NeedsAttention('YouTube要求登录、人工验证或访问权限，请使用登录模式完成后重试') from None
        if any(s in reason for s in ('unavailable', 'removed', 'not available')):
            raise ValueError('该视频已删除、不可见或当前访问条件不可用') from None
        raise NeedsAttention('YouTube解析未完成，请检查网络或代理、运行环境与解析组件版本后重试') from None
    checkpoint()
    return catalog(info, video.video_id)


def catalog(info, expected_id):
    """保留真实画质、语言与身份；参数：提取结果、预期编号；返回：公开资料与私有源。"""
    if not isinstance(info, dict) or info.get('_type', 'video') != 'video' or info.get('id') != expected_id:
        raise ValueError('返回内容不是所选单个视频，已拒绝处理列表或其他视频')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        raise ValueError('第一版仅下载完整视频或已可用的直播回放，暂不录制正在进行的直播')
    duration = info.get('duration')
    if not isinstance(duration, (float, int)) or not duration > 0:
        raise ValueError('无法核对完整视频时长，暂不准备该内容')
    videos, audios, private = [], [], {}
    for f in info.get('formats') or []:
        # 直接媒体格式具备明确文件实体，复用公共Range校验；不把故事板或动态直播片段当视频。
        if f.get('has_drm') or f.get('protocol') not in {'https', 'http'} or not media_url(f.get('url')):
            continue
        vcodec, acodec = f.get('vcodec'), f.get('acodec')
        has_video = bool(vcodec and vcodec != 'none')
        has_audio = bool(acodec and acodec != 'none')
        if not has_video and not has_audio:
            continue
        codec = vcodec if has_video else acodec
        original = 'original' in (f.get('format_note') or '').lower()
        signature = [str(f['format_id']), codec, f.get('width'), f.get('height'), f.get('fps'), f.get('language'), f.get('format_note')]
        key = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:20]
        bitrate = int((f.get('tbr') or f.get('vbr') or f.get('abr') or 0) * 1000)
        known = int(f.get('filesize') or 0) or None
        spec = {'id': key, 'source_id': str(f['format_id']), 'codec': codec, 'width': int(f.get('width') or 0),
                'height': int(f.get('height') or 0), 'fps': f.get('fps'), 'bitrate': bitrate, 'bytes': known,
                'estimated_bytes': known or int(f.get('filesize_approx') or bitrate * duration / 8) or None,
                'ext': f.get('ext') or 'mp4', 'combined': has_video and has_audio, 'language': f.get('language'),
                'original': original, 'note': f.get('format_note') or '', 'channels': f.get('audio_channels')}
        if key not in private:
            (videos if has_video else audios).append(spec)
            # 只保留下载确实需要的白名单请求头，绝不将登录Cookie发给任意CDN。
            headers = {k: v for k, v in (f.get('http_headers') or {}).items() if k.lower() in {'user-agent', 'referer', 'origin', 'accept-language'} and isinstance(v, str) and '\n' not in v and '\r' not in v}
            private[key] = {'url': f['url'], 'headers': headers}
    videos.sort(key=lambda f: (not f['combined'], f['codec'].startswith('avc'), f['height'], f['fps'] or 0, f['bitrate']), reverse=True)
    audios.sort(key=lambda f: (f['original'], f['codec'].startswith('mp4a'), f['bitrate']), reverse=True)
    if not videos or not audios and not any(v['combined'] for v in videos):
        raise NeedsAttention('当前未取得可核验的完整视频与音轨，请检查网络、登录或更新解析组件后重试')
    timestamp = info.get('timestamp') or info.get('release_timestamp')
    metadata = {'video_id': expected_id, 'title': info.get('title') or expected_id,
                'author': info.get('channel') or info.get('uploader') or '未提供', 'duration': duration,
                'published': datetime.fromtimestamp(timestamp, BEIJING).isoformat() if isinstance(timestamp, (int, float)) else None,
                'upload_date': info.get('upload_date'), 'parsed_at': datetime.now(BEIJING).isoformat(),
                'url': 'https://www.youtube.com/watch?v=' + expected_id,
                'stats': {k: info.get(k) if isinstance(info.get(k), (int, float)) and info[k] >= 0 else None for k in ('view_count', 'like_count', 'comment_count')},
                'cover_available': media_url(info.get('thumbnail'))}
    return metadata, {'videos': videos, 'audios': audios, 'duration': duration, 'created': time.time()}, {
        'tracks': private, 'cover': info.get('thumbnail') if media_url(info.get('thumbnail')) else None, 'created': time.time()}
