"""折叠展示边界：独立、混排及列表引用收起，分析、原文及来源链接保留。"""
import re
from html.parser import HTMLParser
from unittest import TestCase
from ecommerce.modules.review_analysis import v6_evidence,v6_report,v6_view


def quote(identifier,text='连续原文。'):
    """构造脱敏引用；参数：原编号、引文；返回：Markdown段落。"""
    return f'【{identifier}｜初评】原文：“{text}”'


class EvidenceFoldingTests(TestCase):
    def test_consecutive_quotes_form_closed_group_and_next_analysis_is_visible(self):
        raw='## 使用场景与需求\n\n明确的场景总结。原文依据：\n\n'+quote(1)+'\n\n'+quote(2)+'\n\n保留后续分析文字。'
        safe=v6_report.render(raw);result=v6_evidence.collapse(safe)
        self.assertEqual(result.count('class="ui-report-evidence"'),1);self.assertIn('查看原文依据（2处引用）',result)
        self.assertNotIn('<details open',result);self.assertIn('<p>明确的场景总结。</p>',result)
        self.assertTrue(result.index('</details>')<result.index('保留后续分析文字。'))
        self.assertEqual(result.count('href="#review-'),2)

    def test_heading_analysis_list_and_rule_separate_groups(self):
        raw=quote(1)+'\n\n## 产品优点\n\n'+quote(2)+'\n\n分析内容。\n\n'+quote(3)+'\n\n- 列表中的建议\n\n'+quote(4)+'\n\n---\n\n'+quote(5)
        result=v6_evidence.collapse(v6_report.render(raw));self.assertEqual(result.count('class="ui-report-evidence"'),5)
        self.assertIn('<h2>产品优点</h2>',result);self.assertIn('<li>列表中的建议</li>',result)

    def test_ambiguous_inline_analysis_and_unrecognized_citation_stay_visible(self):
        for raw in ('分析文字与'+quote(1),'【缺失编号｜初评】原文：“引用。”','<script>不执行</script>','`'+quote(1)+'`'):
            with self.subTest(raw=raw):
                safe=v6_report.render(raw);self.assertEqual(v6_evidence.collapse(safe),safe)

    def test_formatting_entities_and_original_links_preserved(self):
        raw='**【评价1 | 初评】** 原文：“A & B < C。”\n\n'+quote(1,'同源另一段，不能当额外买家。')
        safe=v6_report.render(raw);result=v6_evidence.collapse(safe)
        self.assertIn('查看原文依据（2处引用）',result);self.assert_preserved(raw,result)
        self.assertNotIn('<script>',result);self.assertEqual(result.count('href="#review-1"'),2)

    def test_label_only_paragraph_is_combined_without_removing_real_text(self):
        result=v6_evidence.collapse(v6_report.render('原文依据：\n\n'+quote(1)))
        self.assertNotIn('<p></p>',result);self.assertNotIn('<p>原文依据：</p>',result)
        raw='说明“原文依据：”这个词。\n\n'+quote(1)
        self.assertIn('这个词。',v6_evidence.collapse(v6_report.render(raw)))

    def test_toc_and_no_script_export_work_with_grouping(self):
        raw='## 总体判断\n\n总结。\n\n'+quote(1)+'\n\n## 产品缺点\n\n注意限制。'
        result,nav=v6_view.reading_view(v6_evidence.collapse(v6_report.render(raw)))
        self.assertEqual(len(nav),2);self.assertIn('id="report-section-2"',result)
        self.assertIn('<summary>查看原文依据（1处引用）</summary>',result)
        self.assertNotIn('<script',result)

    def test_mixed_paragraph_keeps_analysis_before_between_and_after_quotes(self):
        raw='**场景总结。**\n'+quote(1)+'\n'+quote(2)+'\n\n优点说明：'+quote(3)+' 后续建议。 '+quote(4)+' 注意规格。'
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertIn('查看原文依据（2处引用）',result)
        self.assertEqual(result.count('class="ui-report-evidence"'),3)
        self.assertIn('<p><strong>场景总结。</strong>\n</p><details',result)
        self.assertIn('</details><p> 后续建议。 </p><details',result)
        self.assertIn('</details><p> 注意规格。</p>',result)
        self.assertNotIn('<p><details',result)
        self.assert_preserved(raw,result)

    def test_bulleted_and_nested_ordered_items_keep_themes_and_list_structure(self):
        raw='- **场景甲：** '+quote(1)+' '+quote(2)+'\n- 场景乙：'+quote(3)+'\n\n    1. 子场景：'+quote(4)
        safe=v6_report.render(raw);result=v6_evidence.collapse(safe)
        self.assertEqual(result.count('class="ui-report-evidence"'),3)
        self.assertEqual(result.count('<li>'),safe.count('<li>'));self.assertEqual(result.count('<ol>'),1)
        self.assertIn('<p><strong>场景甲：</strong> </p><details',result)
        self.assertIn('查看原文依据（2处引用）',result);self.assert_preserved(raw,result)

    def test_blockquote_and_breaks_fold_but_tables_and_code_are_conservative(self):
        raw='> 主题总结：'+quote(1)+'\n>\n> 后续分析。\n\n场景：  \n'+quote(2)
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertEqual(result.count('class="ui-report-evidence"'),2)
        self.assert_preserved(raw,result)
        safe='<blockquote>'+v6_report.render('主题总结：'+quote(1))+'</blockquote>'
        self.assertIn('<blockquote><p>主题总结：</p><details',v6_evidence.collapse(safe))
        for unusual in ('```text\n'+quote(1)+'\n```','| 依据 |\n| --- |\n| '+quote(1)+' |'):
            safe=v6_report.render(unusual);self.assertEqual(v6_evidence.collapse(safe),safe)

    def test_entities_and_formatting_crossing_split_boundaries_are_preserved(self):
        raw='**A & B 场景： '+quote(1,'含 < > & “内引号” 的原文。')+' 后续总结。**'
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertEqual(result.count('class="ui-report-evidence"'),1)
        self.assertEqual(result.count('<strong>'),result.count('</strong>'))
        self.assertNotIn('< >',result);self.assert_preserved(raw,result)

    def test_unknown_and_unclosed_citations_stay_visible_without_hiding_other_analysis(self):
        for bad in ('【缺失编号｜初评】原文：“文字。”','【1｜初评】原文：“没有结束','【1｜初评】没有引号'):
            raw='主题：'+bad+'\n\n建议保留。';safe=v6_report.render(raw)
            self.assertEqual(v6_evidence.collapse(safe),safe)
        raw='主题：'+quote(1,'内含【2｜初评】字样。')+' 后续分析。'
        result=v6_evidence.collapse(v6_report.render(raw));self.assertIn('查看原文依据（1处引用）',result)
        self.assert_preserved(raw,result)

    def test_repeat_render_is_idempotent_and_does_not_change_citation_checks(self):
        raw='- 场景：'+quote(1)+' 后续建议。'
        sources=[{'id':1,'text':'连续原文。','appends':[]}]
        before=v6_report.check(raw,'stop',sources)
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertEqual(v6_evidence.collapse(result),result)
        self.assertEqual(v6_report.check(raw,'stop',sources),before)
        self.assert_preserved(raw,result)

    def test_adjacent_quotes_in_mixed_paragraph_form_one_group(self):
        raw='场景：'+quote(1)+quote(2)+' 后续建议。'
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertIn('查看原文依据（2处引用）',result);self.assert_preserved(raw,result)

    def test_each_reference_has_own_row_for_independent_mixed_and_list_formats(self):
        for raw in (quote(1)+' '+quote(2),quote(1)+'\n\n'+quote(2),'场景：'+quote(1)+' '+quote(2),'- 场景：'+quote(1)+' '+quote(2)):
            with self.subTest(raw=raw):
                result=v6_evidence.collapse(v6_report.render(raw))
                self.assertEqual(result.count('class="ui-report-evidence"'),1)
                self.assertEqual(result.count('class="ui-report-evidence-item"'),2)
                self.assertIn('查看原文依据（2处引用）',result);self.assert_preserved(raw,result)
                self.assertEqual(v6_evidence.collapse(result),result)

    def test_rows_preserve_repeated_source_append_labels_long_text_and_separators(self):
        raw='场景：'+quote(1,'带 < > & “嵌套引号” 的长原文。'*30)+'； '+quote(1)+' '+quote(1).replace('初评','追评1')
        result=v6_evidence.collapse(v6_report.render(raw))
        self.assertEqual(result.count('class="ui-report-evidence-item"'),3)
        self.assertIn('查看原文依据（3处引用）',result);self.assert_preserved(raw,result)
        self.assertIn('追评1',result)

    def assert_preserved(self,source,result):
        """对照所有可见文字及有序链接，忽略新增控件和排版空白；参数：原稿、展示；返回：断言。"""
        class Text(HTMLParser):
            """提取正文，排除新增summary标题。"""
            def __init__(self,value):
                """初始化；参数：HTML；返回：实例。"""
                super().__init__();self.parts=[];self.summary=False;self.feed(value)
            def handle_starttag(self,tag,attrs):
                """标记控件标题；参数：标签、属性；返回：无。"""
                if tag=='summary':self.summary=True
            def handle_endtag(self,tag):
                """结束标题；参数：标签；返回：无。"""
                if tag=='summary':self.summary=False
            def handle_data(self,value):
                """保留正文文字；参数：文字；返回：无。"""
                if not self.summary:self.parts.append(value)
        safe=v6_report.render(source)
        self.assertEqual(re.sub(r'\s','',''.join(Text(safe).parts)),re.sub(r'\s','',''.join(Text(result).parts)))
        self.assertEqual(re.findall(r'href="#review-\d+"',safe),re.findall(r'href="#review-\d+"',result))
