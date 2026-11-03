"""图片独立分析的白名单、版本与章节，联合旧规则保持冻结。"""
KINDS = {
    'main_images': {
        'module': 'main_images', 'label': '主图', 'version': 'main-images-v1',
        'report_version': 'main-images-report-v1', 'prompt': 'main_report.txt',
        'sections': ('总体判断', '逐图分析', '卖点与使用场景', '整组图片的分工与信息完整性', '选品线索与待验证事项', '展示改进建议'),
    },
    'detail_images': {
        'module': 'detail_images', 'label': '详情图', 'version': 'detail-images-v1',
        'report_version': 'detail-images-report-v1', 'prompt': 'detail_report.txt',
        'sections': ('总体判断', '阅读结构与介绍逻辑', '使用场景与需求表达', '规格、包含物与使用说明', '卖点宣称与图片依据', '选品线索与改进方向', '拿样与供应商确认清单'),
    },
}


def kind(module):
    """读取有限模块配置；参数：模块名；返回：新字典，非法类型拒绝。"""
    if module not in KINDS: raise ValueError('只允许主图或详情图独立分析')
    return dict(KINDS[module])
