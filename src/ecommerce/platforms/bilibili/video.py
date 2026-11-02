"""读取B站公开资料及当前账号可播放的实际轨道，不绕过权限或人工验证。"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from ecommerce.infrastructure.browser.session import browser_session, save_session
from ecommerce.infrastructure.media.transfer import CheckedRedirect
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.time import BEIJING
from .links import allowed_url, parse_input


def media_url(url):
    """限定官方CDN；参数：地址；返回：是否允许，不接受局域网和第三方P2P来源。"""
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
                and any((p.hostname or '').endswith('.' + domain) for domain in ('bilivideo.com', 'bilivideo.cn', 'hdslb.com', 'biliimg.com')))
    except (TypeError, ValueError):
        return False


def _cookies(settings):
    """读取本平台的会话Cookie；参数：独立平台配置；返回：仅用于官方API的请求值。"""
    try:
        values = json.loads(settings.session_path.read_text(encoding='utf-8')).get('cookies', [])
    except (OSError, ValueError):
        return ''
    now = time.time()
    return '; '.join(c['name'] + '=' + c['value'] for c in values
                     if c.get('domain', '').lstrip('.') in {'bilibili.com', 'api.bilibili.com', 'www.bilibili.com'}
                     and (c.get('expires', -1) == -1 or c.get('expires', 0) > now)
                     and re.fullmatch('[A-Za-z0-9_]+', c.get('name', ''))
                     and isinstance(c.get('value'), str) and not any(x in c['value'] for x in '\r\n;'))


def api(settings, path, params, checkpoint):
    """请求固定官方API；参数：配置、固定路径、参数、停止回调；返回：业务数据。"""
    checkpoint()
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
    cookie = _cookies(settings)
    if cookie:
        headers['Cookie'] = cookie
    opener = urllib.request.build_opener(CheckedRedirect(lambda u: urlsplit(u).scheme == 'https' and urlsplit(u).hostname == 'api.bilibili.com'))
    try:
        with opener.open(urllib.request.Request('https://api.bilibili.com/' + path + '?' + urlencode(params), headers=headers), timeout=25) as response:
            data = json.loads(response.read(8 * 1024 * 1024))
    except (OSError, ValueError):
        raise NeedsAttention('B站暂时无法返回资料，请稍后重试；需要登录或验证时请选择登录模式') from None
    checkpoint()
    if data.get('code') != 0:
        code = data.get('code')
        if code in (-101, -352, -412, -403, 62002):
            raise NeedsAttention('B站要求登录、验证或访问权限，请用登录模式完成后重新解析')
        raise ValueError(f'B站未提供该内容（代码 {code}），可能已删除、不可见或暂不可用')
    if not isinstance(data.get('data'), dict):
        raise ValueError('B站返回的资料结构不完整')
    return data['data']


def login(settings, checkpoint, url):
    """打开本平台独立窗口等待人工登录；参数：配置、回调、原始链接；返回：无。"""
    with browser_session(settings, channel='chrome') as (context, page):
        page.goto(url, wait_until='domcontentloaded')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            checkpoint()
            if any(c['name'] == 'SESSDATA' and c.get('value') for c in context.cookies('https://www.bilibili.com/')):
                save_session(context, settings)
                return
            page.wait_for_timeout(700)
        raise NeedsAttention('登录等待已结束，请重新选择登录模式，由你完成登录或验证')


def _short(video, checkpoint):
    """跟随官方短链跳转；参数：入口和回调；返回：具体视频入口。"""
    if video.identity:
        return video
    checkpoint()
    try:
        opener = urllib.request.build_opener(CheckedRedirect(allowed_url))
        with opener.open(urllib.request.Request(video.url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as response:
            result = parse_input(response.url)
    except (OSError, ValueError):
        raise ValueError('短链接未能跳转到具体视频，请打开后复制完整视频链接') from None
    if not result.identity:
        raise ValueError('短链接没有对应到具体投稿')
    return result


def _parts(data):
    """生成投稿内的真实分P；参数：view数据；返回：单集元数据列表。"""
    bv = data['bvid']
    return [{'id': f'{bv}_p{int(p["page"])}', 'bvid': bv, 'cid': int(p['cid']), 'page': int(p['page']),
             'title': p.get('part') or data.get('title') or bv, 'submission_title': data.get('title') or bv,
             'duration': int(p.get('duration') or 0), 'cover': data.get('pic') or '',
             'url': f'https://www.bilibili.com/video/{bv}/?p={int(p["page"])}'}
            for p in data.get('pages', [])]


def resolve(settings, video, checkpoint, interactive=False):
    """只解析目录，不下载视频；参数：入口、回调及登录模式；返回：公开目录、图片地址私有目录。"""
    video = _short(video, checkpoint)
    if interactive:
        login(settings, checkpoint, video.url)
    params = {'bvid': video.identity} if video.identity.startswith('BV') else {'aid': video.identity[2:]}
    data = api(settings, 'x/web-interface/view', params, checkpoint)
    if video.identity.startswith('BV') and data.get('bvid') != video.identity or video.identity.startswith('av') and str(data.get('aid')) != video.identity[2:]:
        raise ValueError('返回的投稿身份与链接不一致')
    parts = _parts(data)
    current = next((p for p in parts if p['page'] == video.page), None)
    if current is None:
        raise ValueError('链接指定的分P不存在，请检查页号')
    season = data.get('ugc_season') or {}
    episodes, seen = [], set()
    # 合集可能含章节；保持平台顺序，去除同一BV和页号的重复项。
    for section in season.get('sections') or []:
        for entry in section.get('episodes') or []:
            arc = entry.get('arc') or {}
            page = entry.get('page') or {}
            bv = entry.get('bvid')
            if not isinstance(bv, str) or not re.fullmatch('BV[A-Za-z0-9]{10}', bv) or not entry.get('cid'):
                continue
            number = int(page.get('page') or 1)
            key = f'{bv}_p{number}'
            if key in seen:
                continue
            seen.add(key)
            episodes.append({'id': key, 'bvid': bv, 'cid': int(entry['cid']), 'page': number, 'collection_index': len(episodes) + 1,
                             'title': entry.get('title') or arc.get('title') or bv,
                             'submission_title': arc.get('title') or entry.get('title') or bv,
                             'duration': int(page.get('duration') or arc.get('duration') or 0),
                             'cover': arc.get('pic') or '', 'url': f'https://www.bilibili.com/video/{bv}/?p={number}'})
    owner = data.get('owner') or {}
    positions = {p['id']: p['collection_index'] for p in episodes}
    for part in parts:
        if part['id'] in positions:
            part['collection_index'] = positions[part['id']]
    stat = data.get('stat') or {}
    public = {'title': data.get('title') or data['bvid'], 'bvid': data['bvid'], 'author': owner.get('name') or '未提供',
              'published': datetime.fromtimestamp(data['pubdate'], BEIJING).isoformat() if data.get('pubdate') else None,
              'parsed_at': datetime.now(BEIJING).isoformat(), 'current': current['id'], 'parts': parts,
              'collection': {'title': season.get('title'), 'declared_count': season.get('ep_count'), 'items': episodes},
              'stats': {k: stat.get(k) if isinstance(stat.get(k), (int, float)) and stat[k] >= 0 else None
                        for k in ('view', 'like', 'reply', 'favorite', 'share')},
              'url': f'https://www.bilibili.com/video/{data["bvid"]}/?p={video.page}'}
    images = {'cover': data.get('pic'), 'avatar': owner.get('face')}
    return public, images


def _urls(track):
    """抽取可用官方CDN候选；参数：轨道；返回：HTTPS地址列表。"""
    values = [track.get('baseUrl') or track.get('base_url') or track.get('url')]
    values += track.get('backupUrl') or track.get('backup_url') or []
    return list(dict.fromkeys(u for value in values if isinstance(value, str)
                              for u in [re.sub('^http://', 'https://', value)] if media_url(u)))


def tracks(settings, episode, checkpoint):
    """重新核对分P身份并获取实际可播放轨道；参数：集元数据；返回：规格及私有来源。"""
    view = api(settings, 'x/web-interface/view', {'bvid': episode['bvid']}, checkpoint)
    current = next((p for p in _parts(view) if p['page'] == episode['page']), None)
    if not current or current['cid'] != episode['cid']:
        raise ValueError('该分集的内容编号已经变化，请重新解析')
    play = api(settings, 'x/player/playurl', {'bvid': episode['bvid'], 'cid': episode['cid'], 'qn': 127, 'fnval': 4048, 'fourk': 1}, checkpoint)
    duration = (play.get('timelength') or 0) / 1000
    if play.get('is_preview') or play.get('is_trial') or not duration or current['duration'] and abs(duration - current['duration']) > max(3, current['duration'] * .02):
        raise ValueError('平台仅提供试看、截断内容或时长不一致，不能作为完整视频保存')
    dash = play.get('dash') or {}
    video, audio, private = [], [], {}
    for kind in ('video', 'audio'):
        for track in dash.get(kind) or []:
            sources = _urls(track)
            if not sources:
                continue
            codec = track.get('codecs') or '未知'
            identifier = f'{kind[0]}{int(track["id"])}_{re.sub("[^A-Za-z0-9]", "", codec)}'
            bitrate = int(track.get('bandwidth') or 0)
            public = {'id': identifier, 'quality': int(track['id']), 'codec': codec,
                      'width': int(track.get('width') or 0), 'height': int(track.get('height') or 0),
                      'fps': str(track.get('frameRate') or track.get('frame_rate') or ''), 'bitrate': bitrate,
                      'bytes': int(track.get('size') or 0) or None,
                      'estimated_bytes': int(bitrate * duration / 8) if bitrate else None}
            if identifier not in private:
                (video if kind == 'video' else audio).append(public)
                private[identifier] = sources
    # 单文件普通MP4退化路径，仅接受完整单段，不将FLV或多段伪装为一个MP4。
    segments = play.get('durl') or []
    if not video and len(segments) == 1 and play.get('format', '').startswith('mp4') and _urls(segments[0]):
        key = 'progressive_' + str(play.get('quality', 0))
        video = [{'id': key, 'quality': play.get('quality', 0), 'codec': 'H.264', 'width': 0, 'height': 0,
                  'fps': '', 'bitrate': 0, 'bytes': segments[0].get('size'), 'estimated_bytes': segments[0].get('size'), 'combined': True}]
        private[key] = _urls(segments[0])
    video.sort(key=lambda f: (f['codec'].startswith(('avc', 'H.264')), f['height'] * f['width'], f['bitrate']), reverse=True)
    audio.sort(key=lambda f: (f['codec'].startswith('mp4a'), f['bitrate']), reverse=True)
    if not video or not audio and not video[0].get('combined'):
        raise NeedsAttention('当前账号未取得可用的完整视频与音频，请登录后重新解析')
    stat = view.get('stat') or {}
    current.update(author=(view.get('owner') or {}).get('name') or '未提供',
                   stats={k: stat.get(k) if isinstance(stat.get(k), (int, float)) and stat[k] >= 0 else None for k in ('view', 'like', 'reply', 'favorite', 'share')},
                   published=datetime.fromtimestamp(view['pubdate'], BEIJING).isoformat() if view.get('pubdate') else None,
                   parsed_at=datetime.now(BEIJING).isoformat())
    return {'videos': video, 'audios': audio, 'duration': duration, 'created': time.time(),
            'episode': current, 'permission_note': '仅列出当前会话实际可播放的规格；登录后可重新获取'}, private
