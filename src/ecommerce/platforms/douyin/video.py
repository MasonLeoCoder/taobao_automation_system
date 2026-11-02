"""读取抖音播放来源；不把播放来源自动标记为画面无水印。"""
import time
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from ecommerce.infrastructure.browser.session import browser_session, save_session
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.time import BEIJING


def playback_sources(detail: dict, video_id: str) -> list[dict]:
    """筛选无平台下载水印参数的播放源；参数：响应和预期编号；返回：按画质降序的候选，画面仍待核查。"""
    if str(detail.get('aweme_id')) != video_id:
        raise ValueError('返回的视频与输入编号不一致')
    video = detail.get('video') or {}
    addresses = [(video.get('play_addr_h264'), 'H.264', 0),
                 (video.get('play_addr'), 'H.265' if video.get('is_h265') else 'H.264', 0)]
    addresses.extend((entry.get('play_addr'), 'H.265' if entry.get('is_h265') else 'H.264', entry.get('bit_rate') or 0)
                     for entry in video.get('bit_rate', []))
    forbidden = set((video.get('download_addr') or {}).get('url_list') or [])
    candidates, seen = [], set()
    for address, codec, bitrate in addresses:
        if not isinstance(address, dict):
            continue
        for url in address.get('url_list') or []:
            if url in seen or url in forbidden or not safe_playback_url(url):
                continue
            seen.add(url)
            key = address.get('url_key') or ''
            if 'bytevc2' in key:
                continue
            rate = int(bitrate or (re.search(r'_(\d+)$', key)[1] if re.search(r'_(\d+)$', key) else 0))
            candidates.append({'url': url, 'codec': codec, 'bitrate': rate,
                               'width': int(address.get('width') or 0),
                               'height': int(address.get('height') or 0),
                               'bytes': int(address.get('data_size') or 0)})
    candidates.sort(key=lambda row: (row['width'] * row['height'], row['bytes']), reverse=True)
    if not candidates:
        raise ValueError('未获取到可核查的播放源；已拒绝平台下载水印版本')
    return candidates


def asset_url(url, kind):
    """限制图片及音乐来源；参数：地址、资源种类；返回：是否允许访问。"""
    try:
        parts=urlsplit(url);host=parts.hostname or ''
        domains=('douyinpic.com','byteimg.com') if kind=='image' else ('douyinstatic.com','douyincdn.com','douyinvod.com','bytecdn.cn')
        return parts.scheme=='https' and not parts.username and not parts.password and parts.port in (None,443) and any(host.endswith('.'+d) for d in domains)
    except (ValueError,TypeError):
        return False


def video_catalog(detail, sources):
    """提取可展示资料与私有资源目录；参数：平台详情和播放来源；返回：无地址元数据、带地址私有目录。"""
    video=detail.get('video') or {};author=detail.get('author') or {};music=detail.get('music') or {}
    groups={}
    for source in sources:
        signature=[source['width'],source['height'],source.get('codec','H.264'),source.get('bitrate',0),source.get('bytes',0)]
        identifier=hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:20]
        if identifier not in groups:
            groups[identifier]={'id':identifier,'width':signature[0],'height':signature[1],'codec':signature[2],
                                'bitrate':signature[3],'bytes':signature[4] or None,'sources':[]}
        groups[identifier]['sources'].append(source)
    formats=sorted(groups.values(),key=lambda f:(f['codec']=='H.264',f['width']*f['height'],f['bitrate'],f['bytes'] or 0),reverse=True)
    def urls(block,kind):
        """只保留支持的资源地址；参数：平台地址块和种类；返回：地址列表。"""
        return list(dict.fromkeys(u for u in (block or {}).get('url_list',[]) if asset_url(u,kind)))
    cover=urls(video.get('origin_cover'),'image') or urls(video.get('cover'),'image')
    avatar=urls(author.get('avatar_thumb'),'image')
    music_urls=urls(music.get('play_url'),'music')
    stats=detail.get('statistics') or {}
    published=detail.get('create_time')
    metadata={'video_id':str(detail['aweme_id']),'title':detail.get('desc') or '抖音视频',
              'author':author.get('nickname') or '未提供','duration':(video.get('duration') or 0)/1000,
              'published':datetime.fromtimestamp(published,BEIJING).isoformat() if isinstance(published,(int,float)) else None,
              'parsed_at':datetime.now(BEIJING).isoformat(),'url':'https://www.douyin.com/video/'+str(detail['aweme_id']),
              'stats':{label:stats.get(key) if isinstance(stats.get(key),(int,float)) and stats[key]>=0 else None
                       for label,key in [('likes','digg_count'),('comments','comment_count'),('favorites','collect_count'),('shares','share_count')]},
              'music':{'title':music.get('title') or '背景音乐','duration':music.get('duration'),'available':bool(music_urls)},
              'cover_available':bool(cover),'avatar_available':bool(avatar)}
    public=[{k:v for k,v in f.items() if k!='sources'} for f in formats]
    return metadata,public,{'formats':groups,'cover':cover,'avatar':avatar,'music':music_urls,'created':time.time()}


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
