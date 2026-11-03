"""固定类别顺序、引用标记与组合重点，禁止客户端注入分析规则。"""
from itertools import combinations

KINDS = {'reviews': '评价', 'questions': '问大家', 'main_images': '主图', 'detail_images': '详情图'}
VERSION = 'cross-v1'
TYPE = 'cross'
REPORT_VERSION = 'cross-report-v1'
SECTIONS = ('总体判断', '关键交叉发现', '使用场景与需求匹配', '产品风险与信息表达问题',
            '选品与调整方向', '拿样与供应商确认清单', '下一步行动与分析限制')
REQUIRED = (SECTIONS[0], SECTIONS[1], SECTIONS[-1])
FOCUS = dict(zip(combinations(KINDS, 2), (
    '核对买前顾虑与买后体验；区分已获反馈的问题和仍缺答案的问题。',
    '核对主图突出卖点与评价体验；识别重要痛点是否被展示内容回应。',
    '核对详情说明与评价体验，关注规格、使用方法、包含物与预期落差。',
    '核对主图是否回应购买顾虑；问题只代表关注，不能直接判为缺陷。',
    '核对详情说明能否回应购买顾虑；区分信息缺口与需要实测的问题。',
    '核对主图与详情图中的卖点、规格、包含物和介绍逻辑；图片宣传不等于性能实测。',
)))


def ordered(selection, minimum=2):
    """校验每类一份的选择并规范顺序；参数：类别到报告编号；返回：固定顺序字典。"""
    if not isinstance(selection, dict) or set(selection) - KINDS.keys() or not minimum <= len(selection) <= 4:
        raise ValueError('请选择至少两类、最多四类报告，每类一份。')
    from ecommerce.modules.review_analysis.service import identifier
    return {key: identifier(selection[key]) for key in KINDS if key in selection}


def combination(kinds):
    """生成固定顺序组合名；参数：类别集合；返回：中文名称。"""
    return ' × '.join(label for key, label in KINDS.items() if key in kinds)


def focus(kinds):
    """组合两类规则，三四类仍围绕议题综合，不提前认定有问题。"""
    keys = [key for key in KINDS if key in kinds]
    return '\n'.join(FOCUS[pair] for pair in combinations(keys, 2))
