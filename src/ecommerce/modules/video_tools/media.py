"""素材获取、静态封面转换与本地音轨处理。"""
import hashlib
import os
import subprocess
import time
import urllib.request

from ecommerce.infrastructure.images.converter import convert_image
from ecommerce.platforms.douyin.video import asset_url


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    """按素材种类核对每次跳转。"""
    def __init__(self,kind):
        """记录资源种类；参数：image/music；返回：无。"""
        self.kind=kind

    def redirect_request(self,req,fp,code,msg,headers,newurl):
        """拒绝未知资源跳转；参数：urllib上下文；返回：请求。"""
        if not asset_url(newurl,self.kind):
            raise ValueError('素材跳转到不支持的来源')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch_asset(urls,kind,target,checkpoint):
    """有界流式下载素材；参数：地址列表、种类、目标与停止回调；返回：无。"""
    limit=10*1024*1024 if kind=='image' else 100*1024*1024
    opener=urllib.request.build_opener(AssetRedirect(kind))
    for url in urls[:3]:
        checkpoint()
        if not asset_url(url,kind):
            continue
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.douyin.com/'})
            with opener.open(request,timeout=15) as response:
                if not asset_url(response.url,kind):
                    raise ValueError('素材最终来源无效')
                expected=int(response.headers.get('Content-Length') or 0)
                if expected>limit:
                    raise ValueError('素材超过大小限制')
                size=0
                with target.open('wb') as output:
                    while chunk:=response.read(128*1024):
                        checkpoint();size+=len(chunk)
                        if size>limit:
                            raise ValueError('素材超过大小限制')
                        output.write(chunk)
                if not size or expected and size!=expected:
                    raise ValueError('素材文件不完整')
                return
        except (OSError,ValueError):
            target.unlink(missing_ok=True)
    raise ValueError('未能下载该素材，地址可能已过期，请重新解析后重试')


def image_file(source,target,output_format='original'):
    """验证并转换封面；参数：输入、输出文件和格式；返回：扩展名与MIME。"""
    result=convert_image(source.read_bytes(),output_format)
    target.write_bytes(result.data)
    return result.extension,result.mimetype


def ffmpeg(arguments,checkpoint,timeout=180):
    """只处理本地文件并支持取消；参数：FFmpeg参数、停止回调与秒数；返回：无。"""
    from imageio_ffmpeg import get_ffmpeg_exe
    process=subprocess.Popen([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-nostdin','-y',*arguments],
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    deadline=time.monotonic()+timeout
    try:
        while process.poll() is None:
            checkpoint()
            if time.monotonic()>deadline:
                raise ValueError('音频处理超时，请重试或选择较短的视频')
            time.sleep(.1)
        checkpoint()
        if process.returncode:
            raise ValueError('音频处理失败：文件可能没有音轨、格式不支持或内容不完整')
    finally:
        if process.poll() is None:
            process.kill();process.wait()


def music_file(source,checkpoint):
    """验证音乐并保留原格式字节；参数：文件和停止回调；返回：扩展名与MIME。"""
    with source.open('rb') as stream:
        head=stream.read(32)
    if head.startswith(b'ID3') or len(head)>1 and head[0]==255 and head[1]&224==224:
        extension,mime='.mp3','audio/mpeg'
    elif b'ftyp' in head:
        extension,mime='.m4a','audio/mp4'
    else:
        raise ValueError('背景音乐返回内容不是支持的音频文件')
    ffmpeg(['-i',str(source),'-map','0:a:0','-t','0.1','-f','null','-'],checkpoint,30)
    return extension,mime


def audio_file(source,target,checkpoint):
    """提取所选视频完整音轨；参数：输入、输出M4A、停止回调；返回：无。"""
    ffmpeg(['-i',str(source),'-map','0:a:0','-vn','-c:a','aac','-b:a','192k','-f','ipod',str(target)],checkpoint)


def fingerprint(path):
    """计算文件校验；参数：文件路径；返回：字节数与SHA256。"""
    digest=hashlib.sha256();size=0
    with path.open('rb') as source:
        while chunk:=source.read(1024*1024):
            size+=len(chunk);digest.update(chunk)
    return size,digest.hexdigest()
