"""解析B站输入，仅保留明确的视频身份，不保留跟踪参数。"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class VideoInput:
    """单个入口的视频编号、页号或待解析短链接。"""
    url: str
    identity: str | None = None
    page: int = 1


def allowed_url(url, *, short=False):
    """检查官方入口域名；参数：地址及是否仅短链；返回：布尔值。"""
    try:
        p = urlsplit(url)
        hosts = {'b23.tv'} if short else {'www.bilibili.com', 'bilibili.com', 'm.bilibili.com', 'b23.tv'}
        return p.scheme == 'https' and p.hostname in hosts and not p.username and not p.password and p.port in (None, 443)
    except (ValueError, TypeError):
        return False


def parse_input(text):
    """提取一个视频入口；参数：链接、BV/av号或分享文字；返回：规范化入口。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        raise ValueError('请输入一个B站视频链接或BV/av号')
    text = text.strip()
    if re.fullmatch(r'BV[0-9A-Za-z]{10}|av[1-9][0-9]*', text):
        text = 'https://www.bilibili.com/video/' + text
    urls = list(dict.fromkeys(u.rstrip('.,，。；;!！）】') for u in re.findall(r'https?://[^\s<>"\[\]\\()]+', text)))
    if len(urls) != 1:
        raise ValueError('本次请粘贴一个视频链接；多链接输入将在后续扩展')
    url = urls[0]
    if not allowed_url(url):
        raise ValueError('仅支持B站官方HTTPS视频链接或b23.tv短链接')
    p = urlsplit(url)
    if p.hostname == 'b23.tv':
        if not re.fullmatch(r'/[A-Za-z0-9]+/?', p.path):
            raise ValueError('短链接格式无效')
        return VideoInput(url)
    match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?', p.path)
    if not match:
        raise ValueError('请打开具体投稿后复制视频链接')
    query = parse_qs(p.query, keep_blank_values=True)
    pages = query.get('p', ['1'])
    if len(pages) != 1 or not re.fullmatch('[1-9][0-9]{0,4}', pages[0]):
        raise ValueError('分P页号无效或重复')
    identity = match[1]
    for key in ('bvid', 'aid'):
        if key in query and (len(query[key]) != 1 or query[key][0] != (identity if key == 'bvid' else identity.removeprefix('av'))):
            raise ValueError('链接包含冲突的视频编号')
    page = int(pages[0])
    return VideoInput(f'https://www.bilibili.com/video/{identity}/?p={page}', identity, page)
