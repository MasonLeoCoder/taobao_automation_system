"""抖音单条输入边界；全部样例均不访问网络。"""
import unittest
from ecommerce.platforms.douyin.links import parse_input, video_filename
from ecommerce.platforms.douyin.video import playback_sources, safe_playback_url


class DouyinLinksTests(unittest.TestCase):
    def test_user_modal(self):
        result = parse_input('https://www.douyin.com/user/self?from_tab_name=main&modal_id=7437582846610509092&showTab=like')
        self.assertEqual(result.video_id, '7437582846610509092')
        self.assertEqual(result.url, 'https://www.douyin.com/video/7437582846610509092')

    def test_share_text(self):
        result = parse_input('复制分享文案 https://v.douyin.com/AbCd123/ 打开抖音')
        self.assertIsNone(result.video_id)
        self.assertEqual(result.url, 'https://v.douyin.com/AbCd123/')

    def test_main_site_modal_is_independent_of_page_path(self):
        for host in ('www.douyin.com', 'douyin.com'):
            for path in ('/jingxuan', '/', '/search/武术', '/user/self'):
                with self.subTest(host=host, path=path):
                    result = parse_input(f'https://{host}{path}?modal_id=7689478797011550547')
                    self.assertEqual(result.video_id, '7689478797011550547')
                    self.assertEqual(result.url, 'https://www.douyin.com/video/7689478797011550547')

    def test_direct(self):
        self.assertEqual(parse_input('https://www.douyin.com/video/7437582846610509092?x=1').video_id, '7437582846610509092')

    def test_legacy_share(self):
        self.assertEqual(parse_input('https://www.iesdouyin.com/share/video/7437582846610509092/').video_id, '7437582846610509092')

    def test_multiple_rejected(self):
        with self.assertRaises(ValueError):
            parse_input('https://v.douyin.com/one/ https://v.douyin.com/two/')

    def test_unsafe_and_missing(self):
        for value in ['', 'https://evil.example/video/7437582846610509092',
                      'https://www.douyin.com.evil.example/video/7437582846610509092',
                      'https://user@www.douyin.com/video/7437582846610509092',
                      'https://www.douyin.com:8443/video/7437582846610509092',
                      'https://www.douyin.com/user/self', 'https://v.douyin.com/a/b/',
                      'https://www.douyin.com/user/self?modal_id=abc',
                      'https://www.douyin.com/jingxuan?modal_id=',
                      'https://www.douyin.com/jingxuan?modal_id=&modal_id=7689478797011550547',
                      'https://www.douyin.com/user/self?modal_id=7437582846610509092&modal_id=7437582846610509092']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_input(value)

    def test_conflicting_identity(self):
        with self.assertRaises(ValueError):
            parse_input('https://www.douyin.com/video/7437582846610509092?modal_id=7437582846610509093')

    def test_filename(self):
        self.assertEqual(video_filename('a/b:c? ', '7437582846610509092'), 'a_b_c__7437582846610509092.mp4')
        self.assertEqual(video_filename('', '7437582846610509092'), '抖音视频_7437582846610509092.mp4')
        self.assertLess(len(video_filename('长'*1000, '7437582846610509092')), 120)

    def test_explicit_watermark_and_foreign_hosts_rejected(self):
        for url in ['https://v11.douyinvod.com/video/logo/x',
                    'https://www.douyin.com/aweme/v1/play/?watermark=0',
                    'https://v11.douyinvod.com/video/x?logo_type=1',
                    'https://evil.example/video/x',
                    'https://v11.douyinvod.com.evil.example/x']:
            self.assertFalse(safe_playback_url(url), url)

    def test_playback_source_is_not_proof_of_clean_picture(self):
        detail = {'aweme_id': '7437582846610509092', 'video': {
            'has_watermark': True,
            'play_addr': {'url_list': ['https://v11.douyinvod.com/video/source'], 'width': 720, 'height': 1280},
            'download_addr': {'url_list': ['https://v11.douyinvod.com/video/logo/download']}}}
        result = playback_sources(detail, '7437582846610509092')
        self.assertEqual(len(result), 1)
        self.assertNotIn('watermark_free', result[0])
        with self.assertRaises(ValueError):
            playback_sources(detail, '7557891045083909439')

    def test_download_only_and_overlap_rejected(self):
        source = {'url_list': ['https://v11.douyinvod.com/video/source']}
        with self.assertRaises(ValueError):
            playback_sources({'aweme_id': '7437582846610509092', 'video': {
                'play_addr': source, 'download_addr': source}}, '7437582846610509092')


if __name__ == '__main__':
    unittest.main()
