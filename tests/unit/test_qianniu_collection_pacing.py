"""采集节奏和限流截止时间只用本地模拟验证。"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from ecommerce.platforms.qianniu import collection_pacing as pacing
from ecommerce.shared.exceptions import NeedsAttention


class CollectionPacingTests(TestCase):
    def test_wait_uses_config_and_checks_guard_in_segments(self):
        settings = SimpleNamespace(taobao_timing={'page': (4, 4)})
        context = Mock()
        page = Mock()
        now = 0

        def wait(ms):
            nonlocal now
            now += ms / 1000

        page.wait_for_timeout.side_effect = wait
        with pacing.collection_pacing(settings, context), patch.object(pacing, 'monotonic', side_effect=lambda: now), patch(
                'ecommerce.platforms.qianniu.verification.checkpoint') as checkpoint:
            pacing.pause(page, 'page')
        self.assertEqual(now, 4)
        self.assertEqual(page.wait_for_timeout.call_count, 16)
        self.assertEqual(checkpoint.call_count, 17)

    def test_rate_limit_is_scoped_and_survives_new_task(self):
        with TemporaryDirectory() as path, patch.object(pacing, 'time', return_value=1000):
            settings = SimpleNamespace(profile_path=Path(path))
            context = Mock()
            with self.assertRaises(NeedsAttention), pacing.collection_pacing(settings, context):
                callback = context.on.call_args.args[1]
                for host, kind in [('sycm.taobao.com', 'image'), ('example.com', 'xhr')]:
                    callback(SimpleNamespace(url=f'https://{host}/', status=429, headers={},
                                             request=SimpleNamespace(resource_type=kind)))
                pacing.checkpoint_rate()
                callback(SimpleNamespace(url='https://sycm.taobao.com/mc/', status=429, headers={'retry-after': '120'},
                                         request=SimpleNamespace(resource_type='xhr')))
                pacing.checkpoint_rate()
            with self.assertRaises(NeedsAttention):
                pacing.check_cooldown(settings)
            with patch.object(pacing, 'time', return_value=1121):
                pacing.check_cooldown(settings)

    def test_http_date_and_missing_retry_after(self):
        now = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
        self.assertEqual(pacing._retry_deadline('Fri, 06 Nov 2026 00:03:00 GMT', now), now + 180)
        self.assertEqual(pacing._retry_deadline(None, now), now + 60)
