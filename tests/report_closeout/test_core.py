"""脱敏离线行为验证；不读业务原文、不联网、不访问数据库。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.report_closeout import core,runner,render


def row(uid=1,*,sentiments=('正面',),source='使用体验自述',eligible=True):
    """构建脱敏观点来源；参数：编号、倾向、来源、统计资格；返回：评价。"""
    evidence=[{'evidence_id':'e1','quote':'测试样本表达，不表示已实物核验。','segment':'initial'}]
    return {'id':uid,'evidence':evidence,'flags':[],'observations':[
        {'observation_id':'o'+str(i+1),'topic':'测试属性','information_types':['产品反馈'],'sentiment':s,
         'source_type':source,'event_scope':'当前购买','fact_text':'测试属性的自述',
         'evidence_ids':['e1'],'uncertainties':[],'eligibility':{'basic_fact':eligible,'uncertain_fields':[]}}
        for i,s in enumerate(sentiments)]}


def fixture():
    """构建完整脱敏报告关系；参数：无；返回：五个输入。"""
    rows=[row()];ref={'review_id':1,'observation_id':'o1'}
    fact={'fact_id':'a1','text':'测试事实','observation_refs':[ref],'evidence_refs':[{'review_id':1,'evidence_id':'e1'}]}
    finding={'id':'f1','facts':[fact],'category':'认可表现','statistic_ids':[]}
    counter={'finding_id':'f1','fact_id':'a1','explanation':'尚未核实的解释','inherited_observation_refs':[ref],'inherited_evidence_refs':fact['evidence_refs']}
    card={'id':'p1','name':'原测试方向','priority':'优先拿样验证','supporting_finding_ids':['f1'],'counter_evidence':[counter]}
    proposal={'name':'测试建议','reason':'仅用于核对行为','basis':[ref],
        'checks':[{'problem':'测试问题','action_type':'筛选现成款','action':'先核实资料再拿样',
                   'continue_condition':'资料可核对','stop_scope':'当前款','stop_condition':'当前样品不合要求'}]}
    incoming={'scope':{'sample_count':1,'analyzed_count':1,'pending_count':0},'pending_issues':[]}
    result={'compiled':{'findings':[finding],'opportunities':[card],'business_checks':[]},'issues':[],'structure_issues':[],'warnings':[]}
    assessment={'cards':{'p1':{'proposal':proposal,'counter_reviews':['该解释尚待人工判断']}}}
    return incoming,rows,result,assessment,'baseline-test'


def report():
    """返回脱敏派生；参数：无；返回：报告。"""
    return core.build('test',*fixture())


def receipt(item,*,decision='已采用',binding='baseline-test'):
    """构建模拟人工记录，仅用于测试；参数：条目、状态、来源；返回：记录。"""
    return {'phase':'test','item_id':item['id'],'candidate_digest':item['candidate_digest'],
            'baseline_digest':binding,'decision':decision,'reviewer':'测试填写人','comment':'测试意见','user_confirmed':True}


class CloseoutTests(unittest.TestCase):
    def test_sentiment_dedup_does_not_add_to_mention_count(self):
        views=core.source_views([row(sentiments=('正面','正面','负面')),row(2,sentiments=('中性',))])
        stat=next(s for s in core.statistics(views) if s['kind']=='主题')
        self.assertEqual(stat['count'],2)
        self.assertEqual(stat['sentiments']['正面'],{'count':1,'review_ids':[1]})
        self.assertEqual(stat['sentiments']['负面']['count'],1)
        self.assertEqual(stat['sentiments']['中性']['count'],1)

    def test_uncertain_impression_preserved_without_promoting_performance(self):
        views=core.source_views([row(source='收货或外观感受',eligible=False),row(2,source='转述',eligible=False)])
        self.assertEqual(len(views),2)
        self.assertEqual(core.statistics(views),[])
        self.assertTrue(all(v['expression_status']=='有疑点表达线索' and '未独立验证' in v['performance_status'] for v in views))

    def test_source_categories_keep_their_own_record_sets(self):
        stat=core.statistics(core.source_views([row(source='转述'),row(2,source='使用体验自述')]))[0]
        self.assertEqual(stat['sources']['转述']['review_ids'],[1])
        self.assertEqual(stat['sources']['使用体验自述']['review_ids'],[2])

    def test_history_not_merged_with_current(self):
        old=row(2);old['observations'][0]['event_scope']='历史购买'
        stats=[s for s in core.statistics(core.source_views([row(),old])) if s['kind']=='主题']
        self.assertEqual({s['event_scope'] for s in stats},{'当前购买','历史购买'})

    def test_structured_id_wins_over_free_text_message(self):
        incoming,rows,result,_,_=fixture()
        rows[0]['flags']=[{'code':'局部难以理解','message':'错误归因到评价999','evidence_ids':['e1']}]
        issues=core.program_issues(incoming,rows,result)
        self.assertEqual(issues[0]['review_id'],1)
        self.assertIn('评价1',issues[0]['description']);self.assertNotIn('999',json.dumps(issues))

    def test_duplicate_findings_are_links_not_new_evidence(self):
        args=list(fixture());second=copy.deepcopy(args[2]['compiled']['findings'][0]);second['id']='f2'
        args[2]['compiled']['findings'].append(second)
        built=core.build('test',*args)
        self.assertEqual(built['findings'][1]['canonical_id'],'f1')
        self.assertEqual(len(built['views']),1)

    def test_all_assistant_items_pending_and_inputs_immutable(self):
        args=fixture();saved=copy.deepcopy(args);built=core.build('test',*args)
        self.assertEqual(args,saved)
        self.assertTrue(all(i['review_status']=='待复核' for i in built['review_items']))
        self.assertEqual(built['cards'][0]['review_status'],'待复核')

    def test_explicit_review_only_applies_to_exact_candidate(self):
        built=report();item=built['review_items'][0]
        applied=core.apply_decisions(built,[receipt(item)])
        self.assertEqual(applied['review_items'][0]['review_status'],'已采用')
        self.assertEqual(applied['cards'][0]['review_status'],'待复核')
        self.assertEqual(built['review_items'][0]['review_status'],'待复核')
        changed=receipt(item);changed['candidate_digest']='wrong'
        with self.assertRaises(ValueError):core.apply_decisions(built,[changed])

    def test_baseline_changed_human_conclusion_not_reused(self):
        built=report()
        with self.assertRaises(ValueError):core.apply_decisions(built,[receipt(built['review_items'][0],binding='other')])

    def test_rejected_counter_does_not_become_adopted(self):
        built=report();records=[receipt(built['review_items'][0]),receipt(built['review_items'][1],decision='不采用')]
        applied=core.apply_decisions(built,records)
        self.assertEqual(applied['cards'][0]['review_status'],'已采用')
        self.assertEqual(applied['review_items'][1]['review_status'],'不采用')
        records.append(receipt(built['review_items'][0],decision='待复核'))
        self.assertEqual(core.apply_decisions(built,records)['cards'][0]['review_status'],'待复核')

    def test_missing_user_confirmation_rejected(self):
        built=report();r=receipt(built['review_items'][0]);r['user_confirmed']=False
        with self.assertRaises(ValueError):core.apply_decisions(built,[r])
        with self.assertRaises(ValueError):runner.decide(Path('unused'),'x','已采用','测试人','测试意见')

    def test_proposal_out_of_scope_or_bad_action_rejected(self):
        args=list(fixture());args[3]['cards']['p1']['proposal']['basis'][0]['review_id']=999
        with self.assertRaises(ValueError):core.build('test',*args)
        args=list(fixture());args[3]['cards']['p1']['proposal']['checks'][0]['stop_scope']='全部商品'
        with self.assertRaises(ValueError):core.build('test',*args)

    def test_partial_missing_stays_visible_near_cards(self):
        args=list(fixture());args[0]['pending_issues']=[{'review_id':2,'simulation':True}]
        built=core.build('test',*args)
        self.assertEqual(built['cards'][0]['pending_review_ids'],[2])
        self.assertTrue(any(i['review_id']==2 for i in built['program_issues']))

    def test_malicious_content_escaped(self):
        content=render.shell('<script>test</script>',render.lines(['<img src=x onerror=alert(1)>']))
        self.assertNotIn('<script>',content);self.assertNotIn('<img src=',content)
        self.assertIn('&lt;script&gt;',content)

    def test_decisions_append_and_revision_keeps_history(self):
        built=report()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(runner,'reports',return_value={'test':built}),patch.object(runner,'verify_baseline',return_value=(root,'baseline-test')):
                runner.decide(root,built['review_items'][0]['id'],'已采用','测试填写人','测试意见',True)
                first=(root/'decisions/000001.json').read_bytes()
                runner.decide(root,built['review_items'][0]['id'],'不采用','测试填写人','改变意见',True)
                self.assertEqual((root/'decisions/000001.json').read_bytes(),first)
                self.assertEqual(len(runner.read_receipts(root)),2)

    def test_bad_reference_types_rejected(self):
        with self.assertRaises(ValueError):core.ref_key({'review_id':'1','observation_id':'o1'})

    def test_missing_evidence_never_presented_as_traceable(self):
        source=row();source['evidence']=[]
        with self.assertRaises(ValueError):core.source_views([source])

    def test_publish_always_creates_new_revision(self):
        built=report()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);baseline=root/'baseline';baseline.mkdir()
            runner.write(root/'manifest.json',{'baseline':str(baseline)})
            runner.write(root/'baseline-hashes.json',{})
            runner.write(root/'assistant-assessment.json',{})
            for phase in ('real_extract','synthetic_extract'):runner.write(baseline/(phase+'-processed.json'),{'reviews':[row()]})
            with patch.object(runner,'reports',return_value={'test':built}),patch.object(runner,'verify_baseline',return_value=(baseline,'baseline-test')),patch.object(render,'render'):
                first=runner.publish(root);before=(first/'test.json').read_bytes()
                second=runner.publish(root)
                self.assertNotEqual(first,second)
                self.assertEqual((first/'test.json').read_bytes(),before)

    def test_frozen_baseline_tampering_blocks_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.txt';source.write_text('test',encoding='utf-8')
            hashes={str(source):runner.checksum(source)}
            runner.write(root/'baseline-hashes.json',hashes)
            runner.write(root/'assistant-assessment.json',{'baseline_digest':core.digest(hashes),'author_type':'assistant'})
            runner.write(root/'manifest.json',{'baseline':str(root)})
            runner.verify_baseline(root)
            source.write_text('changed',encoding='utf-8')
            with self.assertRaises(ValueError):runner.verify_baseline(root)

    def test_overview_tracks_decisions_instead_of_fixed_pending_label(self):
        built=report()
        decided=core.apply_decisions(built,[receipt(built['review_items'][0]),receipt(built['review_items'][1],decision='不采用')])
        self.assertEqual(render.review_summary([decided]),{'待复核':0,'已采用':1,'不采用':1})


if __name__=='__main__':unittest.main()
