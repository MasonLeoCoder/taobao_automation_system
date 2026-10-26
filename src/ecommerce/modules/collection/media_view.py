"""评论和回答媒体的展示地址，不下载或修改源文件。"""
from urllib.parse import urlsplit


def media_preview(reference):
    """整理本地或来源媒体地址；参数：含校验状态的引用；返回：安全展示副本。"""
    result={**reference,'file_id':None,'url':None}
    asset=reference.get('file')
    if asset and reference.get('preview_status')=='present':
        result['file_id']=asset['id']
    else:
        source=reference.get('source_url') or ''
        try:
            parsed=urlsplit(source)
            allowed='img.alicdn.com' if reference['role'].endswith(('image','poster')) else 'cloud.video.taobao.com'
            if parsed.scheme=='https' and parsed.hostname==allowed and not parsed.username and not parsed.password and parsed.port in (None,443):
                result['url']=source
        except ValueError:
            pass
    return result
