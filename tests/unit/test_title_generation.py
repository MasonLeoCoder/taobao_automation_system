"""标题生成的归属、来源标注及单次付费保护。"""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from ecommerce.modules.product.title_generation import _candidates, _product, _sku_candidates, evidence
from ecommerce.modules.product.web_materials import CATEGORY
from ecommerce.web.app import create_app
from tests.unit.test_web_listing import fixture_materials, fixture_settings


BATCH = 'a' * 32
OLDER = 'b' * 32
RANKED = [('猫毛清理器', 1), ('猫毛', 5), ('除毛刷', 8)]


def fake_ranking(settings, category_id, period, batch_id, *, word_type):
    assert (category_id, period, word_type) == (7, '7d', None)
    latest = dict(id=BATCH, sycm_category_path=CATEGORY, publish_category_path=CATEGORY,
                  status='complete', requested_pages=2, captured_pages=Decimal(2), has_rows=True,
                  started_at=datetime(2026, 11, 6, 15), period_start=date(2026, 10, 30), period_end=date(2026, 11, 5))
    older = dict(latest, id=OLDER, sycm_category_path=CATEGORY.rsplit(' > ', 1)[0], requested_pages=10,
                 captured_pages=10, started_at=datetime(2026, 11, 6, 14))
    return {'history': [latest, older], 'batch': older if batch_id == OLDER else latest,
            'types': {'search': dict(collected_pages=2, row_count=3, status='complete')},
            'entries': [dict(id=index, keyword=word, rank=rank, word_type='search',
                             page_no=1, row_no=index, popularity_raw='300 ~ 600',
                             click_rate_raw='106%', payment_conversion_raw='25% ~ 30%')
                        for index, (word, rank) in enumerate(RANKED, 1)]}


class TitleGenerationTests(TestCase):
    def test_source_preview_confirm_reuse_and_reject_wrong_batch_inputs(self):
        temporary = self.enterContext(TemporaryDirectory())
        root = Path(temporary)
        folder = fixture_materials(root)
        app = create_app(fixture_settings(root))
        client = app.test_client()
        headers = {'X-Collection-Token': app.config['COLLECTION_TOKEN']}
        data = client.post('/api/listing/preflight', headers=headers, data={
            'keyword': '粘毛器', 'brand': '无品牌/无注册商标', 'category': CATEGORY,
            'excel': (BytesIO((folder / '任意表名.xlsx').read_bytes()), '任意表名.xlsx'),
            'main': [(BytesIO(path.read_bytes()), path.name) for path in sorted((folder / '1-1主图').iterdir())],
        }).json
        workbench = app.extensions['product_listing']
        workbench.settings = replace(workbench.settings, collection_backend='mysql')
        manifest = workbench.root / data['id'] / 'manifest.json'
        sealed = json.loads(manifest.read_text(encoding='utf-8'))
        sealed['category_binding'] = {'id': 7}
        manifest.write_text(json.dumps(sealed), encoding='utf-8')
        url = f'/api/listing/{data["id"]}/titles'
        module = 'ecommerce.modules.product.title_generation.'
        with (patch(module + 'check_binding'),
              patch(module + 'sycm_search.browse', side_effect=fake_ranking),
              patch(module + 'api_key', return_value='fake'),
              patch(module + 'create_client') as create):
            words = client.get(url + '/source').json
            self.assertEqual(words['batch']['id'], BATCH)
            self.assertEqual([row['id'] for row in words['history']], [BATCH, OLDER])
            self.assertEqual(words['types']['search']['collected_pages'], 2)
            body = dict(period='7d', batch=BATCH, selected=[1, 2, 3], locked={'title':[1], 'guide':[]},
                        titles={'title': '旧标题', 'guide': ''}, facts='', allow_mismatch=False)
            for change in ({'selected':[99]}, {'batch':OLDER}, {'locked':{'title':[99], 'guide':[]}}):
                self.assertEqual(client.post(url + '/preview', headers=headers, json={**body, **change}).status_code, 422)
            preview = client.post(url + '/preview', headers=headers, json=body).json
            self.assertNotIn('价格', preview['plan']['request'])
            self.assertEqual(preview['plan']['groups'], 5)
            self.assertEqual(preview['plan']['input']['sku_specs'], [{'sku_key': 'sku-1', 'name': '白色'}])
            self.assertGreater(preview['plan']['budget']['output_tokens'], 1536)
            for phrase in ('目标56～60字符', '目标28～30字符', '按去掉空格后的标题计数',
                           '搜索词、趋势词、核心词、修饰词', '避免仅调换词序'):
                self.assertIn(phrase, preview['plan']['request'])
            start = dict(input=body, key=preview['plan']['key'], allow_paid=False, retry=None)
            self.assertEqual(client.post(url + '/generate', headers=headers, json=start).status_code, 422)
            self.assertEqual(client.post(url + '/generate', headers=headers,
                                         json={**start, 'key':'stale', 'allow_paid':True}).status_code, 409)
            create.assert_not_called()
            generate = create.return_value.complete
            generate.return_value = {'text': json.dumps({'groups': [
                {'title':'猫毛清理器 家用除毛刷', 'guide':'家用粘毛器猫毛清理器'},
                {'title':'猫毛清理器猫毛清理器', 'guide':'家用粘毛器猫毛清理器'},
            ], 'sku_titles': [{'sku_key':'sku-1', 'title':'白色 猫毛清理器'}]})}
            start['allow_paid'] = True
            done = client.post(url + '/generate', headers=headers, json=start).json
            self.assertEqual(done['state'], 'complete')
            self.assertEqual(done['sku_titles'], [{'sku_key':'sku-1', 'title':'白色猫毛清理器',
                                                   'raw_title':'白色 猫毛清理器', 'issue':None}])
            self.assertEqual(done['groups'][0]['title']['used'][0]['sources'][0]['rank'], 1)
            self.assertEqual(done['groups'][0]['title']['used'][1]['sources'][0]['rank'], 8)
            self.assertEqual(done['groups'][0]['title']['value'], '猫毛清理器 家用除毛刷')
            self.assertEqual(done['groups'][0]['title']['fill_value'], '猫毛清理器家用除毛刷')
            self.assertEqual(done['groups'][0]['title']['width'], 20)
            self.assertIsNone(done['groups'][0]['guide']['issue'])
            self.assertIn('重复使用', done['groups'][1]['title']['issue'])
            self.assertIn('候选标题重复', done['groups'][1]['guide']['issue'])
            self.assertEqual(client.post(url + '/generate', headers=headers, json=start).json['id'], done['id'])
            self.assertEqual(client.get(url + '/result').json['id'], done['id'])
            record_file = manifest.parent / 'title-generation.json'
            history = json.loads(record_file.read_text(encoding='utf-8'))
            prior = history['runs'][history['latest']][0]
            prior['state'] = 'invalid'
            prior['groups'][0]['title']['issue'] = '旧规则判为重复'
            for group in prior['groups']:
                for title in group.values():
                    title.pop('fill_value')
                    title.pop('width')
            prior['sku_titles'][0].pop('raw_title')
            second = '1' * 32
            history['runs'][history['latest']].append({**done, 'id':second})
            saved = json.dumps(history)
            record_file.write_text(saved, encoding='utf-8')
            latest = client.get(url + '/result').json
            self.assertEqual(latest['id'], second)
            self.assertEqual(latest['attempts'], [done['id'], second])
            restored = client.get(url + '/result?attempt=' + done['id']).json
            self.assertEqual(restored['groups'], done['groups'])
            self.assertEqual(restored['sku_titles'], done['sku_titles'])
            self.assertEqual(restored['state'], 'complete')
            self.assertEqual(restored['latest_id'], second)
            self.assertEqual(client.get(url + '/result?attempt=' + '2' * 32).status_code, 404)
            self.assertEqual(record_file.read_text(encoding='utf-8'), saved)
            generate.assert_called_once()
            self.assertFalse((manifest.parent / 'state.json').exists())

    def test_sku_results_match_keys_and_keep_independent_failures(self):
        specs = [{'sku_key': f'sku-{i}', 'name': str(i)} for i in range(1, 7)]
        terms = [{'keyword': '猫毛清理器'}]
        rows = _sku_candidates([
            {'sku_key': 'sku-2', 'title': '猫毛清理器 ' + '白' * 10},
            {'sku_key': 'sku-1', 'title': '白色 猫毛清理器'},
            {'sku_key': 'sku-3', 'title': '白色猫毛清理器'},
            {'sku_key': 'sku-3', 'title': '黑色猫毛清理器'},
            {'sku_key': 'sku-5', 'title': '猫毛清理器' + '甲' * 10 + 'A'},
            {'sku_key': 'sku-6', 'title': ''},
            {'sku_key': 'sku-7', 'title': '猫毛清理器'},
        ], specs, terms)
        self.assertEqual([row['sku_key'] for row in rows], [spec['sku_key'] for spec in specs])
        self.assertEqual(rows[0]['title'], '白色猫毛清理器')
        self.assertEqual(rows[1]['title'], '猫毛清理器' + '白' * 10)
        self.assertIn('重复', rows[2]['issue'])
        self.assertIn('未返回', rows[3]['issue'])
        self.assertIn('超过 30', rows[4]['issue'])
        self.assertEqual(rows[4]['raw_title'], '猫毛清理器' + '甲' * 10 + 'A')
        self.assertIsNone(rows[5]['issue'])
        self.assertTrue(all(row['title'] == '' for row in rows[2:]))

    def test_request_includes_specs_after_the_previous_thirty_row_limit(self):
        specs = [{'销售规格': f'规格 {i}'} for i in range(1, 36)]
        product = _product({'category': CATEGORY, 'brand': '', 'attributes': {}, 'title': '粘毛器',
                            'guide': '', 'skus': specs})
        self.assertEqual(len(product['sku_specs']), 35)
        self.assertEqual(product['sku_specs'][-1], {'sku_key':'sku-35', 'name':'规格 35'})

    def test_evidence_only_credits_the_longer_exact_phrase(self):
        words = [{'id': index, 'keyword': word, 'rank': rank} for index, (word, rank) in enumerate(RANKED, 1)]
        marked, used = evidence('猫毛清理器 清理猫毛 除毛刷', words)
        self.assertEqual([item['text'] for item in marked if item.get('ranked')], ['猫毛清理器', '猫毛', '除毛刷'])
        self.assertEqual([item['count'] for item in used], [1, 1, 1])
        result = _candidates({'text':'{"groups":[{"title":"猫毛清理器","guide":"除毛刷"}]}'}, words,
                             {'title': [], 'guide': []})[0]
        self.assertIsNone(result['title']['issue'])
        self.assertIn('20～30', result['guide']['issue'])

    def test_targets_allow_shorter_titles_and_caps_are_independent(self):
        word = {'id': 1, 'keyword': '猫毛清理器', 'rank': 1}
        response = {'text': json.dumps({'groups': [
            {'title': '猫毛清理器', 'guide': '猫毛清理器 ' + '家' * 10 + 'A'},
            {'title': '猫毛清理器 ' + '家' * 25 + 'A', 'guide': '猫毛清理器 ' + '家' * 5},
            {'title': '猫毛清理器\u3000' + '用' * 25, 'guide': '猫毛清理器\u00a0' + '用' * 10},
        ]})}
        short, long, caps = _candidates(response, [word], {'title': [], 'guide': []})
        self.assertIsNone(short['title']['issue'])
        self.assertIn('20～30', short['guide']['issue'])
        self.assertIn('超过 60', long['title']['issue'])
        self.assertIsNone(long['guide']['issue'])
        self.assertTrue(all(title['issue'] is None for title in caps.values()))
        self.assertEqual((long['title']['width'], short['guide']['width']), (61, 31))
        self.assertEqual((caps['title']['width'], caps['guide']['width']), (60, 30))
        self.assertIn('\u3000', caps['title']['value'])
        self.assertNotIn('\u3000', caps['title']['fill_value'])
        self.assertNotIn('\u00a0', caps['guide']['fill_value'])

    def test_rank_matches_are_rechecked_on_fill_text(self):
        words = [{'id': 1, 'keyword': '猫毛清理器', 'rank': 1},
                 {'id': 2, 'keyword': '衣物 刷', 'rank': 2}]
        response = {'text': json.dumps({'groups': [
            {'title': '猫毛 清理器 猫毛清理器', 'guide': '家用手持除毛工具 衣物 刷'},
            {'title': '猫毛 清理器', 'guide': '家用手持除毛工具 猫毛 清理器'},
        ]})}
        repeated, compact = _candidates(response, words, {'title': [], 'guide': []})
        self.assertIn('重复使用', repeated['title']['issue'])
        self.assertIn('去掉空格后未逐字', repeated['guide']['issue'])
        self.assertIsNone(compact['title']['issue'])
        self.assertIsNone(compact['guide']['issue'])

    def test_single_character_matches_are_reference_only_for_repeat_checks(self):
        words = [{'keyword': '毛'}, {'keyword': '去猫毛'}]
        natural = '宠物去猫毛 长毛浮毛刮毛'
        repeated = '去猫毛家用长毛 去猫毛'
        response = {'text': json.dumps({'groups': [
            {'title': natural, 'guide': natural}, {'title': repeated, 'guide': repeated},
        ]})}
        accepted, rejected = _candidates(response, words, {'title': [], 'guide': []})
        for key in ('title', 'guide'):
            self.assertIsNone(accepted[key]['issue'])
            self.assertEqual(next(word['count'] for word in accepted[key]['used'] if word['keyword'] == '毛'), 3)
            self.assertIn('「去猫毛」重复使用 2 次', rejected[key]['issue'])
        rows = _sku_candidates([{'sku_key':'sku-1', 'title':natural}, {'sku_key':'sku-2', 'title':repeated}],
                               [{'sku_key':'sku-1'}, {'sku_key':'sku-2'}], words)
        self.assertIsNone(rows[0]['issue'])
        self.assertIn('「去猫毛」重复使用 2 次', rows[1]['issue'])
        self.assertEqual(rows[1]['raw_title'], repeated)
