"""类目模板下载和导入校验，候选值取自发放的模板。"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import load_workbook

from .listing_profiles import CATEGORY, LINT_ROLLER, MATERIAL_FOLDERS


TEMPLATE_VERSION = 1
TEMPLATES = {(LINT_ROLLER.key, CATEGORY): '粘毛器-产品信息模板.xlsx'}

BUNDLE_INSTRUCTIONS = """商品资料填写说明

1. 打开产品信息.xlsx，在 Sheet1 填写正式数据。填写示例和属性选项可以参考。
2. 1-1主图必需。放入恰好 5 张图片，文件名用 1.jpg、2.jpg 到 5.jpg。
3. 3-4主图按需提供。提供时放入 5 张，建议同样使用 1.jpg 到 5.jpg。
4. 详情图按需提供。按展示顺序用正整数命名，例如 1.jpg、2.jpg、10.jpg。
5. sku图按需提供。文件名与 Excel 中的销售规格一致，例如「长柄款」对应长柄款.jpg。
6. 商品视频按需提供。将一个 MP4 放在产品信息.xlsx 同一级，例如商品视频.mp4。

主图和详情图支持 PNG、JPG 和 JPEG。图片扩展名应与实际格式一致。
资料包里的图片目录都是空的。请放入自己的素材，再在上架页选择整个解压后的商品文件夹。
"""


def template_for(profile_key, category):
    """返回指定类目的工作簿路径。"""
    name = TEMPLATES.get((profile_key, category))
    return Path(__file__).with_name('templates') / name if name else None


def build_material_bundle(template, name):
    """把已确认的工作簿和空素材目录打包为 ZIP。"""
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        archive.mkdir(name)
        for folder in MATERIAL_FOLDERS.values():
            archive.mkdir(f'{name}/{folder}')
        archive.write(template, f'{name}/产品信息.xlsx')
        archive.writestr(f'{name}/填写说明.txt', BUNDLE_INSTRUCTIONS)
    stream.seek(0)
    return stream


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
