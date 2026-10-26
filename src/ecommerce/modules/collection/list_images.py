"""列表主图下载：保留原链接，解码校验后保存，单张失败不丢商品。"""
import base64
import hashlib
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_images import FORMATS, MAX_IMAGE_BYTES, image_format, _ImageRedirect
from ecommerce.modules.collection.detail_video import validate_media_url


def download_list_image(context, page, url, directory, checkpoint):
    """下载并解码一张图；参数：会话、解码页、源地址、目录、检查点；返回：文件元数据。"""
    url = validate_media_url(url)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ('.' + uuid4().hex + '.part')
    # 请求使用当前会话的对应域 Cookie；不写入日志和导出。
    headers = {'Referer': 'https://s.taobao.com/', 'Accept': 'image/*',
               'User-Agent': page.evaluate('navigator.userAgent')}
    cookies = context.cookies([url])
    if cookies:
        headers['Cookie'] = '; '.join(c['name'] + '=' + c['value'] for c in cookies)

    class Redirect(_ImageRedirect):
        """校验每次重定向，并避免跨域传播会话凭据。"""
        def redirect_request(self, request, response, code, message, response_headers, newurl):
            redirected = super().redirect_request(request, response, code, message, response_headers, newurl)
            if redirected is not None:
                redirected.remove_header('Cookie')
            return redirected

    size, digest = 0, hashlib.sha256()
    try:
        with build_opener(ProxyHandler({}), Redirect()).open(Request(url, headers=headers), timeout=15) as response:
            validate_media_url(response.url)
            if response.status != 200:
                raise ValueError('图片响应不是完整文件')
            mime = response.headers.get_content_type()
            if not (mime.startswith('image/') or mime == 'application/octet-stream'):
                raise ValueError('响应内容不是图片')
            length = response.headers.get('Content-Length')
            expected = int(length) if length and length.isdigit() else None
            if expected is not None and not 0 < expected <= MAX_IMAGE_BYTES:
                raise ValueError('图片大小无效或超过32MB')
            with temporary.open('xb') as output:
                while True:
                    checkpoint('下载列表主图')
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ValueError('图片超过32MB')
                    output.write(chunk)
                    digest.update(chunk)
            if expected is not None and size != expected:
                raise ValueError('图片未完整接收')
        data = temporary.read_bytes()
        fmt = image_format(data[:64])
        suffix, mime = FORMATS[fmt]
        checkpoint('校验列表主图')
        dimensions = page.evaluate("""async ({data,mime})=>{
            const image=new Image();image.src='data:'+mime+';base64,'+data;
            await image.decode();return {width:image.naturalWidth,height:image.naturalHeight};
        }""", {'data': base64.b64encode(data).decode('ascii'), 'mime': mime})
        if not dimensions['width'] or not dimensions['height']:
            raise ValueError('图片尺寸无效')
        destination = directory / (digest.hexdigest() + suffix)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest.hexdigest():
                raise ValueError('现有图片文件校验冲突')
        else:
            temporary.rename(destination)
        return {'image_path': str(destination.resolve()), 'sha256': digest.hexdigest(),
                'size_bytes': size, 'content_type': mime, **dimensions}
    finally:
        temporary.unlink(missing_ok=True)


def collect_list_image(context, page, url, directory, checkpoint):
    """单图有限重试；参数：会话、解码页、链接、目录、检查点；返回：成功或失败结果。"""
    if not url:
        return {'status': 'not_applicable', 'reason': '未获取图片地址', 'attempts': 0, 'processed_at': now()}
    reason = '图片下载失败'
    for attempt in range(1, 4):
        checkpoint('处理列表主图')
        try:
            saved = download_list_image(context, page, url, directory, checkpoint)
            return {'status': 'downloaded', 'attempts': attempt, 'processed_at': now(), **saved}
        except HTTPError as error:
            reason = '图片请求返回 HTTP ' + str(error.code)
        except (URLError, TimeoutError):
            reason = '图片请求超时或连接失败'
        except IncompleteRead:
            reason = '图片未完整接收'
        except (ValueError, PlaywrightError):
            # 不把异常中的签名链接、Cookie 等写入业务记录。
            reason = '图片请求、格式或解码校验失败'
    return {'status': 'failed', 'reason': reason, 'attempts': 3, 'processed_at': now()}


def collect_list_images(settings, context, page, items, batch_id, checkpoint):
    """处理整页主图；参数：配置、会话、页面、商品、批次、检查点；返回：数量汇总，原位补充商品。"""
    directory = settings.workspace / 'output/raw/taobao_products/images' / batch_id
    counts = {'total': len(items), 'downloaded': 0, 'failed': 0, 'not_applicable': 0}
    for item in items:
        item['list_image'] = collect_list_image(context, page, item.get('main_image_url'), directory, checkpoint)
        counts[item['list_image']['status']] += 1
    return counts
