"""B站输入、真实范围确认和缓存状态的隔离测试，不请求实际平台。"""
import copy
import json
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ecommerce.infrastructure.media import transfer
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.video_tools.bilibili import BilibiliTools, resource_key, filename
from ecommerce.platforms.bilibili.links import parse_input
from ecommerce.platforms.bilibili.video import media_url, tracks, resolve
from ecommerce.shared.config import load_settings, bilibili_options
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.web.app import create_app

BV = 'BV1jBHA68ERy'
OTHER = 'BV1mVPzzDESJ'
EP = {'id': BV + '_p1', 'bvid': BV, 'cid': 101, 'page': 1, 'title': '第一节', 'submission_title': '测试课程', 'duration': 12, 'cover': '', 'url': 'https://www.bilibili.com/video/' + BV}
EP2 = {**EP, 'id': OTHER + '_p1', 'bvid': OTHER, 'cid': 102, 'title': '第二节', 'url': 'https://www.bilibili.com/video/' + OTHER}
V = {'id': 'v32_avc1', 'quality': 32, 'codec': 'avc1', 'width': 854, 'height': 480, 'fps': '30', 'bitrate': 2000, 'bytes': None, 'estimated_bytes': 3000}
A = {'id': 'a30280_mp4a', 'quality': 30280, 'codec': 'mp4a', 'width': 0, 'height': 0, 'fps': '', 'bitrate': 1000, 'bytes': None, 'estimated_bytes': 1500}
META = {'title': '隔离样例', 'bvid': BV, 'author': '测试', 'published': None, 'parsed_at': '2026-11-02T12:00:00+08:00', 'current': EP['id'], 'parts': [EP], 'collection': {'title': '测试合集', 'declared_count': 2, 'items': [EP, EP2]}, 'stats': {'like': 0}, 'url': EP['url']}
DATA = b'\x00\x00\x00\x18ftypmp42-test-data'


class BilibiliInputTests(unittest.TestCase):
    def test_supported_inputs_and_tracking(self):
        for value in (BV, 'https://m.bilibili.com/video/' + BV + '?p=2&spm_id_from=abc', '[https://www.bilibili.com/video/' + BV + '/?p=2](https://www.bilibili.com/video/' + BV + '/?p=2)'):
            parsed = parse_input(value)
            self.assertEqual(parsed.identity, BV)
            self.assertNotIn('spm', parsed.url)
        self.assertEqual(parse_input('av123').identity, 'av123')
        self.assertIsNone(parse_input('分享 https://b23.tv/AbC123 打开B站').identity)

    def test_reject_ambiguous_and_external(self):
        for value in ('', 'https://evil.test/video/' + BV, 'http://www.bilibili.com/video/' + BV,
                      'https://www.bilibili.com.evil.test/video/' + BV, 'https://user@www.bilibili.com/video/' + BV,
                      'https://www.bilibili.com/video/' + BV + '?p=1&p=2', 'https://www.bilibili.com/video/' + BV + '?p=0',
                      'https://www.bilibili.com/video/' + BV + '?bvid=' + OTHER, EP['url'] + ' ' + EP2['url']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_input(value)

    def test_media_whitelist_and_options(self):
        self.assertTrue(media_url('https://upos-sz-mirror.bilivideo.com/v.m4s'))
        for value in ('https://127.0.0.1/video', 'https://bilivideo.com.evil.test/v', 'https://user@a.bilivideo.com/v', 'http://a.bilivideo.com/v'):
            self.assertFalse(media_url(value))
        self.assertEqual(bilibili_options({}, Path.cwd())['cache_warn_gib'], 20)
        for values in ({'cache_warn_gib': -1}, {'idle_hours': 0}, {'idle_hours': True}, {'video_max_gib': 5}):
            with self.assertRaises(ValueError):
                bilibili_options(values, Path.cwd())
        self.assertNotEqual(resource_key(EP['id'], 'video', 'v', 'a'), resource_key(EP2['id'], 'video', 'v', 'a'))
        self.assertNotIn('/', filename({**EP, 'title': 'a/b:*?'}, 'video', V))

    def test_platform_distinguishes_collection_and_parts(self):
        view = {'bvid': BV, 'aid': 1, 'title': '课程', 'pages': [{'cid': 101, 'page': 1, 'part': '第一节', 'duration': 12}, {'cid': 102, 'page': 2, 'part': '第二节', 'duration': 12}],
                'owner': {}, 'stat': {'like': 0}, 'ugc_season': {'title': '合集', 'ep_count': 1, 'sections': [{'episodes': [{'bvid': BV, 'cid': 101, 'title': '课程', 'arc': {'duration': 24}, 'page': {'page': 1, 'duration': 12}}]}]}}
        with patch('ecommerce.platforms.bilibili.video.api', return_value=view):
            meta, _ = resolve(None, parse_input(EP['url'] + '?p=2'), lambda: None)
        self.assertEqual(meta['current'], BV + '_p2')
        self.assertEqual(len(meta['parts']), 2)
        self.assertEqual(len(meta['collection']['items']), 1)
        self.assertEqual(meta['stats']['like'], 0)
        self.assertIsNone(meta['stats']['view'])

    def test_reject_trial_and_changed_identity(self):
        view = {'bvid': BV, 'pages': [{'page': 1, 'cid': 101, 'duration': 12}]}
        for play in ({'timelength': 12000, 'is_preview': 1}, {'timelength': 5000}):
            with patch('ecommerce.platforms.bilibili.video.api', side_effect=[view, play]), self.assertRaises(ValueError):
                tracks(None, EP, lambda: None)
        with patch('ecommerce.platforms.bilibili.video.api', return_value={**view, 'pages': [{'page': 1, 'cid': 999, 'duration': 12}]}), self.assertRaises(ValueError):
            tracks(None, EP, lambda: None)


class Response(BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.url = 'https://a.bilivideo.com/video'
        self.status = status
        self.headers = headers or {'Content-Length': str(len(data))}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'video.part'
        self.requests = []

    def run_download(self, response, checkpoint=lambda: None, source='https://a.bilivideo.com/video'):
        class Opener:
            def open(inner, request, timeout):
                self.requests.append(request)
                return response
        return transfer.download([source], self.path, identity='same-video', allowed=media_url, checkpoint=checkpoint,
                                 progress=lambda *args: None, referer='https://www.bilibili.com/', reserve=0, opener=Opener())

    def partial(self):
        self.path.write_bytes(DATA[:5])
        atomic_json(self.path.with_suffix('.part.json'), {'identity': 'same-video', 'url': 'https://a.bilivideo.com/video', 'etag': 'same-etag'})

    def test_true_range_resume(self):
        self.partial()
        size, _ = self.run_download(Response(DATA[5:], 206, {'Content-Range': f'bytes 5-{len(DATA)-1}/{len(DATA)}', 'ETag': 'same-etag'}))
        self.assertEqual(self.path.read_bytes(), DATA)
        self.assertEqual(size, len(DATA))
        self.assertEqual(self.requests[0].get_header('Range'), 'bytes=5-')

    def test_range_ignored_restarts(self):
        self.partial()
        self.run_download(Response(DATA))
        self.assertEqual(self.path.read_bytes(), DATA)
        self.partial()
        atomic_json(self.path.with_suffix('.part.json'), {'identity': 'same-video', 'url': 'https://a.bilivideo.com/video'})
        self.run_download(Response(DATA))
        self.assertIsNone(self.requests[-1].get_header('Range'))
        self.assertEqual(self.path.read_bytes(), DATA)

    def test_range_mismatch_rejected(self):
        self.partial()
        with self.assertRaises(ValueError):
            self.run_download(Response(DATA[5:], 206, {'Content-Range': f'bytes 0-{len(DATA)-1}/{len(DATA)}', 'ETag': 'same-etag'}))
        self.assertEqual(self.path.read_bytes(), DATA[:5])

    def test_source_etag_changed_does_not_append(self):
        self.partial()
        with self.assertRaises(ValueError):
            self.run_download(Response(DATA[5:], 206, {'Content-Range': f'bytes 5-{len(DATA)-1}/{len(DATA)}', 'ETag': 'changed'}))
        self.assertFalse(self.path.exists())

    def test_five_gib_is_not_a_hard_limit_and_disk_is_checked(self):
        with patch.object(transfer, 'ensure_space') as space, self.assertRaises(ValueError):
            self.run_download(Response(DATA, headers={'Content-Length': str(6 * 1024 ** 3)}))
        self.assertGreaterEqual(space.call_args_list[0].args[1], 6 * 1024 ** 3)
        with patch.object(transfer.shutil, 'disk_usage') as usage:
            usage.return_value.free = 1
            with self.assertRaisesRegex(ValueError, '空间不足'):
                transfer.ensure_space(self.path.parent, 10)

    def test_pause_keeps_partial(self):
        self.partial()
        with self.assertRaises(NeedsAttention):
            self.run_download(Response(DATA), checkpoint=lambda: (_ for _ in ()).throw(NeedsAttention('暂停')))
        self.assertTrue(self.path.exists())


class BilibiliServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = load_settings(root=Path(self.temp.name))
        self.app = create_app(self.settings)
        self.tool = self.app.extensions['bilibili_tools']
        self.client = self.app.test_client()
        self.headers = {'X-Collection-Token': self.app.config['COLLECTION_TOKEN']}
        self.patches = [patch('ecommerce.modules.video_tools.bilibili.platform.resolve', return_value=(copy.deepcopy(META), {})),
                        patch('ecommerce.modules.video_tools.bilibili.platform.tracks', side_effect=self.catalog),
                        patch('ecommerce.modules.video_tools.bilibili.transfer.download', side_effect=self.download),
                        patch('ecommerce.modules.video_tools.bilibili.local.inspect_media'),
                        patch('ecommerce.modules.video_tools.bilibili.local.merge', side_effect=lambda v, a, out, *args: out.write_bytes(DATA)),
                        patch('ecommerce.modules.video_tools.bilibili.local.extract_audio', side_effect=lambda src, out, *args, **kw: out.write_bytes(DATA))]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def catalog(self, settings, episode, checkpoint):
        checkpoint()
        return {'videos': [dict(V)], 'audios': [dict(A)], 'duration': 12, 'created': time.time(), 'episode': episode, 'permission_note': '隔离测试'}, {V['id']: ['https://a.bilivideo.com/video'], A['id']: ['https://a.bilivideo.com/audio']}

    def download(self, sources, target, **kwargs):
        kwargs['checkpoint']()
        target.write_bytes(DATA)
        kwargs['progress'](len(DATA), len(DATA), 100, '')
        return transfer.fingerprint(target)

    def wait(self, identifier):
        self.tool.workers[identifier][0].join(12)
        self.assertFalse(self.tool.workers[identifier][0].is_alive())
        return self.tool.snapshot(identifier)

    def resolved(self):
        identifier = self.tool.start(EP['url'])
        self.assertEqual(self.wait(identifier)['phase'], 'resolved')
        return identifier

    def prepare(self, identifier, **kwargs):
        return self.tool.prepare(identifier, episode_id=EP['id'], video_id=V['id'], audio_id=A['id'], **kwargs)

    def test_page_and_protection(self):
        self.assertEqual(self.client.get('/tools/bilibili').status_code, 200)
        self.assertIn(b'/tools/bilibili', self.client.get('/tools').data)
        self.assertEqual(self.client.post('/api/tools/bilibili', json={'input': BV}).status_code, 403)
        self.assertEqual(self.client.post('/api/tools/bilibili', json={'input': BV}, headers={**self.headers, 'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/tools/bilibili', base_url='http://evil.test').status_code, 403)

    def test_parse_does_not_download_and_single_is_default(self):
        identifier = self.resolved()
        self.mocks[2].assert_not_called()
        self.assertEqual(self.tool.snapshot(identifier)['plan'], [])
        key = self.prepare(identifier)
        row = self.wait(identifier)
        self.assertEqual(len(row['plan']), 1)
        self.assertTrue(row['resources'][key]['ready'])
        self.assertEqual(row['metadata']['stats']['like'], 0)

    def test_confirmed_collection_uses_independent_tasks_and_reuses(self):
        identifier = self.resolved()
        self.prepare(identifier, scope='collection')
        row = self.wait(identifier)
        self.assertEqual([p['state'] for p in row['plan']], ['prepared', 'prepared'])
        self.assertEqual(len({a['task_id'] for a in row['attempts']}), 3)
        count = self.mocks[2].call_count
        self.tool.control(identifier, 'resume')
        self.wait(identifier)
        self.assertEqual(count, self.mocks[2].call_count)

    def test_pause_resume_new_attempt_and_cancel_partial_only(self):
        identifier = self.resolved()
        entered = threading.Event()
        def downloading(sources, target, **kwargs):
            target.write_bytes(DATA[:5]); entered.set()
            while True:
                time.sleep(.01); kwargs['checkpoint']()
        self.mocks[2].side_effect = downloading
        self.prepare(identifier)
        self.assertTrue(entered.wait(4))
        self.tool.control(identifier, 'pause')
        row = self.wait(identifier)
        self.assertEqual(row['phase'], 'paused')
        self.assertTrue(list(self.tool._cache(self.tool._read(identifier)).glob('*.part')))
        self.assertFalse(any(a['ready'] for a in row['resources'].values()))
        before = len(row['attempts'])
        self.mocks[2].side_effect = self.download
        self.tool.control(identifier, 'resume')
        row = self.wait(identifier)
        self.assertEqual(row['phase'], 'prepared')
        self.assertEqual(len(row['attempts']), before + 1)
        cache = self.tool._cache(self.tool._read(identifier))
        (cache / 'unfinished.part').write_bytes(b'partial')
        row = self.tool._read(identifier); row['phase'] = 'paused'; self.tool._write(row)
        self.tool.control(identifier, 'cancel')
        self.assertFalse((cache / 'unfinished.part').exists())
        self.assertTrue(list(cache.glob('*.track')))

    def test_no_silent_quality_downgrade_and_retry_completed_skipped(self):
        identifier = self.resolved()
        real_catalog = self.catalog
        def different(settings, episode, checkpoint):
            catalog, sources = real_catalog(settings, episode, checkpoint)
            if episode['id'] == EP2['id']:
                catalog['videos'][0].update(id='v16_avc1', quality=16)
            return catalog, sources
        self.mocks[1].side_effect = different
        self.prepare(identifier, scope='collection')
        row = self.wait(identifier)
        self.assertEqual(row['phase'], 'partial')
        self.assertEqual(row['plan'][0]['state'], 'prepared')
        self.assertEqual(row['plan'][1]['state'], 'failed')
        self.mocks[1].side_effect = real_catalog
        count = self.mocks[2].call_count
        self.tool.control(identifier, 'resume')
        self.assertEqual(self.wait(identifier)['phase'], 'prepared')
        self.assertEqual(self.mocks[2].call_count - count, 2)

    def test_hash_guard_review_range_and_persistent_summary(self):
        identifier = self.resolved(); key = self.prepare(identifier); self.wait(identifier)
        url = '/api/tools/bilibili/' + identifier + '/assets/' + key
        self.assertEqual(self.client.post(url, json={'reviewed': False}, headers=self.headers).status_code, 400)
        response = self.client.get(url, headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206); response.close()
        response = self.client.post(url, json={'reviewed': True}, headers=self.headers)
        self.assertEqual(response.data, DATA); response.close()
        recovered = BilibiliTools(self.settings)
        self.assertTrue(recovered.snapshot(identifier)['resources'][key]['ready'])
        path, _, _ = self.tool.file(identifier, key); path.write_bytes(b'x' * len(DATA))
        with self.assertRaises(ValueError):
            self.tool.file(identifier, key)
        self.tool.delete_cache(identifier)
        self.assertEqual(self.tool.snapshot(identifier)['phase'], 'cleared')
        self.assertTrue(self.tool.snapshot(identifier)['attempts'])

    def test_settings_new_records_and_pause_protected_from_cleanup(self):
        identifier = self.resolved()
        row = self.tool._read(identifier); row.update(phase='paused', touched=0); self.tool._write(row)
        self.assertEqual(self.tool.cleanup(), 0)
        original = self.tool._cache(row)
        self.tool.configure({'temp_dir': str(Path(self.temp.name) / 'other'), 'cache_warn_gib': 0, 'idle_hours': 1})
        new = self.resolved()
        self.assertNotEqual(original.parent, self.tool._cache(self.tool._read(new)).parent)
        self.assertTrue(original.is_dir())
        self.assertEqual(BilibiliTools(self.settings).options['idle_hours'], 1)

    def test_corrupt_or_incomplete_selection_and_schema_validation(self):
        identifier = self.resolved()
        with self.assertRaises(ValueError):
            self.tool.prepare(identifier, episode_id=EP['id'], video_id='wrong')
        for body in ({'action': 'prepare', 'episode_id': []}, {'action': 'pause', 'unexpected': 'x'}, {'action': 'select', 'episode_id': 'nope'}):
            response = self.client.post('/api/tools/bilibili/' + identifier, json=body, headers=self.headers)
            self.assertIn(response.status_code, (400, 422))
        with self.assertRaises(LookupError):
            self.tool.snapshot('../outside')

    def test_multi_p_all_requires_explicit_scope(self):
        identifier = self.resolved()
        row = self.tool._read(identifier)
        second = {**EP, 'id': BV + '_p2', 'cid': 103, 'page': 2, 'title': '分P第二节'}
        row['metadata']['parts'].append(second)
        self.tool._write(row)
        self.prepare(identifier, scope='parts')
        result = self.wait(identifier)
        self.assertEqual([p['episode_id'] for p in result['plan']], [EP['id'], second['id']])
        self.assertEqual(result['phase'], 'prepared')

    def test_completed_track_survives_merge_failure_and_is_reused(self):
        identifier = self.resolved()
        original = self.mocks[4].side_effect
        self.mocks[4].side_effect = ValueError('隔离模拟合并失败')
        self.prepare(identifier)
        self.assertEqual(self.wait(identifier)['phase'], 'partial')
        count = self.mocks[2].call_count
        self.mocks[4].side_effect = original
        self.tool.control(identifier, 'resume')
        self.assertEqual(self.wait(identifier)['phase'], 'prepared')
        self.assertEqual(count, self.mocks[2].call_count)


class LocalMediaTests(unittest.TestCase):
    def test_real_stream_copy_merge_and_audio(self):
        from ecommerce.infrastructure.media import local
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v, a, merged, audio = [root / n for n in ('v.mp4', 'a.m4a', 'merged.mp4', 'audio.m4a')]
            local.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=green:s=64x64:r=10', '-t', '1', '-an', '-c:v', 'libx264', str(v)], root, lambda: None)
            local.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1', '-c:a', 'aac', str(a)], root, lambda: None)
            local.merge(v, a, merged, lambda: None, 1)
            local.extract_audio(merged, audio, lambda: None, 1)
            self.assertTrue(merged.stat().st_size > v.stat().st_size)
            self.assertTrue(audio.stat().st_size > 1000)
            with self.assertRaisesRegex(ValueError, '时长'):
                local.inspect_media(merged, lambda: None, duration=30, video=True, audio=True)


if __name__ == '__main__':
    unittest.main()
