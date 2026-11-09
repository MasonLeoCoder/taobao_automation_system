"""代码版本维护的末级类目上架方案。"""
from dataclasses import dataclass, field

CATEGORY = '宠物/宠物食品及用品 > 猫/狗美容护理工具 > 粘毛器'
BACKPACK_CATEGORY = '宠物/宠物食品及用品 > 猫/狗出行用品 > 背包/箱包'
GROUPS = {'excel': '商品 Excel', 'main': '1:1 主图', 'portrait': '3:4 主图',
          'detail': '详情图', 'sku': 'SKU 图', 'video': '视频'}
MATERIAL_FOLDERS = {'main': '1-1主图', 'portrait': '3-4主图', 'sku': 'sku图', 'detail': '详情图'}
ATTRIBUTES = ('适用场景', '适用宠物类型', '握柄形态', '粘面材质', '粘性强度')


@dataclass(frozen=True)
class ListingProfile:
    key: str
    label: str
    groups: dict
    required_files: frozenset
    attributes: tuple
    required_attributes: frozenset = frozenset()
    text_limits: dict[str, int] = field(default_factory=dict)
    search_keyword: str | None = None
    brand_in_editor: bool = False
    allow_submit: bool = True


LINT_ROLLER = ListingProfile('lint_roller', '粘毛器上架方案', GROUPS,
                             frozenset({'excel', 'main'}), ATTRIBUTES)
BACKPACK = ListingProfile('pet_backpack', '背包/箱包上架方案', GROUPS, frozenset({'excel', 'main'}),
                          ('商品品类', '适用场景', '适用动物种类', '适用品种', '是否防水'),
                          required_attributes=frozenset({'适用场景', '适用动物种类', '适用品种'}),
                          text_limits={'适用品种': 100},
                          search_keyword='宠物背包', brand_in_editor=True, allow_submit=False)
PROFILES = {profile.key: profile for profile in (LINT_ROLLER, BACKPACK)}
INITIAL_PROFILES = {CATEGORY: LINT_ROLLER.key}


def support(row):
    if not row['is_visible']:
        return '当前账号不可见'
    if not row['is_leaf']:
        return '请选择下级'
    if row['listing_profile_key'] not in PROFILES:
        return '待扩充'
    return '已支持' if row['profile_status'] == 'verified' else '待复核'
