"""视频工具缓存、任务和接口的隔离验证，媒体内容为明确标注的模拟字节。"""
import hashlib
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app

VIDEO_ID='7557891045083909439'
INPUT='https://www.douyin.com/video/'+VIDEO_ID
PAYLOAD=b'\x00\x00\x00\x18ftypmp42fixture'


class VideoToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=create_app(load_settings(root=Path(self.temp.name)))
        self.client=self.app.test_client()
        self.service=self.app.extensions['video_tools']
        self.headers={'X-Collection-Token':self.app.config['COLLECTION_TOKEN']}

    def prepared(self):
        """完成模拟准备任务；参数：无；返回：缓存编号。"""
        detail={'aweme_id':VIDEO_ID,'desc':'<script>测试</script>','video':{'duration':15000},'author':{'nickname':'测试'}}
        def fetch(sources,target,checkpoint,progress):
            checkpoint();target.write_bytes(PAYLOAD);progress(len(PAYLOAD),len(PAYLOAD))
            return len(PAYLOAD),hashlib.sha256(PAYLOAD).hexdigest()
        with patch('ecommerce.modules.video_tools.service.resolve_video',return_value=(detail,[])),patch('ecommerce.modules.video_tools.service.fetch_video',side_effect=fetch):
            identifier=self.service.start(INPUT)
            self.service.workers[identifier][0].join(10)
        self.assertTrue(self.service.snapshot(identifier)['ready'])
        return identifier

    def test_page_and_toolbox(self):
        self.assertEqual(self.client.get('/tools/douyin').status_code,200)
        self.assertIn(b'/tools/douyin',self.client.get('/tools').data)

    def test_protection(self):
        self.assertEqual(self.client.post('/api/tools/douyin',json={'input':INPUT}).status_code,403)
        self.assertEqual(self.client.post('/api/tools/douyin',json={'input':INPUT},headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/tools/douyin',base_url='http://evil.example').status_code,403)

    def test_review_and_cache_reuse(self):
        identifier=self.prepared();url='/api/tools/douyin/'+identifier+'/media'
        self.assertEqual(self.client.post(url,json={'reviewed':False},headers=self.headers).status_code,400)
        for _ in range(2):
            response=self.client.post(url,json={'reviewed':True},headers=self.headers)
            self.assertEqual(response.status_code,200);self.assertEqual(response.data,PAYLOAD);response.close()
        preview=self.client.get(url,headers={'Range':'bytes=0-3'})
        self.assertEqual(preview.status_code,206);preview.close()

    def test_refresh_and_tamper(self):
        identifier=self.prepared()
        from ecommerce.modules.video_tools.service import VideoTools
        recovered=VideoTools(self.service.settings)
        self.assertTrue(recovered.snapshot(identifier)['ready'])
        path=recovered._path(identifier)/'video.mp4'
        path.write_bytes(b'x'*len(PAYLOAD))
        with self.assertRaises(ValueError):recovered.file(identifier)

    def test_expiry_and_traversal(self):
        identifier=self.prepared();row=self.service._read(identifier);row['expires']=time.time()-1;self.service._write(row)
        self.assertFalse(self.service.snapshot(identifier)['ready'])
        self.service.cleanup();self.assertFalse((self.service._path(identifier)/'video.mp4').exists())
        with self.assertRaises(LookupError):self.service.file('../outside')

    def test_dedup_and_cancel(self):
        started=threading.Event();release=threading.Event()
        def resolve(settings,video,checkpoint,**kwargs):
            started.set();release.wait(5);checkpoint();raise AssertionError('取消后不应继续')
        with patch('ecommerce.modules.video_tools.service.resolve_video',side_effect=resolve):
            identifier=self.service.start(INPUT);self.assertTrue(started.wait(5))
            from ecommerce.infrastructure.tasks.lock import all_browser_resources
            from ecommerce.shared.exceptions import TaskBusy
            with self.assertRaises(TaskBusy):
                with all_browser_resources(self.app.config['SETTINGS']):
                    pass
            self.assertEqual(identifier,self.service.start(INPUT))
            self.service.cancel(identifier);release.set();self.service.workers[identifier][0].join(10)
        self.assertEqual(self.service.snapshot(identifier)['phase'],'cancelled')

    def test_fetch_rejects_html_and_truncated_media(self):
        from ecommerce.modules.video_tools.service import fetch_video
        source={'url':'https://v11.douyinvod.com/video/source'}
        for payload,total in [(b'<html>error</html>',18),(PAYLOAD,len(PAYLOAD)+1)]:
            response=BytesIO(payload);response.url=source['url'];response.headers={'Content-Length':str(total)}
            with patch('ecommerce.modules.video_tools.service.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=response
                target=Path(self.temp.name)/'video.part'
                with self.assertRaises(ValueError):fetch_video([source],target,lambda:None,lambda a,b:None)
                self.assertFalse(target.exists())

    def test_redirect_cannot_escape_or_add_watermark(self):
        from ecommerce.modules.video_tools.service import SafeRedirect
        for url in ['http://127.0.0.1/video','https://evil.example/x','https://v11.douyinvod.com/logo/x']:
            with self.assertRaises(ValueError):SafeRedirect().redirect_request(None,None,302,'',{},url)


if __name__=='__main__':unittest.main()
