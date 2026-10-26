"""问大家入口最多十次滚动、稳定底部提前停止及异常不跳过；不打开真实浏览器。"""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

from ecommerce.platforms.taobao.detail_questions import ensure_questions_open
from ecommerce.shared.exceptions import NeedsAttention


class QuestionEntryTests(unittest.TestCase):
    def setUp(self):
        """建立可计数滚动的页面替身；参数：无；返回：无。"""
        self.scrolled=0;self.position=0;self.stable=False;self.found_at=None;self.opened=False;self.duplicate=False
        self.page=MagicMock();self.guard=MagicMock()
        self.result=SimpleNamespace(page=self.page,target=SimpleNamespace(item_id='123'))
        self.page.locator.return_value.filter.return_value.count.return_value=0
        self.button=self.page.locator.return_value.get_by_text.return_value
        self.button.count.side_effect=lambda:2 if self.duplicate else int(self.found_at is not None and self.scrolled>=self.found_at)
        self.button.click.side_effect=lambda **kw:setattr(self,'opened',True)
        self.page.evaluate.side_effect=self.evaluate
        self.guard.perform.side_effect=self.perform

    def evaluate(self,expression):
        """模拟滚动和页面位置；参数：页面表达式；返回：位置数组或空。"""
        if 'window.scrollBy' in expression:
            self.scrolled+=1
            if not self.stable:self.position+=750
            return None
        return [self.position,100000,1000]

    def perform(self,label,action,**options):
        """执行受控动作并检查完成判定；参数：动作及判定；返回：动作结果。"""
        result=action()
        if options.get('completed'):self.assertTrue(options['completed']())
        return result

    def run_search(self,**snapshot):
        """执行真实入口寻找流程；参数：可覆盖快照；返回：查找结论。"""
        with patch('ecommerce.platforms.taobao.detail_questions.active_guard',return_value=self.guard),patch(
                'ecommerce.platforms.taobao.detail_questions.read_questions',side_effect=lambda *args:{'panel_present':self.opened,**snapshot}),patch(
                'ecommerce.platforms.taobao.detail_questions.matches_detail_url',return_value=True):
            return ensure_questions_open(self.result,100)

    def test_scroll_limit_is_ten_and_records_skip_reason(self):
        result=self.run_search()
        self.assertEqual(self.scrolled,10)
        self.assertEqual(result['status'],'skipped')
        self.assertEqual(result['stop_reason'],'entry_not_found')
        self.assertEqual(result['entry_search']['stop_reason'],'scroll_limit_reached')
        self.button.click.assert_not_called()

    def test_three_unchanged_scrolls_stop_early(self):
        self.stable=True
        result=self.run_search()
        self.assertEqual(self.scrolled,3)
        self.assertEqual(result['entry_search']['stop_reason'],'stable_position')

    def test_entry_appearing_after_tenth_scroll_is_opened_instead_of_skipped(self):
        self.found_at=10
        self.assertIsNone(self.run_search())
        self.assertEqual(self.scrolled,10);self.button.click.assert_called_once()

    def test_entry_appearing_after_third_unchanged_scroll_is_opened(self):
        self.stable=True;self.found_at=3
        self.assertIsNone(self.run_search());self.assertEqual(self.scrolled,3)
        self.button.click.assert_called_once()

    def test_existing_entry_or_explicit_empty_requires_no_scroll(self):
        self.found_at=0
        self.assertIsNone(self.run_search());self.assertEqual(self.scrolled,0)
        self.assertIsNone(self.run_search(empty_text='暂无问答'))

    def test_ambiguous_entry_or_unknown_structure_is_not_skipped(self):
        self.duplicate=True
        with self.assertRaisesRegex(ValueError,'不唯一'):self.run_search()
        self.duplicate=False
        with self.assertRaisesRegex(ValueError,'结构'):self.run_search(state='unrecognized_structure',reason='侧栏结构未知')
        self.assertEqual(self.scrolled,0)

    def test_verification_checkpoint_still_blocks(self):
        self.guard.checkpoint.side_effect=NeedsAttention('需要登录验证')
        with self.assertRaises(NeedsAttention):self.run_search()
        self.assertEqual(self.scrolled,0)


if __name__=='__main__':unittest.main()
