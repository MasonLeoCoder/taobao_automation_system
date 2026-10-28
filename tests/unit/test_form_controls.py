"""公共控件模板的语义和全站接入契约，不启动浏览器或真实数据库。"""
from pathlib import Path
import re
import unittest

from flask import Flask, render_template_string


class FormControlsTests(unittest.TestCase):
    def setUp(self):
        """加载项目公共模板；参数：无；返回：无。"""
        self.root = Path(__file__).resolve().parents[2] / 'src/ecommerce/web'
        self.app = Flask(__name__, template_folder=str(self.root / 'templates'))

    def render(self, content, **values):
        """在自动转义上下文渲染控件；参数：模板及值；返回：HTML。"""
        with self.app.app_context():
            return render_template_string("{% from 'form_controls.html' import input_control,select_control,field,range_field,control_button %}" + content, **values)

    def test_boolean_values_zero_and_escaping_are_preserved(self):
        """False不产生禁用属性，0保留，用户文字与属性安全转义。"""
        text = self.render("{{ input_control({'name':'quantity','type':'number','value':0,'required':true,'disabled':false}) }}")
        self.assertIn('value="0"', text)
        self.assertIn('required=', text)
        self.assertNotIn('disabled=', text)
        text = self.render("{{ input_control({'value':danger,'placeholder':danger}) }}{{ control_button(danger) }}", danger='<script>alert(1)</script>')
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_native_names_labels_and_range_semantics_remain(self):
        """公共模板保留原生选择、明确标签和范围边界。"""
        text = self.render("{% call field('资料来源') %}{% call select_control({'name':'origin','disabled':true}) %}<option value=''>全部</option><option value='list' selected>列表</option>{% endcall %}{% endcall %}")
        self.assertIn('name="origin"', text)
        self.assertIn('disabled=', text)
        self.assertIn('ui-select', text)
        self.assertIn("value='list' selected", text)
        text = self.render("{{ range_field('销量范围',{'name':'sales_min','type':'number','min':'0','value':0,'aria-label':'销量最小值'},{'name':'sales_max','type':'number','value':'','aria-label':'销量最大值'}) }}")
        self.assertIn('<legend>销量范围</legend>', text)
        self.assertIn('aria-label="销量最小值"', text)
        self.assertIn('aria-label="销量最大值"', text)

    def test_all_visible_inputs_and_selects_use_shared_macros(self):
        """页面不再另写可见原生控件，公共资源从base统一加载一次。"""
        for file in (self.root / 'templates').glob('*.html'):
            if file.name == 'form_controls.html':
                continue
            text = file.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'<select\b', file.name)
            for tag in re.findall(r'<input\b[^>]*>', text):
                self.assertIn('type="hidden"', tag, file.name + ': ' + tag)
        base = (self.root / 'templates/base.html').read_text(encoding='utf-8')
        for resource in ('form_controls.css', 'searchable_select.css', 'searchable_select.js'):
            self.assertEqual(base.count(resource), 1)
            for file in (self.root / 'templates').glob('*.html'):
                if file.name != 'base.html':
                    self.assertNotIn("filename='" + resource + "'", file.read_text(encoding='utf-8'), file.name)
