"""V1.1结构、局部保留、统计、版本和付费门禁回归；只用脱敏样例，不联网。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.prompt_trials import contracts as c, processing as p, runner


def source(uid=1,text='大小合适，用着方便。',appends=None):
    """创建脱敏来源；参数：编号、初评及追评；返回：来源。"""
    return {'id':uid,'text':text,'sku':'脱敏规格','appends':appends or []}


def point(oid='o1',**values):
    """创建有完整字段的尺寸观点；参数：编号及覆盖项；返回：对象。"""
    return {'observation_id':oid,'information_types':['规格反馈'],'topic':'尺寸与大小','fact_text':'大小合适',
        'sentiment':'正面','target':'产品','target_detail':None,'source_type':'不明确','event_scope':'当前购买',
        'comparison':None,'explicit_impact':None,'evidence_ids':['e1'],'uncertainties':[],**values}


def review(uid=1,quote='大小合适',observations=None):
    """创建可追溯提取；参数：编号、引文及观点；返回：模型输出条目。"""
    return {'id':uid,'evidence':[{'evidence_id':'e1','segment':'initial','quote':quote}],
        'usage_stages':[],'observations':[point()] if observations is None else observations,'relations':[],'flags':[]}


def extraction(rows):
    """构造模型顶层；参数：数组；返回：对象。"""
    return {'schema_version':'reviews-extraction-v1.1','reviews':rows}


def incoming():
    """构造程序提供的报告输入；参数：无；返回：输入。"""
    rows=p.extraction(extraction([review()]),[source()])['reviews']
    return {'scope':{'sample_count':1,'analyzed_count':1,'pending_count':0,'report_stage':'完整样例报告'},
            'extraction':{'reviews':rows},'statistics':p.statistics(rows)['statistics'],'materials':[]}


def report():
    """构造最小完整报告；参数：无；返回：合法JSON结构对象。"""
    return {'schema_version':'competitor-report-v1.1','decision_summary':[{'text':'有尺寸认可线索','finding_ids':['f1'],'opportunity_ids':[],'limitation':'仅为脱敏样例'}],
        'findings':[{'id':'f1','category':'规格与取舍','facts':[{'text':'买家反馈大小合适',
            'observation_refs':[{'review_id':1,'observation_id':'o1'}],'evidence_refs':[{'review_id':1,'evidence_id':'e1'}]}],
            'interpretation':None,'statistics':[],'evidence_strength':'线索性发现','strength_reason':'仅有样例','limitations':[]}],
        'opportunities':[],'business_checks':[],'next_actions':[],'data_issues':[]}


class ContractTests(unittest.TestCase):
    def test_complete_valid_contracts(self):
        """完整提取和允许不推荐方向的报告结构有效。"""
        self.assertEqual(c.errors(extraction([review()]),c.EXTRACTION),[])
        self.assertEqual(c.errors(report(),c.REPORT),[])

    def test_type_extra_enum_and_required_null(self):
        """整数不能被字符串或布尔替代；空值仍须明确字段，额外键及越界枚举被发现。"""
        for uid in ('1',True):
            self.assertTrue(c.errors(review(uid),c.REVIEW))
        value=review();value['observations'][0]['target']='别的'
        self.assertTrue(c.errors(value,c.REVIEW))
        value=review();del value['observations'][0]['comparison']
        self.assertTrue(c.errors(value,c.REVIEW))
        value=review();value['information_status']='general_only'
        self.assertTrue(c.errors(value,c.REVIEW))

    def test_fixed_nested_fields_and_local_ids(self):
        """使用阶段不能随机换子键，片段和引用有固定格式。"""
        value=review();value['usage_stages']=[{'stage':'已使用','time_or_count':None,'evidence_ids':['e1']}]
        self.assertTrue(c.errors(value,c.REVIEW))
        self.assertTrue(c.errors({'evidence_id':'e0','segment':'append:0','quote':'x'},c.EVIDENCE))

    def test_strict_json_no_repair_and_truncation(self):
        """无效引号及截断明确失败，原文不被修复。"""
        text='{"text":"错"误"}'
        self.assertIsNone(p.decode(text)[0]);self.assertEqual(text,'{"text":"错"误"}')
        self.assertIsNone(p.decode('{}','length')[0]);self.assertIsNone(p.decode('[]')[0])


class ExtractionTests(unittest.TestCase):
    def test_local_detail_uncertainty_keeps_known_fact(self):
        """对象未知不丢尺寸满意及统计，保留精确疑点。"""
        o=point(uncertainties=[{'fields':['target_detail'],'reason':'未指明部件','evidence_ids':['e1']}])
        result=p.extraction(extraction([review(observations=[o])]),[source()])
        self.assertTrue(result['reviews'][0]['observations'][0]['eligibility']['basic_fact'])
        self.assertTrue(p.statistics(result['reviews'])['statistics'])

    def test_core_uncertainty_excludes_only_affected_point(self):
        """核心含义存疑仅排除相应观点统计，其他观点及整条保留。"""
        o=point('o2',uncertainties=[{'fields':['fact_text'],'reason':'含义不明','evidence_ids':['e1']}])
        result=p.extraction(extraction([review(observations=[point(),o])]),[source()])
        stats=p.statistics(result['reviews'])
        self.assertEqual(len(stats['excluded_observations']),1)
        self.assertEqual(result['reviews'][0]['information_status'],'informative')

    def test_invalid_quote_does_not_discard_sibling(self):
        """拼接或改写引文不合格，只隔离依赖观点，原返回不改。"""
        row=review();row['evidence'].append({'evidence_id':'e2','segment':'initial','quote':'大小方便'})
        row['observations'].append(point('o2',evidence_ids=['e2']))
        raw=extraction([row]);before=copy.deepcopy(raw)
        result=p.extraction(raw,[source()])
        self.assertEqual(raw,before);self.assertEqual(len(result['reviews'][0]['observations']),1)
        self.assertEqual(len(result['issues']),2)

    def test_repurchase_and_partial_understanding_status(self):
        """具体回购和物流信息由程序派生为有信息，局部难懂不覆盖整条。"""
        row=review(quote='一直在回购',observations=[point(information_types=['回购经历'],fact_text='一直在回购')])
        self.assertEqual(p.extraction(extraction([row]),[source(text='一直在回购')])['reviews'][0]['information_status'],'informative')
        row=review(quote='物流很快',observations=[point(topic='物流',fact_text='物流很快')])
        row['flags']=[{'code':'局部难以理解','message':'另半句不明','evidence_ids':['e1']}]
        self.assertEqual(p.extraction(extraction([row]),[source(text='不明文字，物流很快')])['reviews'][0]['information_status'],'informative')

    def test_missing_is_not_unclear(self):
        """缺失返回或空提取不伪装成买家无法理解。"""
        result=p.extraction(extraction([]),[source()])
        self.assertEqual(result['reviews'],[]);self.assertTrue(result['issues'])
        row=review(observations=[])
        self.assertEqual(p.extraction(extraction([row]),[source()])['reviews'][0]['information_status'],'pending')

    def test_blank_and_same_text_different_ids(self):
        """空白从源文字判定；相同文字不同编号保持独立。"""
        blank={'id':3,'evidence':[],'usage_stages':[],'observations':[],'relations':[],'flags':[]}
        result=p.extraction(extraction([review(),review(2),blank]),[source(),source(2),source(3,' \n')])
        self.assertEqual(len(result['reviews']),3);self.assertEqual(result['reviews'][-1]['information_status'],'empty')

    def test_id_restore_has_audit_and_ambiguous_duplicates_blocked(self):
        """类型恢复有审计，重复身份不选择其中之一，不接受前导零。"""
        result=p.extraction(extraction([review('1')]),[source()])
        self.assertEqual(len(result['adjustments']),1);self.assertTrue(result['structure_issues'])
        self.assertEqual(p.extraction(extraction([review(1),review('1')]),[source()])['reviews'],[])
        self.assertEqual(p.extraction(extraction([review('01')]),[source()])['reviews'],[])

    def test_duration_literal_and_cross_segment(self):
        """原文没有的次数只置空时间字段，跨初追评引用不接受。"""
        row=review(quote='刚用时开合顺手。')
        row['usage_stages']=[{'segment':'initial','stage':'已使用','duration_text':'用了几次','evidence_ids':['e1']}]
        result=p.extraction(extraction([row]),[source(text='刚用时开合顺手。')])
        self.assertIsNone(result['reviews'][0]['usage_stages'][0]['duration_text']);self.assertTrue(result['adjustments'])
        row['usage_stages'][0]['segment']='append:1'
        self.assertEqual(p.extraction(extraction([row]),[source(text='刚用时开合顺手。')])['reviews'][0]['usage_stages'],[])

    def test_history_and_current_statistics_separate(self):
        """历史负面不进入当前统计，事件归属疑点形成单独范围。"""
        rows=[review(1,observations=[point(sentiment='负面',event_scope='历史购买')]),review(2)]
        result=p.extraction(extraction(rows),[source(),source(2)])
        stats=p.statistics(result['reviews'])['statistics']
        self.assertEqual({s['event_scope'] for s in stats},{'历史购买','当前购买'})
        with self.assertRaises(ValueError):p.union_count(stats,[s['id'] for s in stats])

    def test_record_dedup_and_union(self):
        """同条多观点不重复计数，明确同范围来源取并集。"""
        rows=p.extraction(extraction([review(observations=[point(),point('o2')]),review(2)]),[source(),source(2)])['reviews']
        stats=p.statistics(rows)['statistics']
        self.assertTrue(all(s['count']==2 for s in stats))
        merged=p.union_count(stats,[s['id'] for s in stats])
        self.assertEqual(merged['count'],2);self.assertEqual(merged['review_ids'],[1,2])


class ReportTests(unittest.TestCase):
    def test_program_scope_and_no_claim_of_semantic_acceptance(self):
        """覆盖范围由程序填入，模型不能自写；机器不宣称语义已验证。"""
        data=incoming();raw=report();raw['scope']={'analyzed_count':999}
        result=p.report(raw,data)
        self.assertTrue(result['structure_issues'])
        self.assertEqual(result['compiled']['scope'],data['scope'])
        self.assertEqual(result['compiled']['review_status'],'unreviewed')

    def test_missing_ref_local_preservation_and_safe_summary(self):
        """一项引用无效只隔离该发现及依赖，不丢另一合格发现。"""
        raw=report();second=copy.deepcopy(raw['findings'][0]);second['id']='f2'
        second['facts'][0]['observation_refs'][0]['observation_id']='o99';raw['findings'].append(second)
        result=p.report(raw,incoming())
        self.assertEqual([f['id'] for f in result['compiled']['findings']],['f1'])
        self.assertTrue(result['issues']);self.assertTrue(result['compiled']['decision_summary'])

    def test_statistics_must_link_same_observation(self):
        """引用编号存在但无观点交集，不能直接展示为支持统计。"""
        data=incoming();stat=copy.deepcopy(data['statistics'][0]);stat['id']='unrelated'
        stat['observation_refs']=[{'review_id':99,'observation_id':'o1'}];data['statistics'].append(stat)
        raw=report();raw['findings'][0]['statistics']=[{'stat_id':'unrelated','meaning':'关联回购'}]
        result=p.report(raw,data)
        self.assertEqual(result['compiled']['findings'][0]['statistics'],[]);self.assertTrue(result['issues'])

    def test_enum_error_local_and_original_immutable(self):
        """证据枚举越界记录错误，原响应不被规范成假通过。"""
        raw=report();raw['findings'][0]['evidence_strength']='个别反馈';before=copy.deepcopy(raw)
        result=p.report(raw,incoming())
        self.assertTrue(result['structure_issues']);self.assertEqual(raw,before)
        self.assertEqual(result['compiled']['findings'],[])

    def test_no_fabricated_business_material_and_stat_warning(self):
        """无来源经营资料不进入展示，正文条数样式警示不冒充语义判定。"""
        raw=report();raw['business_checks']=[{'item':'利润','status':'已提供','known_information':'很高','material_refs':[],
            'missing_information':'无','decision_impact':'可以采购'}]
        raw['findings'][0]['facts'][0]['text']='有2条正面'
        result=p.report(raw,incoming())
        self.assertEqual(result['compiled']['business_checks'],[]);self.assertTrue(result['warnings'])


class GateTests(unittest.TestCase):
    def setUp(self):
        """建立临时门禁资料；参数：无；返回：无。"""
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for label in ('extract','report'):(self.root/(label+'-instruction.txt')).write_text('脱敏规则',encoding='utf-8')
        for label in ('extraction','report'):runner.write(self.root/(label+'.schema.json'),{})
        (self.root/'offline-tests.log').write_text('临时离线门禁样例',encoding='utf-8')
        runner.write(self.root/'manifest.json',{'max_calls':4,'max_reserved_tokens':runner.LIMIT,'phases':runner.PHASES,
            'prompt_hashes':{k:runner.checksum(self.root/(k+'-instruction.txt')) for k in ('extract','report')},
            'schema_hashes':{k:runner.checksum(self.root/(k+'.schema.json')) for k in ('extraction','report')}})
        runner.write(self.root/'offline-result.json',{'passed':True,'test_count':1,'code_hashes':runner.code_hashes(),
            'log_sha256':runner.checksum(self.root/'offline-tests.log')})

    def test_fee_and_failed_offline_never_create_client(self):
        """缺少显式费用或离线失败时绝不创建客户端。"""
        with patch.object(runner,'create_client') as client:
            with self.assertRaises(ValueError):runner.call(self.root,'real_extract')
            gate=runner.load(self.root/'offline-result.json');gate['passed']=False;runner.write(self.root/'offline-result.json',gate)
            with self.assertRaises(ValueError):runner.reserve(self.root,'real_extract','脱敏文字')
            client.assert_not_called()

    def test_no_duplicate_request_and_budget_cap(self):
        """意图落盘后禁止重发，预约总量不会自动提高。"""
        runner.reserve(self.root,'real_extract','脱敏文字')
        with self.assertRaisesRegex(ValueError,'禁止重试'):runner.reserve(self.root,'real_extract','脱敏文字')
        for phase in ('synthetic_extract','complete_report','partial_report'):runner.reserve(self.root,phase,'脱敏文字')
        self.assertEqual(len(list(self.root.glob('*-intent.json'))),4)

    def test_input_bound_and_tampering_fail_before_reservation(self):
        """超长输入、提示快照变化或代码指纹变化均不占名额。"""
        with self.assertRaises(ValueError):runner.reserve(self.root,'real_extract','x'*60001)
        (self.root/'extract-instruction.txt').write_text('已变化',encoding='utf-8')
        with self.assertRaises(ValueError):runner.reserve(self.root,'real_extract','脱敏文字')
        self.assertFalse(list(self.root.glob('*-intent.json')))

    def test_reserved_tokens_are_enforced(self):
        """已消耗保守额度不足时拒绝新请求，不自动抬高上限。"""
        runner.write(self.root/'previous-intent.json',{'reserved_tokens':runner.LIMIT-1})
        with self.assertRaisesRegex(ValueError,'预留上限'):runner.reserve(self.root,'real_extract','脱敏文字')
        self.assertFalse((self.root/'real_extract-intent.json').exists())

    def test_partial_input_isolation_and_original_unchanged(self):
        """完整/阶段范围由程序生成，不混入合成或排除三条，旧提取文件不变。"""
        ids=[601,602,603,604,605,610,618,624,646,658,663,672,679,694,696,699]
        sources=[source(uid) for uid in ids]
        result=p.extraction(extraction([review(uid) for uid in ids]),sources)
        runner.write(self.root/'real-input.json',{'reviews':sources,'product_context':{'platform_product_id':'123'},'captured_at':'2026-10-30 00:00:00'})
        runner.write(self.root/'real_extract-processed.json',result)
        before=(self.root/'real_extract-processed.json').read_bytes()
        full=runner.build_report_input(self.root);partial=runner.build_report_input(self.root,True)
        self.assertEqual(full['scope']['analyzed_count'],16)
        self.assertEqual(partial['scope']['analyzed_count'],13)
        self.assertEqual(partial['scope']['pending_count'],3)
        self.assertFalse({658,672,679}&{r['id'] for r in partial['extraction']['reviews']})
        self.assertEqual((self.root/'real_extract-processed.json').read_bytes(),before)

    def test_atomic_write_does_not_touch_v1(self):
        """证据写入只覆盖明确的新文件，旧版输入保持原样。"""
        old=self.root/'v1.json';old.write_text('{"old":true}',encoding='utf-8')
        before=old.read_bytes();runner.write(self.root/'v1_1.json',{'new':True})
        self.assertEqual(old.read_bytes(),before)


if __name__=='__main__':unittest.main()
