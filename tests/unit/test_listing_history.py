"""使用隔离数据验证两种入口及各店铺的入仓归属。"""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import sqlalchemy as sa

from ecommerce.cli.interaction import prompt_duplicate_listing
from ecommerce.modules.product.cli_listing import CliListing
from ecommerce.modules.product.listing_history import cli_listing_records, code_fingerprint, pending_submissions
from ecommerce.modules.product.web_listing import ListingWorkbench
from ecommerce.platforms.qianniu.product_submit import SubmissionResult
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped


class ListingHistoryTests(TestCase):
    def test_events_belong_to_the_execution_account_and_store(self):
        workspace = self.enterContext(TemporaryDirectory())
        engine = sa.create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        columns = sa.MetaData()
        tasks = sa.Table('tasks', columns, sa.Column('id', sa.String, primary_key=True),
                         sa.Column('name', sa.String), sa.Column('shop_id', sa.String),
                         sa.Column('started_at', sa.DateTime))
        events = sa.Table('task_events', columns, sa.Column('task_id', sa.String),
                          sa.Column('task_sequence', sa.Integer), sa.Column('event_type', sa.String),
                          sa.Column('occurred_at', sa.DateTime), sa.Column('details', sa.JSON))
        columns.create_all(engine)
        settings = SimpleNamespace(task_backend='mysql', task_database=SimpleNamespace(engine=engine, tables=columns.tables),
                                   workspace=Path(workspace))
        when = datetime(2026, 11, 15, 9, 30)
        identifiers = {key: digit * 32 for key, digit in zip(('pending', 'other_shop', 'confirmed', 'navigation', 'web'), 'abcde')}
        with engine.begin() as connection:
            for key, task_id in identifiers.items():
                shop = 'B' if key == 'other_shop' else 'A'
                account = 'taobao010:大橘' if key == 'pending' else 'taobao010'
                connection.execute(tasks.insert().values(id=task_id, name='product.open', shop_id=shop, started_at=when))
                source = {'shop_id': shop, 'login_name': account, 'shop_name': f'{shop} 店', 'platform_shop_id': shop}
                details = {'entry': 'web' if key == 'web' else 'cli', 'title': f'商品 {key}',
                           'code': 'secret=[已隐藏]', 'code_sha256': code_fingerprint('secret:TEST')}
                values = [('task_started', {'qianniu_source': source})]
                if key != 'navigation':
                    values += [('listing_filled', details), ('listing_submit_intent', details)]
                if key == 'confirmed':
                    values += [('listing_warehouse', {**details, 'item_id': '123456'})]
                connection.execute(events.insert(), [dict(task_id=task_id, task_sequence=sequence,
                    event_type=event, occurred_at=when, details=data) for sequence, (event, data) in enumerate(values, 1)])

        records = cli_listing_records(settings)
        self.assertEqual(records.keys(), {identifiers[key] for key in ('pending', 'other_shop', 'confirmed')})
        self.assertEqual(records[identifiers['pending']]['target']['login_name'], 'taobao010:大橘')
        self.assertEqual(records[identifiers['pending']]['outcome'], 'uncertain')
        self.assertIsNone(records[identifiers['pending']]['item_id'])
        self.assertEqual(records[identifiers['confirmed']]['item_id'], '123456')
        self.assertEqual(records[identifiers['confirmed']]['outcome'], 'warehouse')
        self.assertEqual(list(cli_listing_records(settings, task_id=identifiers['pending'])), [identifiers['pending']])

        web = {'task_id': identifiers['web'], 'entry': 'web', 'outcome': 'uncertain', 'created_at': '',
               'code': 'secret:TEST', 'target': {'shop_id': 'A', 'login_name': 'taobao010'}}
        with patch('ecommerce.modules.product.listing_history.listing_records', return_value=[web]):
            self.assertEqual(pending_submissions(settings, 'A', 'secret:TEST', exclude_task=identifiers['web']),
                             [identifiers['pending']])
            self.assertEqual(pending_submissions(settings, 'A', 'secret:TEST', exclude_task=identifiers['pending']),
                             [identifiers['web']])
            self.assertEqual(pending_submissions(settings, 'B', 'secret:TEST', exclude_task=None),
                             [identifiers['other_shop']])
            self.assertEqual(pending_submissions(settings, 'A', 'secret:test', exclude_task=None), [])
        workbench = object.__new__(ListingWorkbench)
        workbench.settings, workbench.root = settings, settings.workspace
        with patch('ecommerce.modules.product.web_listing.listing_records', return_value=[web]):
            rows, _ = workbench.history({'entry': 'cli', 'shop': 'A 店', 'account': '大橘'}, 1)
            self.assertEqual([row['task_id'] for row in rows], [identifiers['pending']])
            rows, _ = workbench.history({'entry': 'web'}, 1)
            self.assertEqual(rows, [web])

    def test_cli_requires_review_and_records_only_an_actual_receipt(self):
        events = Mock(task_id='a' * 32)
        identity = SimpleNamespace(login_name='taobao010:大橘', shop_id='100')
        page = Mock()
        with (patch('ecommerce.modules.product.cli_listing.current_events', return_value=events),
              patch('ecommerce.modules.product.cli_listing.verify_editor_session', return_value=identity),
              patch('ecommerce.modules.product.cli_listing.read_product_fields', return_value={'title': '商品', 'code': 'ABC'}) as read,
              patch('ecommerce.modules.product.cli_listing.pending_submissions', return_value=['b' * 32]),
              patch('ecommerce.modules.product.cli_listing.storage_checkpoint') as checkpoint):
            journal = CliListing(SimpleNamespace(port=8001), {'shop_id': 'A'}, Mock(), None)
            journal.filled(page)
            self.assertEqual(events.emit.call_args.args[0], 'listing_filled')
            with self.assertRaisesRegex(NeedsAttention, '/tasks/' + 'b' * 32):
                journal.before_click(page)
            events.append.assert_not_called()
            review = Mock()
            journal.on_duplicate_review = review
            read.side_effect = [{'title': '商品', 'code': 'ABC'}, {'title': '商品', 'code': 'CHANGED'}]
            with self.assertRaisesRegex(NeedsAttention, '发生变化'):
                journal.before_click(page)
            read.side_effect = None
            journal.before_click(page)
            self.assertIn('http://localhost:8001/tasks/', review.call_args.args[0])
            journal.click_intent()
            self.assertIs(events.collection_barrier, events.append.return_value)
            checkpoint.assert_called_once()
            with self.assertRaises(NeedsAttention):
                journal.click_intent()
            journal.result(SubmissionResult('unknown', '未确认', page))
            self.assertEqual(events.emit.call_count, 1)
            journal.result(SubmissionResult('succeeded', '入仓成功', page, item_id='123456'))
            self.assertEqual(events.emit.call_args.args[0], 'listing_warehouse')
            self.assertEqual(events.emit.call_args.kwargs['details']['item_id'], '123456')

    def test_terminal_requires_specific_approval_or_cancel(self):
        with (patch('ecommerce.cli.interaction._play_attention_sound'), patch('ecommerce.cli.interaction.terminal_print'),
              patch('ecommerce.cli.interaction.print'), patch('ecommerce.cli.interaction._manual_state'),
              patch('builtins.input', side_effect=['继续', '继续入仓']) as feedback):
            prompt_duplicate_listing('待核对')
            self.assertEqual(feedback.call_count, 2)
            feedback.side_effect = ['取消']
            with self.assertRaises(TaskStopped):
                prompt_duplicate_listing('待核对')
            feedback.side_effect = EOFError
            with self.assertRaises(NeedsAttention):
                prompt_duplicate_listing('待核对')
