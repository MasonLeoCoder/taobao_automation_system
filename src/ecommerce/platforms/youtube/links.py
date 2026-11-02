"""只解析当前YouTube视频，丢弃列表、起始时间及跟踪参数。"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class VideoInput:
    """规范化的单视频身份。"""
    url: str
    video_id: str


def parse_input(text):
    """解析单视频链接；参数：链接或分享文案；返回：标准watch入口。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        raise ValueError('请输入一个YouTube视频链接')
    urls = list(dict.fromkeys(u.rstrip('.,，。；;!！）】') for u in re.findall(r'https?://[^\s<>"\[\]\\()]+', text)))
    if len(urls) != 1:
        raise ValueError('一次只处理一个YouTube视频链接')
    try:
        p = urlsplit(urls[0])
        if p.scheme != 'https' or p.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtube-nocookie.com'} or p.username or p.password or p.port not in (None, 443):
            raise ValueError('仅支持YouTube官方HTTPS视频链接')
        query = parse_qs(p.query, keep_blank_values=True)
        ids = query.get('v', [])
        if len(ids) > 1:
            raise ValueError('链接包含重复的视频编号')
        if p.hostname == 'youtu.be':
            match = re.fullmatch(r'/([A-Za-z0-9_-]{11})/?', p.path)
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?', p.path)
        if match:
            video_id = match[1]
            if ids and ids != [video_id]:
                raise ValueError('链接中的视频编号冲突')
        elif p.path == '/watch' and len(ids) == 1:
            video_id = ids[0]
        else:
            raise ValueError('请提供具体视频链接，不支持播放列表或频道入口')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('YouTube视频编号无效')
        return VideoInput('https://www.youtube.com/watch?v=' + video_id, video_id)
    except (TypeError, OverflowError):
        raise ValueError('视频链接格式无效') from None
