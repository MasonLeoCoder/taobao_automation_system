"""参数名称和值独立展示，空值、重复名称及分页排序均保留原观察。"""
import re
import unittest

from ecommerce.modules.collection.queries import query
from tests.unit import test_collection_browser as fixtures


class ParameterDisplayTests(unittest.TestCase):
    def setUp(self):
        """准备隔离服务；参数：无；返回：无。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.client=self.fixture.client;self.settings=self.fixture.settings

    def test_columns_show_actual_values_and_keep_duplicate_names(self):
        for url in ['/collections/products/123?module=parameters','/collections/batches/20']:
            body=self.client.get(url).get_data(as_text=True)
            self.assertIn('参数名称',body);self.assertIn('参数值',body)
            self.assertNotIn('保留来源原文',body);self.assertNotIn('同名参数：A',body)
            self.assertEqual(body.count('>同名参数</td>'),2)
            self.assertIn('id="parameter-value-20" class="parameter-value-text">A</div>',body)
            self.assertIn('id="parameter-value-21" class="parameter-value-text">B</div>',body)
            self.assertIn('data-parameter-copy role="button" tabindex="0" title="点击复制"',body)
            self.assertNotIn('>复制参数值</button>',body)
            self.assertIn('id="parameter-copy-feedback"',body)

    def test_empty_zero_and_html_values_are_not_confused_or_rewritten(self):
        for identifier,value in [(22,None),(23,''),(24,' \t '),(25,'0'),(26,'<script>unsafe()</script>')]:
            self.fixture.put('parameter_observations',id=identifier,batch_id=20,position=identifier,name='样例',value=value)
        body=self.client.get('/collections/batches/20').get_data(as_text=True)
        self.assertEqual(body.count('>未采到参数值</div>'),3)
        self.assertIn('id="parameter-value-25" class="parameter-value-text">0</div>',body)
        self.assertIn('&lt;script&gt;unsafe()&lt;/script&gt;',body);self.assertNotIn('<script>unsafe()',body)
        rows=query(self.settings,'records',batch_id=20,page=1)['records']
        self.assertEqual([row['value'] for row in rows[-5:]],[None,'',' \t ','0','<script>unsafe()</script>'])

    def test_full_long_value_is_preserved_and_sorting_stays_numbered(self):
        long='第一行\n'+'颜色与型号♥ '*100+'\n末尾  '
        self.fixture.put('parameter_observations',id=22,batch_id=20,position=3,name='颜色分类',value=long)
        for i in range(23):
            self.fixture.put('parameter_observations',id=100+i,batch_id=20,position=4+i,name='名称'+str(i),value=str(i))
        first=query(self.settings,'records',batch_id=20,page=1,sort_by='position',order='asc')
        second=query(self.settings,'records',batch_id=20,page=2,sort_by='position',order='asc')
        self.assertEqual((len(first['records']),len(second['records'])),(20,6))
        self.assertEqual(first['records'][2]['value'],long)
        body=self.client.get('/collections/batches/20?page=1&sort=position&order=asc').get_data(as_text=True)
        self.assertIn(long,body);self.assertIn('page=2',body)
        self.assertEqual(len(re.findall(r'<tr>',re.search(r'<tbody>(.*?)</tbody>',body,re.S).group(1))),20)
        sorted_rows=query(self.settings,'records',batch_id=20,page=1,sort_by='value',order='desc')['records']
        self.assertEqual(sorted_rows[0]['value'],long)


if __name__=='__main__':unittest.main()
