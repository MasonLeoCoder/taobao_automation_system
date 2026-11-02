"""YouTube单视频、规格/语言和资源生命周期的隔离测试，禁止真实平台请求。"""
import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO
from PIL import Image

from ecommerce.modules.video_tools.youtube import YouTubeTools, resource_key, output_container
from ecommerce.platforms.youtube.links import parse_input
from ecommerce.platforms.youtube.video import catalog, media_url, options
from ecommerce.shared.config import load_settings, youtube_options
from ecommerce.web.app import create_app
from ecommerce.infrastructure.media import transfer

ID = 'VlFoYM8LsBQ'
V = {'id': '1'*20, 'source_id': '134', 'codec': 'avc1.test', 'width': 640, 'height': 360, 'fps': 30,
     'bitrate': 5000, 'bytes': None, 'estimated_bytes': 2000, 'ext': 'mp4', 'combined': False, 'language': None, 'note': '', 'original': False}
V2 = {**V, 'id': '2'*20, 'height': 720, 'width': 1280, 'fps': 60}
A = {'id': '3'*20, 'source_id': '140', 'codec': 'mp4a.40.2', 'width': 0, 'height': 0, 'fps': None,
     'bitrate': 2000, 'bytes': None, 'estimated_bytes': 1000, 'ext': 'm4a', 'combined': False, 'language': 'en-US', 'note': 'original', 'original': True}
A2 = {**A, 'id': '4'*20, 'language': 'ja', 'original': False}
META = {'video_id': ID, 'title': '隔离测试 <script>', 'author': '样例频道', 'duration': 2, 'published': None,
        'parsed_at': '2026-11-02T12:00:00+08:00', 'stats': {'view_count': 0, 'like_count': None}, 'url': 'https://www.youtube.com/watch?v='+ID}
DATA = b'\0\0\0\x18ftypmp42-synthetic-fixture'


class YouTubeInputTests(unittest.TestCase):
    def test_only_current_video_and_complete_duration(self):
        for text in ('https://www.youtube.com/watch?v='+ID+'&list=PL123&t=30&index=2',
                     '分享 https://youtu.be/'+ID+'?si=tracking&t=123', 'https://www.youtube.com/shorts/'+ID,
                     'https://m.youtube.com/live/'+ID, '[https://www.youtube.com/watch?v='+ID+'](https://www.youtube.com/watch?v='+ID+')'):
            with self.subTest(text=text):
                result = parse_input(text)
                self.assertEqual(result.video_id, ID)
                self.assertEqual(result.url, 'https://www.youtube.com/watch?v='+ID)

    def test_reject_lists_channels_conflicts_and_other_hosts(self):
        for text in ('https://www.youtube.com/playlist?list=abc','https://www.youtube.com/@channel','https://youtube.com.evil.test/watch?v='+ID,
                     'https://user@www.youtube.com/watch?v='+ID,'http://www.youtube.com/watch?v='+ID,
                     'https://www.youtube.com/watch?v='+ID+'&v='+ID,'https://youtu.be/'+ID+'?v=abcdefghijk',
                     'https://www.youtube.com/watch?v='+ID+' https://youtu.be/abcdefghijk','https://www.youtube.com/watch?v=bad'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_input(text)

    def test_options_isolation_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            with patch('ecommerce.platforms.youtube.video.shutil.which',return_value='node'):
                opts = options(settings, lambda: None)
            self.assertTrue(opts['noplaylist']); self.assertTrue(opts['skip_download']); self.assertFalse(opts['geo_bypass'])
            self.assertEqual(youtube_options({},settings.root)['cache_warn_gib'],20)
            for values in ({'scope':'all'},{'proxy':'http://user:pass@host:80'},{'proxy':'socks5://localhost:80'},{'idle_hours':0}):
                with self.assertRaises(ValueError): youtube_options(values,settings.root)
        self.assertTrue(media_url('https://rr1.googlevideo.com/videoplayback'))
        self.assertFalse(media_url('https://googlevideo.com.evil.test/media'))
        self.assertFalse(media_url('http://rr1.googlevideo.com/media'))

    def info(self):
        return {'id':ID,'duration':2,'title':'样例','view_count':0,'formats':[
            {'format_id':'v1','vcodec':'avc1.123','acodec':'none','height':1080,'width':1920,'fps':60,'protocol':'https','url':'https://a.googlevideo.com/v','ext':'mp4','tbr':1000},
            {'format_id':'a1','vcodec':'none','acodec':'mp4a.40.2','protocol':'https','url':'https://a.googlevideo.com/a','language':'en','format_note':'English original','abr':128},
            {'format_id':'a2','vcodec':'none','acodec':'mp4a.40.2','protocol':'https','url':'https://a.googlevideo.com/b','language':'ja','format_note':'dubbed','abr':128},
            {'format_id':'drm','vcodec':'av01','acodec':'none','has_drm':True,'protocol':'https','url':'https://a.googlevideo.com/c'},
            {'format_id':'story','vcodec':'none','acodec':'none','protocol':'mhtml','url':'https://i.ytimg.com/story'}]}

    def test_catalog_keeps_fps_original_language_and_filters(self):
        meta, cat, sources = catalog(self.info(), ID)
        self.assertEqual(len(cat['videos']),1); self.assertEqual(len(cat['audios']),2)
        self.assertEqual(cat['videos'][0]['fps'],60)
        self.assertTrue(cat['audios'][0]['original']); self.assertEqual(cat['audios'][0]['language'],'en')
        self.assertEqual(meta['stats']['view_count'],0); self.assertIsNone(meta['stats']['like_count'])
        self.assertNotIn('googlevideo',json.dumps(cat)); self.assertEqual(len(sources['tracks']),3)
        changed=self.info(); changed['formats'][1]['language']='fr'
        self.assertNotEqual(cat['audios'][0]['id'],catalog(changed,ID)[1]['audios'][0]['id'])

    def test_reject_live_mismatch_and_empty_tracks(self):
        for info in ({**self.info(),'id':'abcdefghijk'},{**self.info(),'_type':'playlist'},{**self.info(),'is_live':True},
                     {**self.info(),'duration':0},{**self.info(),'formats':[]}):
            with self.subTest(info=info.get('id')):
                from ecommerce.shared.exceptions import NeedsAttention
                with self.assertRaises((ValueError,NeedsAttention)):catalog(info,ID)
        self.assertEqual(output_container({**V,'codec':'vp9'},{**A,'codec':'opus'})[0],'webm')
        self.assertEqual(output_container(V,A)[0],'mp4')


class YouTubeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings=load_settings(root=Path(self.temp.name)); self.app=create_app(self.settings)
        self.tool=self.app.extensions['youtube_tools']; self.client=self.app.test_client()
        self.headers={'X-Collection-Token':self.app.config['COLLECTION_TOKEN']}
        self.patches=[patch('ecommerce.modules.video_tools.youtube.platform.extract',side_effect=self.extract),
                      patch('ecommerce.modules.video_tools.youtube.transfer.download',side_effect=self.download),
                      patch('ecommerce.modules.video_tools.youtube.local.inspect_media'),
                      patch('ecommerce.modules.video_tools.youtube.local.merge',side_effect=lambda v,a,out,*args,**kw:out.write_bytes(DATA)),
                      patch('ecommerce.modules.video_tools.youtube.local.extract_audio',side_effect=lambda src,out,*args,**kw:out.write_bytes(DATA))]
        self.mocks=[p.start() for p in self.patches]
        for p in self.patches:self.addCleanup(p.stop)

    def extract(self,settings,video,checkpoint,interactive=False):
        checkpoint()
        return copy.deepcopy(META), {'videos':copy.deepcopy([V,V2]),'audios':copy.deepcopy([A,A2]),'duration':2,'created':time.time()}, {
            'created':time.time(),'cover':None,'tracks':{s['id']:{'url':'https://a.googlevideo.com/'+s['id'],'headers':{}} for s in [V,V2,A,A2]}}

    def download(self,sources,target,**kwargs):
        kwargs['checkpoint'](); target.write_bytes(DATA); kwargs['progress'](len(DATA),len(DATA),100,'')
        return transfer.fingerprint(target)

    def wait(self,identifier):
        self.tool.workers[identifier][0].join(10)
        self.assertFalse(self.tool.workers[identifier][0].is_alive())
        return self.tool.snapshot(identifier)

    def resolved(self):
        identifier=self.tool.start('https://www.youtube.com/watch?v='+ID+'&list=ignored&t=100')
        self.assertEqual(self.wait(identifier)['phase'],'resolved');return identifier

    def prepared(self):
        identifier=self.resolved(); key=self.tool.prepare(identifier,'video',V['id'],A['id'])
        self.assertEqual(self.wait(identifier)['phase'],'prepared');return identifier,key

    def test_page_protection_and_no_list_actions(self):
        self.assertEqual(self.client.get('/tools/youtube').status_code,200)
        self.assertIn(b'/tools/youtube',self.client.get('/tools').data)
        self.assertEqual(self.client.post('/api/tools/youtube',json={'input':ID}).status_code,403)
        self.assertEqual(self.client.post('/api/tools/youtube',json={'input':ID},headers={**self.headers,'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.get('/api/tools/youtube',base_url='http://evil.test').status_code,403)
        identifier=self.resolved()
        for extra in ({'scope':'collection'},{'episode_id':'fake'},{'kind':[]},{'action':'select'}):
            body={'action':'prepare','kind':'video','video_id':V['id'],'audio_id':A['id'],**extra}
            self.assertIn(self.client.post('/api/tools/youtube/'+identifier,json=body,headers=self.headers).status_code,(400,422))

    def test_parse_only_and_exact_quality_language(self):
        identifier=self.resolved();self.mocks[1].assert_not_called()
        self.assertEqual(self.tool.snapshot(identifier)['input'],'https://www.youtube.com/watch?v='+ID)
        one=self.tool.prepare(identifier,'video',V['id'],A['id']);self.wait(identifier)
        two=self.tool.prepare(identifier,'video',V2['id'],A['id']);self.wait(identifier)
        three=self.tool.prepare(identifier,'video',V['id'],A2['id']);row=self.wait(identifier)
        self.assertEqual(len({one,two,three}),3)
        self.assertTrue(all(row['resources'][key]['ready'] for key in [one,two,three]))
        calls=self.mocks[1].call_count;self.tool.prepare(identifier,'video',V['id'],A['id'])
        self.assertEqual(calls,self.mocks[1].call_count)
        restored=YouTubeTools(self.settings).snapshot(identifier)
        self.assertEqual(restored['selection'],{'video_id':V['id'],'audio_id':A['id']})
        self.assertEqual(restored['operation']['key'],one)

    def test_refresh_missing_quality_does_not_fallback(self):
        identifier=self.resolved();row=self.tool._read(identifier);row['catalog']['created']=0
        source=self.tool._path(identifier)/'sources_video.json';values=json.loads(source.read_text());values['created']=0;source.write_text(json.dumps(values))
        def changed(*args,**kw):
            meta,cat,private=self.extract(*args,**kw);cat['videos']=[V2];return meta,cat,private
        self.mocks[0].side_effect=changed
        self.tool.prepare(identifier,'video',V['id'],A['id']);row=self.wait(identifier)
        self.assertEqual(row['phase'],'failed');self.mocks[1].assert_not_called()

    def test_pause_resume_and_cancel_only_incomplete(self):
        identifier=self.resolved();entered=threading.Event()
        def pausing(sources,target,**kwargs):
            target.write_bytes(DATA[:5]);entered.set()
            while True:time.sleep(.01);kwargs['checkpoint']()
        self.mocks[1].side_effect=pausing
        self.tool.prepare(identifier,'video',V['id'],A['id']);self.assertTrue(entered.wait(4))
        self.tool.control(identifier,'pause');row=self.wait(identifier)
        self.assertEqual(row['phase'],'paused');count=len(row['attempts'])
        self.assertTrue(list(self.tool._cache(self.tool._read(identifier)).glob('*.part')))
        self.mocks[1].side_effect=self.download;self.tool.control(identifier,'resume');row=self.wait(identifier)
        self.assertEqual(row['phase'],'prepared');self.assertEqual(len(row['attempts']),count+1)
        cache=self.tool._cache(self.tool._read(identifier));(cache/'unfinished.part').write_bytes(b'x')
        raw=self.tool._read(identifier);raw['phase']='paused';self.tool._write(raw);self.tool.control(identifier,'cancel')
        self.assertFalse((cache/'unfinished.part').exists());self.assertTrue(list(cache.glob('*.track')))

    def test_hash_review_preview_and_cache_namespace(self):
        identifier,key=self.prepared();url='/api/tools/youtube/'+identifier+'/assets/'+key
        self.assertEqual(self.client.post(url,json={'reviewed':False},headers=self.headers).status_code,400)
        r=self.client.get(url,headers={'Range':'bytes=0-3'});self.assertEqual(r.status_code,206);r.close()
        r=self.client.post(url,json={'reviewed':True},headers=self.headers);self.assertEqual(r.data,DATA);r.close()
        recovered=YouTubeTools(self.settings);self.assertTrue(recovered.snapshot(identifier)['resources'][key]['ready'])
        self.assertNotEqual(self.tool.root,self.app.extensions['bilibili_tools'].root)
        path,_,_=self.tool.file(identifier,key);path.write_bytes(b'x'*len(DATA))
        with self.assertRaises(ValueError):self.tool.file(identifier,key)
        with self.assertRaises(LookupError):self.tool.file(identifier,'../bad')

    def test_reuse_tracks_after_merge_failure_and_audio_without_video_download(self):
        identifier=self.resolved();merge=self.mocks[3].side_effect;self.mocks[3].side_effect=ValueError('隔离合并失败')
        self.tool.prepare(identifier,'video',V['id'],A['id']);self.assertEqual(self.wait(identifier)['phase'],'failed')
        count=self.mocks[1].call_count;self.mocks[3].side_effect=merge;self.tool.control(identifier,'resume')
        self.assertEqual(self.wait(identifier)['phase'],'prepared');self.assertEqual(count,self.mocks[1].call_count)
        second=self.resolved();self.tool.prepare(second,'audio',V['id'],A['id']);row=self.wait(second)
        self.assertEqual(row['phase'],'prepared');self.assertEqual(self.mocks[1].call_count,count+1)

    def test_settings_and_cleanup_protect_pause_and_keep_summary(self):
        identifier,key=self.prepared();row=self.tool._read(identifier);row.update(phase='paused',touched=0);self.tool._write(row)
        self.assertEqual(self.tool.cleanup(),0)
        self.tool.configure({'temp_dir':str(Path(self.temp.name)/'other'),'proxy':'http://127.0.0.1:7890','js_runtime':'node','cache_warn_gib':0,'idle_hours':1})
        self.assertEqual(self.tool._network().youtube['proxy'],'http://127.0.0.1:7890')
        self.assertEqual(YouTubeTools(self.settings).options['idle_hours'],1)
        self.tool.delete_cache(identifier);row=self.tool.snapshot(identifier)
        self.assertEqual(row['phase'],'cleared');self.assertTrue(row['attempts']);self.assertFalse(row['resources'][key]['ready'])

    def test_cover_formats_use_shared_converter(self):
        identifier=self.resolved();sources=self.tool._path(identifier)/'sources_video.json'
        values=json.loads(sources.read_text());values['cover']='https://i.ytimg.com/cover.jpg';sources.write_text(json.dumps(values))
        payload=BytesIO();Image.new('RGB',(20,10),'green').save(payload,format='JPEG')
        def image_download(urls,target,**kwargs):
            target.write_bytes(payload.getvalue());return transfer.fingerprint(target)
        self.mocks[1].side_effect=image_download
        key=self.tool.prepare(identifier,'cover',output_format='png');row=self.wait(identifier)
        self.assertTrue(row['resources'][key]['ready']);path,_,mime=self.tool.file(identifier,key)
        self.assertEqual(mime,'image/png')
        with Image.open(path) as image:self.assertEqual(image.size,(20,10))

    def test_other_process_is_not_treated_as_an_interrupted_download(self):
        from dataclasses import replace
        identifier=self.resolved();row=self.tool._read(identifier);row['phase']='downloading';self.tool._write(row)
        self.tool.workers.clear();self.tool.settings=replace(self.tool.settings,task_backend='mysql')
        task={'name':'tools.youtube.video','status':'running','owner_pid':123,'owner_process_started_at':'test-owner'}
        with patch.object(self.tool,'_task',return_value=task),patch('ecommerce.modules.video_tools.cache.owner_alive',return_value=True):
            with self.assertRaises(ValueError):self.tool._idle()
            self.assertEqual(self.tool.snapshot(identifier)['phase'],'waiting')
        with patch.object(self.tool,'_task',return_value=task),patch('ecommerce.modules.video_tools.cache.owner_alive',return_value=False):
            self.tool._idle()
            self.assertEqual(self.tool.snapshot(identifier)['phase'],'interrupted')


class YouTubeLocalMediaTests(unittest.TestCase):
    def test_webm_preserves_video_and_opus_audio_can_export_m4a(self):
        from ecommerce.infrastructure.media import local
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);v=root/'video.webm';a=root/'audio.webm';out=root/'merged.webm';m4a=root/'audio.m4a'
            local.run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=64x64:r=10','-t','1','-an','-c:v','libvpx-vp9',str(v)],root,lambda:None)
            local.run_ffmpeg(['-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:a','libopus',str(a)],root,lambda:None)
            local.merge(v,a,out,lambda:None,1,container='webm')
            local.inspect_media(out,lambda:None,duration=1,video=True,audio=True)
            local.extract_audio(out,m4a,lambda:None,1,copy=False)
            self.assertGreater(m4a.stat().st_size,1000)


if __name__=='__main__':unittest.main()
