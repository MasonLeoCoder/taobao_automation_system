"""人工确认题数后复用原数据发布：保留原件，拒绝不完整或被修改的批次。"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ecommerce.modules.collection.detail_questions import QuestionStore, confirm_questions_batch, export_questions, compact
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.shared.config import load_settings

ITEM_ID = '621242715247'
BATCH = 'a' * 32 + '-' + 'b' * 8


class QuestionsConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = load_settings(root=Path(self.temp.name))
        self.product = product_directory(self.settings, ITEM_ID)
        self.directory = self.product / 'history' / BATCH
        store = QuestionStore(self.directory, ITEM_ID)
        self.answer = {'index': 1, 'question_key': 'q1', 'answer_key': 'a1', 'answer_id': None, 'author': '匿名',
            'content': '可以，原文保持', 'date_raw': '近1个月发布', 'sku': '规格A', 'meta_raw': '近1个月发布 · 规格A',
            'identity_labels': ['已购'], 'purchase_labels': [], 'image_urls': [], 'video_urls': [], 'video_poster_urls': [],
            'unresolved_media': [], 'captured_at': '2026-10-22T06:00:00+00:00'}
        self.question = {'index': 1, 'product_id': ITEM_ID, 'question_key': 'q1', 'question_id': None, 'question': '问题原文',
            'author': None, 'date_raw': None, 'answer_count': 1, 'answers_complete': True, 'answer_stop_reason': 'expanded_all_visible',
            'captured_at': '2026-10-22T06:00:00+00:00'}
        with store.db:
            store.db.execute('INSERT INTO questions VALUES (?,?,?)', (1, 'q1', compact(self.question)))
            store.db.execute('INSERT INTO answers VALUES (?,?,?,?)', ('q1', 1, 'a1', compact(self.answer)))
        self.source = export_questions(store, self.directory, {'schema_version': 1, 'product_id': ITEM_ID, 'task_id': 'a' * 32,
            'batch_id': BATCH, 'status': 'partial', 'stop_reason': 'user_stopped', 'collected_count': 1, 'completed_question_count': 1,
            'answer_count': 1, 'question_count_display': {'raw': '64', 'value': 64}, 'question_count_difference': 63,
            'last_attention_reason': '连续3轮问答无新增，尚未确认末尾', 'end_confirmed': False})
        store.close()
        save_detail_section(self.product, ITEM_ID, 'questions_last_attempt', self.source)
        save_detail_section(self.product, ITEM_ID, 'parameters', {'items': ['保留参数']})

    def confirm(self, count=1):
        return confirm_questions_batch(self.settings, ITEM_ID, BATCH, confirmed_count=count, note='用户已人工清点，标题数量不正确')

    def test_manual_confirmation_publishes_without_changing_source_or_content(self):
        source_files = {p.name: p.read_bytes() for p in (self.directory / '问大家').iterdir()}
        source_record = (self.directory / 'questions.json').read_bytes()
        result = self.confirm()
        self.assertEqual((result['status'], result['stop_reason']), ('complete', 'manual_confirmation'))
        self.assertTrue(result['end_confirmed'])
        self.assertEqual(result['completion_evidence']['confirmed_question_count'], 1)
        self.assertEqual(result['question_count_display']['raw'], '64')
        self.assertEqual(result['question_count_difference'], 63)
        self.assertNotEqual(result['batch_id'], BATCH)
        published = json.loads((self.product / '问大家/问答明细.json').read_text(encoding='utf-8'))
        self.assertEqual(published['questions'], [{**self.question, 'answers': [self.answer]}])
        self.assertEqual((self.product / '问大家/问答明细.csv').read_bytes(), source_files['问答明细.csv'])
        self.assertEqual((self.directory / 'questions.json').read_bytes(), source_record)
        self.assertEqual({p.name: p.read_bytes() for p in (self.directory / '问大家').iterdir()}, source_files)
        root = json.loads((self.product / (ITEM_ID + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(root['parameters'], {'items': ['保留参数']})
        self.assertEqual(root['questions']['batch_id'], result['batch_id'])
        self.assertEqual(root['questions_last_attempt']['batch_id'], BATCH)

    def test_wrong_confirmed_count_cannot_publish(self):
        with self.assertRaises(ValueError):
            self.confirm(2)
        self.assertFalse((self.product / '问大家').exists())

    def test_modified_csv_cannot_publish(self):
        with (self.directory / '问大家/问答明细.csv').open('a', encoding='utf-8') as stream:
            stream.write('人为修改')
        with self.assertRaisesRegex(ValueError, '已修改'):
            self.confirm()
        self.assertFalse((self.product / '问大家').exists())

    def test_mismatched_database_cannot_publish(self):
        with closing(sqlite3.connect(self.directory / 'questions-progress.sqlite3')) as connection:
            changed = {**self.answer, 'content': '与原件不符'}
            connection.execute('UPDATE answers SET payload=?', (compact(changed),))
            connection.commit()
        with self.assertRaisesRegex(ValueError, '数据库与明细不一致'):
            self.confirm()
        self.assertFalse((self.product / '问大家').exists())

    def test_untrusted_or_incomplete_source_cannot_be_confirmed(self):
        for changes in ({'completeness_trusted': False}, {'completed_question_count': 0}, {'last_attention_reason': '未知入口'}):
            with self.subTest(changes=changes):
                (self.directory / 'questions.json').write_text(json.dumps({**self.source, **changes}, ensure_ascii=False), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.confirm()
        self.assertFalse((self.product / '问大家').exists())
