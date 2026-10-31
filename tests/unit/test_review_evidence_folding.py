"""折叠展示的边界回归：连续引文可收起，分析文字和原始内容保留。"""
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

    def test_inline_analysis_and_unrecognized_citation_stay_visible(self):
        for raw in ('分析文字与'+quote(1),quote(1)+' 这是分析结论。','【缺失编号｜初评】原文：“引用。”','<script>不执行</script>'):
            with self.subTest(raw=raw):
                safe=v6_report.render(raw);self.assertEqual(v6_evidence.collapse(safe),safe)

    def test_formatting_entities_and_original_links_preserved(self):
        raw='**【评价1 | 初评】** 原文：“A & B < C。”\n\n'+quote(1,'同源另一段，不能当额外买家。')
        safe=v6_report.render(raw);result=v6_evidence.collapse(safe)
        self.assertIn('查看原文依据（2处引用）',result);self.assertIn(safe,result)
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
