"""读取抖音播放来源；不把播放来源自动标记为画面无水印。"""
import time
from urllib.parse import parse_qs, urlsplit

from ecommerce.infrastructure.browser.session import browser_session, save_session
from ecommerce.shared.exceptions import NeedsAttention


def playback_sources(detail: dict, video_id: str) -> list[dict]:
    """筛选无平台下载水印参数的播放源；参数：响应和预期编号；返回：按画质降序的候选，画面仍待核查。"""
    if str(detail.get('aweme_id')) != video_id:
        raise ValueError('返回的视频与输入编号不一致')
    video = detail.get('video') or {}
    addresses = [video.get('play_addr_h264'), video.get('play_addr')]
    addresses.extend(entry.get('play_addr') for entry in video.get('bit_rate', []) if not entry.get('is_h265'))
    forbidden = set((video.get('download_addr') or {}).get('url_list') or [])
    candidates, seen = [], set()
    for address in addresses:
        if not isinstance(address, dict):
            continue
        for url in address.get('url_list') or []:
            if url in seen or url in forbidden or not safe_playback_url(url):
                continue
            seen.add(url)
            candidates.append({'url': url, 'width': int(address.get('width') or video.get('width') or 0),
                               'height': int(address.get('height') or video.get('height') or 0),
                               'bytes': int(address.get('data_size') or 0)})
    candidates.sort(key=lambda row: (row['width'] * row['height'], row['bytes']), reverse=True)
    if not candidates:
        raise ValueError('未获取到可核查的播放源；已拒绝平台下载水印版本')
    return candidates


def safe_playback_url(url: str) -> bool:
    """检查来源域名与显式水印参数；参数：候选地址；返回：是否允许进一步核查。"""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        query = parse_qs(parsed.query)
        return bool(parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and parsed.port in (None, 443)
                    and (host.endswith('.douyinvod.com') or host == 'www.douyin.com')
                    and not any(word in parsed.path.lower() for word in ('/logo/', 'playwm', 'watermark'))
                    and not any(key in query for key in ('logo_type', 'logo_name', 'watermark')))
    except (TypeError, ValueError):
        return False


def resolve_video(settings, video_input, checkpoint, *, interactive=False, timeout=90):
    """在独立会话读取真实详情；参数：平台配置、输入、停止检查、交互模式和秒数；返回：详情与候选来源。"""
    from dataclasses import replace
    from ecommerce.platforms.douyin.links import parse_input
    found = []
    with browser_session(replace(settings, headless=not interactive), channel='chrome') as (context, page):
        def capture(response):
            """只读取详情响应；参数：浏览器响应；返回：无。"""
            if '/aweme/v1/web/aweme/detail/' not in response.url:
                return
            try:
                detail = response.json().get('aweme_detail')
                if detail and (not video_input.video_id or str(detail.get('aweme_id')) == video_input.video_id):
                    found.append(detail)
            except Exception:
                pass
        page.on('response', capture)
        page.goto(video_input.url, wait_until='domcontentloaded', timeout=45000)
        deadline = time.monotonic() + timeout
        while not found and time.monotonic() < deadline:
            checkpoint()
            page.wait_for_timeout(500)
        if not found:
            raise NeedsAttention('未取得视频信息；可能需要登录或完成验证，请打开登录模式后重试')
        checkpoint()
        identifier = str(found[0].get('aweme_id', ''))
        resolved = parse_input('https://www.douyin.com/video/' + identifier)
        # 短链接必须跳转到同一个视频，防止把推荐结果当作分享目标。
        if video_input.video_id is None:
            landed = parse_input(page.url)
            if landed.video_id != resolved.video_id:
                raise ValueError('分享链接跳转结果与视频响应不一致')
        sources = playback_sources(found[0], resolved.video_id)
        save_session(context, settings)
        return found[0], sources
