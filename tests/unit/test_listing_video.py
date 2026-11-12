"""商品视频本地适配回归。"""
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ecommerce.infrastructure.media.local import run_ffmpeg
from ecommerce.modules.product.listing_video import ADAPTED_VIDEO, inspect_listing_video, prepare_listing_video


class ListingVideoTests(unittest.TestCase):
    def make_video(self, path, size, seconds=6, audio=False):
        inputs = ['-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=8']
        if audio:
            inputs += ['-f', 'lavfi', '-i', 'sine=frequency=400', '-c:a', 'aac']
        run_ffmpeg([*inputs, '-t', str(seconds), '-c:v', 'libx264', str(path)],
                   path.parent, lambda: None, timeout=30)

    def test_horizontal_copy_has_square_ratio_and_keeps_audio_and_duration(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / '原始视频.mp4'
            self.make_video(source, '160x90', audio=True)
            before = source.read_bytes()
            adapted = root / 'video_adapted' / ADAPTED_VIDEO
            self.assertEqual(prepare_listing_video(source, adapted), {
                'source_ratio': '16:9', 'upload_ratio': '1:1', 'duration': 6.0, 'adapted': True,
            })
            info = inspect_listing_video(adapted)
            self.assertEqual((info.width, info.height, info.audio), (1280, 1280, 'aac'))
            self.assertEqual(source.read_bytes(), before)

    def test_rotation_is_applied_before_ratio_check(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            rotated = root / 'rotated.mp4'
            self.make_video(source, '128x96')
            run_ffmpeg(['-display_rotation:v:0', '90', '-i', str(source), '-c', 'copy', str(rotated)],
                       root, lambda: None, timeout=30)
            self.assertEqual(inspect_listing_video(rotated).ratio, Fraction(3, 4))
            adapted = root / 'video_adapted' / ADAPTED_VIDEO
            self.assertEqual(prepare_listing_video(rotated, adapted), {
                'source_ratio': '3:4', 'upload_ratio': '3:4', 'duration': 6.0, 'adapted': False,
            })
            self.assertFalse(adapted.exists())

    def test_corrupt_or_short_videos_report_specific_preflight_errors(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            adapted = root / 'video_adapted' / ADAPTED_VIDEO
            source.write_bytes(b'not an mp4')
            with self.assertRaisesRegex(ValueError, '视频无法读取或解码'):
                prepare_listing_video(source, adapted)
            self.make_video(source, '64x64', seconds=1)
            with self.assertRaisesRegex(ValueError, '时长为 1 秒'):
                prepare_listing_video(source, adapted)
