"""只解析抖音输入，不访问用户资料或发起网络请求。"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class VideoInput:
    """单条输入；video_id为空时必须先解析短链接，再验证目标。"""

    url: str
    video_id: str | None


def parse_input(value: str) -> VideoInput:
    """提取一个受支持链接；参数：链接或分享文案；返回：标准输入，非法时抛ValueError。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError('请粘贴一个抖音视频链接或分享文案')
    urls = list(dict.fromkeys(url.rstrip('.,，。!！?？;；)）]】}') for url in
                             re.findall(r'https?://[^\s<>"`]+', value.replace('\\&', '&'))))
    if len(urls) != 1:
        raise ValueError('第一版一次只支持一个抖音链接')
    try:
        parts = urlsplit(urls[0])
        if parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError
        host = parts.hostname
    except ValueError:
        raise ValueError('抖音链接格式无效') from None
    if parts.scheme != 'https' or host not in {
        'douyin.com', 'www.douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com'
    }:
        raise ValueError('请使用抖音官方的HTTPS视频链接')
    if host == 'v.douyin.com':
        if not re.fullmatch(r'/[A-Za-z0-9_-]+/?', parts.path):
            raise ValueError('抖音分享短链接格式无效')
        return VideoInput('https://v.douyin.com/' + parts.path.strip('/') + '/', None)
    match = re.fullmatch(r'/(?:share/)?video/(\d{10,25})/?', parts.path)
    modal = parse_qs(parts.query, keep_blank_values=True).get('modal_id', [])
    if modal and (len(modal) != 1 or not re.fullmatch(r'\d{10,25}', modal[0])):
        raise ValueError('视频编号格式无效')
    if match and modal and match[1] != modal[0]:
        raise ValueError('链接中的视频编号不一致，请重新复制')
    # 精选、推荐、搜索和个人页都可能通过modal_id打开视频，主站路径不决定视频身份。
    identifier = match[1] if match else modal[0] if modal and host in {'douyin.com', 'www.douyin.com'} else None
    if not identifier:
        raise ValueError('链接中没有具体视频，请打开视频后复制链接')
    return VideoInput('https://www.douyin.com/video/' + identifier, identifier)


def video_filename(title: str, video_id: str) -> str:
    """生成Windows可保存文件名；参数：标题和视频编号；返回：建议的MP4名称。"""
    if not re.fullmatch(r'\d{10,25}', video_id):
        raise ValueError('视频编号格式无效')
    clean = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', title).strip(' .')[:80].rstrip(' .') or '抖音视频'
    return f'{clean}_{video_id}.mp4'
