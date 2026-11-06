"""代码版本维护的末级类目上架方案。"""
from dataclasses import dataclass

CATEGORY = '宠物/宠物食品及用品 > 猫/狗美容护理工具 > 粘毛器'
GROUPS = {'excel': '商品 Excel', 'main': '1:1 主图', 'portrait': '3:4 主图',
          'detail': '详情图', 'sku': 'SKU 图', 'video': '视频'}
ATTRIBUTES = ('适用场景', '适用宠物类型', '握柄形态', '粘面材质', '粘性强度')


@dataclass(frozen=True)
class ListingProfile:
    key: str
    label: str
    groups: dict
    required_files: frozenset
    attributes: tuple


LINT_ROLLER = ListingProfile('lint_roller', '粘毛器上架方案', GROUPS,
                             frozenset({'excel', 'main'}), ATTRIBUTES)
PROFILES = {LINT_ROLLER.key: LINT_ROLLER}
INITIAL_PROFILES = {CATEGORY: LINT_ROLLER.key}


def support(row):
    if not row['is_visible']:
        return '当前账号不可见'
    if not row['is_leaf']:
        return '请选择下级'
    if row['listing_profile_key'] not in PROFILES:
        return '待扩充'
    return '已支持' if row['profile_status'] == 'verified' else '待复核'
