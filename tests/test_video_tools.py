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

    def resolved(self,detail=None):
        """解析模拟资料但不下载视频；参数：可选资料；返回：记录编号。"""
        detail=detail or {'aweme_id':VIDEO_ID,'desc':'<script>测试</script>','video':{'duration':15000},'author':{'nickname':'测试'}}
        sources=[{'url':'https://v11.douyinvod.com/video/source','width':1080,'height':1920,'codec':'H.264','bitrate':1700000,'bytes':len(PAYLOAD)},
                 {'url':'https://v26.douyinvod.com/video/other','width':720,'height':1280,'codec':'H.265','bitrate':550000,'bytes':len(PAYLOAD)}]
        with patch('ecommerce.modules.video_tools.service.resolve_video',return_value=(detail,sources)),patch('ecommerce.modules.video_tools.service.fetch_video') as download:
            identifier=self.service.start(INPUT)
            self.service.workers[identifier][0].join(10)
            download.assert_not_called()
        self.assertTrue(self.service.snapshot(identifier)['metadata_ready'])
        self.video_key='video_'+self.service.snapshot(identifier)['formats'][0]['id']
        return identifier

    def prepared(self):
        """完成已选择版本的模拟准备；参数：无；返回：记录编号。"""
        identifier=self.resolved()
        def fetch(sources,target,checkpoint,progress):
            checkpoint();target.write_bytes(PAYLOAD);progress(len(PAYLOAD),len(PAYLOAD))
            return len(PAYLOAD),hashlib.sha256(PAYLOAD).hexdigest()
        with patch('ecommerce.modules.video_tools.service.fetch_video',side_effect=fetch):
            self.service.prepare(identifier,'video',self.video_key[6:])
            self.service.workers[identifier][0].join(10)
        self.assertTrue(self.service.snapshot(identifier)['resources'][self.video_key]['ready'])
        return identifier

    def test_page_and_toolbox(self):
        self.assertEqual(self.client.get('/tools/douyin').status_code,200)
        self.assertIn(b'/tools/douyin',self.client.get('/tools').data)

    def test_protection(self):
        self.assertEqual(self.client.post('/api/tools/douyin',json={'input':INPUT}).status_code,403)
        self.assertEqual(self.client.post('/api/tools/douyin',json={'input':INPUT},headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/tools/douyin',base_url='http://evil.example').status_code,403)

    def test_review_and_cache_reuse(self):
        identifier=self.prepared();url='/api/tools/douyin/'+identifier+'/assets/'+self.video_key
        self.assertEqual(self.client.post(url,json={'reviewed':False},headers=self.headers).status_code,400)
        for _ in range(2):
            response=self.client.post(url,json={'reviewed':True},headers=self.headers)
            self.assertEqual(response.status_code,200);self.assertEqual(response.data,PAYLOAD);response.close()
        preview=self.client.get(url,headers={'Range':'bytes=0-3'})
        self.assertEqual(preview.status_code,206);preview.close()

    def test_refresh_and_tamper(self):
        identifier=self.prepared()
        from ecommerce.modules.video_tools.studio import VideoTools
        recovered=VideoTools(self.service.settings)
        self.assertTrue(recovered.snapshot(identifier)['resources'][self.video_key]['ready'])
        path=recovered._asset_path(identifier,self.video_key)
        path.write_bytes(b'x'*len(PAYLOAD))
        with self.assertRaises(ValueError):recovered.file(identifier,self.video_key)

    def test_expiry_and_traversal(self):
        identifier=self.prepared();row=self.service._read(identifier);row['expires']=time.time()-1;self.service._write(row)
        self.assertFalse(self.service.snapshot(identifier)['resources'][self.video_key]['ready'])
        self.service.cleanup();self.assertFalse(self.service._asset_path(identifier,self.video_key).exists())
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

    def test_resolve_only_and_private_sources(self):
        identifier=self.resolved();row=self.service.snapshot(identifier)
        self.assertEqual(len(row['formats']),2)
        self.assertFalse(any(k.startswith('video_') for k in row['resources']))
        self.assertNotIn('douyinvod.com',str(row))
        self.assertNotIn('sources',str(row['formats']))
        self.assertEqual(row['formats'][0]['codec'],'H.264')

    def test_selected_format_cache_and_audio_failure_do_not_replace_video(self):
        identifier=self.prepared();fmt=self.video_key[6:]
        with patch('ecommerce.modules.video_tools.service.fetch_video') as fetch:
            self.assertEqual(self.video_key,self.service.prepare(identifier,'video',fmt));fetch.assert_not_called()
        with patch('ecommerce.modules.video_tools.studio.audio_file',side_effect=ValueError('没有音轨')):
            self.service.prepare(identifier,'audio',fmt);self.service.workers[identifier][0].join(10)
        row=self.service.snapshot(identifier)
        self.assertEqual(row['phase'],'failed')
        self.assertTrue(row['resources'][self.video_key]['ready'])
        self.assertTrue(row['metadata_ready'])

    def test_wrong_formats_and_bad_action_types(self):
        identifier=self.resolved()
        for kind,fmt,output in [('video','not-a-format','original'),('video',{},'original'),({},None,'original'),('cover',None,[])]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                self.service.prepare(identifier,kind,fmt,output)
        self.assertEqual(self.client.post('/api/tools/douyin/'+identifier,json={'action':'prepare','kind':{}},headers=self.headers).status_code,422)

    def test_delete_cache_retains_metadata_and_history(self):
        identifier=self.prepared()
        with patch('pathlib.Path.unlink',side_effect=PermissionError('模拟文件占用')):
            with self.assertRaisesRegex(ValueError,'正在被预览'):
                self.service.delete_cache(identifier)
        self.assertTrue(self.service.snapshot(identifier)['resources'][self.video_key]['ready'])
        self.service.delete_cache(identifier)
        row=self.service.snapshot(identifier)
        self.assertEqual(row['phase'],'cleared')
        self.assertTrue(row['metadata_ready']);self.assertFalse(row['can_prepare']);self.assertEqual(row['resources'],{})
        self.assertFalse(self.service._asset_path(identifier,self.video_key).exists())
        self.assertEqual(row['items'][0]['video_id'],VIDEO_ID)
        self.assertTrue(self.service.recent())

    def test_cover_formats_validate_pixels_and_preserve_original(self):
        from PIL import Image
        from ecommerce.modules.video_tools.media import image_file
        source=Path(self.temp.name)/'source.png';target=Path(self.temp.name)/'output.bin'
        Image.new('RGBA',(8,12),(0,128,0,180)).save(source)
        ext,mime=image_file(source,target,'original')
        self.assertEqual(target.read_bytes(),source.read_bytes());self.assertEqual(mime,'image/png')
        ext,mime=image_file(source,target,'jpg')
        with Image.open(target) as decoded:
            self.assertEqual(decoded.size,(8,12));self.assertEqual(decoded.format,'JPEG')

    def test_format_refresh_never_substitutes_another_spec(self):
        import json
        identifier=self.resolved();row=self.service._read(identifier);fmt=row['formats'][0]['id']
        path=self.service._path(identifier)/'catalog.json';catalog=json.loads(path.read_text(encoding='utf-8'));catalog['created']=0;path.write_text(json.dumps(catalog),encoding='utf-8')
        detail={'aweme_id':VIDEO_ID,'video':{'duration':15000}}
        sources=[{'url':'https://v11.douyinvod.com/video/new','width':480,'height':640,'bytes':12,'codec':'H.264','bitrate':10000}]
        with patch('ecommerce.modules.video_tools.service.resolve_video',return_value=(detail,sources)),patch('ecommerce.modules.video_tools.service.fetch_video') as download:
            self.service.prepare(identifier,'video',fmt);self.service.workers[identifier][0].join(10)
            download.assert_not_called()
        row=self.service.snapshot(identifier);self.assertEqual(row['phase'],'failed');self.assertTrue(row['metadata_ready'])

    def test_real_audio_extraction_and_music_validation(self):
        from ecommerce.modules.video_tools.media import ffmpeg,audio_file,music_file
        source=Path(self.temp.name)/'tone.m4a';target=Path(self.temp.name)/'audio.m4a'
        ffmpeg(['-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','aac',str(source)],lambda:None)
        audio_file(source,target,lambda:None)
        self.assertGreater(target.stat().st_size,100)
        self.assertEqual(music_file(target,lambda:None),('.m4a','audio/mp4'))
        bad=Path(self.temp.name)/'bad.mp3';bad.write_bytes(b'<html>error</html>')
        with self.assertRaises(ValueError):music_file(bad,lambda:None)

    def test_legacy_cache_still_previews_and_exports(self):
        from ecommerce.modules.video_tools.service import VideoTools
        old=VideoTools(self.app.config['SETTINGS'])
        detail={'aweme_id':VIDEO_ID,'desc':'旧缓存','video':{'duration':15000}}
        def fetch(sources,target,checkpoint,progress):
            target.write_bytes(PAYLOAD);return len(PAYLOAD),hashlib.sha256(PAYLOAD).hexdigest()
        with patch('ecommerce.modules.video_tools.service.resolve_video',return_value=(detail,[])),patch('ecommerce.modules.video_tools.service.fetch_video',side_effect=fetch):
            identifier=old.start(INPUT);old.workers[identifier][0].join(10)
        self.assertTrue(self.service.snapshot(identifier)['legacy'])
        response=self.client.post('/api/tools/douyin/'+identifier+'/media',json={'reviewed':True},headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.data,PAYLOAD);response.close()


if __name__=='__main__':unittest.main()
