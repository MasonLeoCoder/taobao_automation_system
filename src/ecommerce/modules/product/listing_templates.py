"""类目模板下载和导入校验，候选值取自发放的模板。"""
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook

from .listing_profiles import CATEGORY, LINT_ROLLER


TEMPLATE_VERSION = 1
TEMPLATES = {(LINT_ROLLER.key, CATEGORY): '粘毛器-产品信息模板.xlsx'}


def template_for(profile_key, category):
    """返回指定类目可下载的工作簿路径。"""
    name = TEMPLATES.get((profile_key, category))
    return Path(__file__).with_name('templates') / name if name else None


@lru_cache(maxsize=len(TEMPLATES))
def _attribute_options(path):
    workbook = load_workbook(path, read_only=True, keep_links=False)
    try:
        options = {}
        for field, value in workbook['属性选项'].iter_rows(min_row=2, max_col=2, values_only=True):
            if field and value:
                options.setdefault(field, set()).add(value)
        return options
    finally:
        workbook.close()


def validate_template(book, profile, category):
    """只对带模板标记的资料核对版本和明确填写的属性。"""
    if book.template is None:
        return
    template = template_for(profile.key, category)
    if not template or book.template != (category, TEMPLATE_VERSION):
        raise ValueError('产品信息模板与当前类目或版本不一致，请重新下载')
    options = _attribute_options(template)
    for field in profile.attributes:
        if field not in book.fields:
            raise ValueError(f'产品信息模板缺少「{field}」列，请重新下载')
        value = book.fields[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not isinstance(value, str) or value.strip() not in options[field]:
            raise ValueError(f'「{field}」的值「{value}」不在单选候选中，请查看「属性选项」')
