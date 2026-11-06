"""工作台菜单与静态功能规划；不读取业务数据或创建执行任务。"""
from flask import current_app, g, request, url_for
from ecommerce.modules.workspace_menu import service as menu_service
from ecommerce.infrastructure.storage.mysql import MySQLStorageError


def planned(key, label, problem, help_items, inputs, related, note=''):
    """定义规划页；参数：标识、名称、问题、帮助、资料、关联入口及说明；返回：页面定义。"""
    return dict(key=key, label=label, planned=True, problem=problem,
                help_items=help_items, inputs=inputs, related=related, note=note,
                endpoint='workspace.plan', values={'key': key})


def existing(item_key, label, endpoint, **values):
    """定义已有入口；参数：标识、名称、端点及参数；返回：原地址的菜单定义。"""
    return dict(key=item_key, label=label, planned=False, endpoint=endpoint, values=values)


GROUPS = (
    dict(key='workbench', label='经营工作台', items=(
        existing('home', '工作台概览', 'pages.home'),
        planned('business-profile', '经营档案',
                '让经营建议符合你的店铺阶段、资金条件、供货方式和经营目标。',
                ('整理店铺背景与经营目标', '明确预算、可投入时间与经营约束', '为AI建议提供统一的经营背景'),
                '店铺阶段、经营类目、供货方式、预算和目标。', ('home', 'ai')),
        planned('business-assistant', 'AI经营助手',
                '把分散的研究结论转成有依据、可以推进和复盘的经营行动。',
                ('结合当前商品与资料提供咨询', '整理经营计划、待办和完成标准', '跟踪决定、执行结果与复盘结论'),
                '经营档案、商品研究资料、你的决定及实际执行结果。', ('home', 'favorites', 'ai')),
        planned('calendar', '经营日历',
                '集中安排上新、询价、拿样、采购、活动和复盘，减少遗漏。',
                ('按时间查看经营安排', '关联商品与选品项目', '跟踪计划时间与实际进展'),
                '计划事项、时间安排、关联商品或项目。', ('home', 'tasks')),
        planned('reminders', '提醒中心',
                '把需要关注、核对和处理的事项汇集到一个地方。',
                ('汇总经营异常与待处理问题', '按影响和紧急程度整理事项', '记录处理进展与结果'),
                '经营数据、订单与库存资料、待确认问题及提醒偏好。', ('tasks', 'logs')),
        planned('knowledge', '经营知识库',
                '把零散经验沉淀成可查找、可复用的经营标准。',
                ('保存选品标准与拿样检查项', '整理供应商经验、话术和经营案例', '为后续AI建议提供你的经验依据'),
                '你确认的经验、业务规则、资料及案例。', ('favorites', 'catalog')),
    )),
    dict(key='research', label='市场与选品', items=(
        existing('collection', '商品采集', 'pages.module', key='collection'),
        existing('searches', '搜索列表', 'pages.collection_searches'),
        existing('catalog', '商品库', 'pages.collection_details'),
        existing('analysis', '搜索词分析', 'analysis.index'),
        existing('sycm-search', '生意参谋搜索排行', 'sycm_search.index'),
        existing('favorites', '我的关注', 'favorites.index'),
        planned('selection-projects', '选品项目',
                '围绕一个经营方向集中研究和比较商品，让收藏能够推进到选品决定。',
                ('关联搜索词、竞品与分析报告', '比较多个候选商品及其差异', '管理候选阶段、决策理由与待确认问题'),
                '研究方向、候选商品、分析资料和你的筛选条件。', ('analysis', 'catalog', 'favorites'),
                '我的关注用于收藏研究资料；选品项目用于正式评估和推进候选商品。'),
        planned('purchasing', '采购与拿样',
                '把值得研究的商品推进到询价、样品验证和采购判断。',
                ('整理供应商、报价与供货条件', '跟踪询价、拿样及待确认问题', '记录样品实测、照片与采购结论'),
                '供应商资料、报价、起订量、交期、样品记录和实测结果。', ('catalog', 'favorites', 'tools')),
        planned('profit-planning', '成本与利润测算',
                '在投入前估算成本、价格空间和收益，辅助判断商品是否值得做。',
                ('汇总采购、包装、运费等预计成本', '比较不同价格与推广投入情景', '列出测算假设和仍需确认的成本'),
                '供应商报价、预计售价、费用与损耗假设。', ('analysis', 'catalog')),
    )),
    dict(key='products', label='我的商品', items=(
        planned('product-files', '商品档案',
                '将你准备经营或正在销售的商品资料集中起来，方便追溯决策与调整。',
                ('关联货源、成本、SKU与参考竞品', '管理商品经营阶段', '保留标题、图片、详情和价格的版本记录'),
                '自有商品资料、供货信息、经营阶段及资料变更记录。', ('catalog', 'favorites'),
                '商品库保存采集研究资料；商品档案规划管理你自己的经营商品，两者可以关联。'),
        planned('listing', '商品策划与上架',
                '把选品研究转成明确的商品方案、内容需求和上架准备。',
                ('整理规格、组合和核心卖点', '策划标题、文案、主图和详情内容', '组织上架准备、检查与填写协助'),
                '已确认的商品方案、真实规格、素材、供货及平台填写要求。', ('catalog', 'tools'),
                '单商品填写与提交到仓库已接入，商品策划和批量流程仍在规划。'),
        existing('publish-categories', '发布类目管理', 'publish_categories.index'),
        planned('pricing', '定价与促销',
                '让日常售价、优惠和活动方案与成本及经营目标衔接。',
                ('整理价格与促销方案', '关联成本测算和商品目标', '记录方案变化及后续表现'),
                '商品成本、售价、优惠条件、活动目标和经营结果。', ('analysis', 'catalog')),
        planned('asset-library', '素材资产库',
                '集中管理自己的图片、视频、文案和供应商素材，减少重复查找与制作。',
                ('按商品和用途归档素材', '保留素材来源及版本', '关联素材策划与现有处理工具'),
                '图片、视频、文案、来源及所属商品。', ('tools', 'catalog')),
    )),
    dict(key='operations', label='店铺经营', items=(
        planned('business-data', '经营数据',
                '了解自己店铺和商品的实际表现，为经营判断提供数据依据。',
                ('汇总流量、点击、加购、成交与退款等资料', '按商品和时间查看表现', '标明数据来源、周期与更新时间'),
                '自己店铺的经营报表或手动整理的数据。', ('home', 'analysis'),
                '搜索词分析反映已采集的外部商品样本，不能代替自己店铺的经营数据。'),
        planned('diagnosis', '经营诊断',
                '结合实际资料找出优先需要处理的经营问题。',
                ('梳理曝光、商品表达、价格与转化问题', '给出有依据的改进方向', '说明资料缺口和下一步核对动作'),
                '店铺经营数据、商品资料、成本、经营目标和调整记录。', ('catalog', 'analysis')),
        planned('promotion', '推广与优化',
                '让推广和商品调整有计划、有记录，并能根据结果复盘。',
                ('整理推广目标和测试方案', '记录主图、标题、价格及推广设置的调整', '对照结果决定继续、调整或停止'),
                '推广投入与效果数据、商品调整内容、观察周期和目标。', ('catalog', 'tools')),
        existing('ledger', '经营账本', 'ledger.index'),
    )),
    dict(key='fulfillment', label='交易与履约', items=(
        planned('orders', '订单与异常',
                '集中掌握订单进展，及时发现和跟进需要处理的异常。',
                ('整理订单与履约状态', '汇总异常及待处理事项', '关联商品并跟踪处理结果'),
                '订单资料、履约记录和异常处理信息。', ('tasks', 'logs')),
        planned('inventory', '库存与补货',
                '了解可用库存和采购进展，辅助安排补货。',
                ('整理商品与SKU库存', '关联采购和到货进展', '结合经营情况提示补货关注项'),
                '期初库存、出入库、采购、销售和供货周期资料。', ('catalog',)),
        planned('after-sales', '售后与反馈',
                '将退款、买家反馈和处理经验转成商品与经营改进依据。',
                ('归纳退款原因和买家反馈', '关联具体商品、规格及处理结果', '将问题回流到采购、页面说明和包装改进'),
                '退款与售后记录、买家反馈、商品规格和处理结果。', ('catalog',)),
    )),
    dict(key='system', label='工具与系统', items=(
        existing('tools', '工具箱', 'image_tools.index'),
        existing('tasks', '任务记录', 'pages.tasks'),
        existing('logs', '运行日志', 'pages.logs'),
        existing('ai', 'AI设置', 'ai.index'),
        planned('data-connections', '数据接入',
                '为店铺经营、账本和订单等模块提供统一的资料接入入口。',
                ('规划手动资料与表格导入', '说明各模块所需资料和接入状态', '保留来源、时间范围和核对结果'),
                '待接入的数据文件、字段说明、来源及所服务的业务模块。', ('collection', 'tools'),
                '现有淘宝采集继续使用原入口；本页不导入数据或恢复历史资料。'),
        planned('backup', '备份与恢复',
                '保护后续账本、商品资料和经营记录，降低资料丢失后的恢复成本。',
                ('规划业务资料的备份范围', '查看备份记录与可恢复范围', '在明确选择后进行恢复核对'),
                '需要保护的资料、备份位置和保留要求。', ('tools', 'logs'),
                '本页仅说明规划，不创建备份，也不恢复已删除的历史数据。'),
    )),
)

ITEMS = {item['key']: item for group in GROUPS for item in group['items']}


def default_rows():
    """生成程序已登记菜单目录；参数：无；返回：非MySQL兼容默认及校验目录，不写数据库。"""
    groups = [dict(group_key=group['key'], name=group['label'], sort_order=number*10,
                   is_enabled=True, is_visible=True) for number, group in enumerate(GROUPS, 1)]
    items = [dict(menu_key=item['key'], group_key=group['key'], name=item['label'], sort_order=number*10,
                  construction_status='planned' if item['planned'] else 'available', is_enabled=True, is_visible=True)
             for group in GROUPS for number, item in enumerate(group['items'], 1)]
    return groups, items


def menu_data():
    """请求内共用菜单快照；参数：无；返回：数据库配置，正式MySQL不静默降级。"""
    if not hasattr(g, 'workspace_menu'):
        try:
            g.workspace_menu = menu_service.snapshot(current_app.config['SETTINGS'].mysql, default_rows(),
                database=current_app.config['WORKSPACE_MENU_DATABASE'])
        except MySQLStorageError as error:
            raise menu_service.MenuConfigurationError(str(error)) from None
    return g.workspace_menu


def active_key(active):
    """沿用原页面归属；参数：模板active；返回：唯一菜单标识或空值。"""
    if active == 'collections':
        return 'searches' if ('/searches' in request.path or '/batches' in request.path
                              or request.args.get('view') == 'searches') else 'catalog'
    if active and active.startswith('plan:'):
        return active[5:]
    return active


def workspace_navigation(active=None):
    """生成本次页面菜单；参数：原页面标识；返回：含链接与当前项的六组菜单。"""
    if getattr(g, 'workspace_menu_error', False):
        return []
    data = menu_data()
    selected = active_key(active)
    result = []
    for group in sorted(data['groups'].values(), key=lambda row: (row['sort_order'], row['group_key'])):
        items = []
        for row in sorted(data['items'].values(), key=lambda row: (row['sort_order'], row['menu_key'])):
            if row['group_key'] != group['group_key'] or not menu_service.visible(data, row['menu_key']):
                continue
            item = ITEMS[row['menu_key']]
            items.append(dict(item, label=row['name'], href=url_for(item['endpoint'], **item['values']),
                              current=item['key'] == selected, partial=item['key'] == 'listing'))
        if items:
            result.append(dict(key=group['group_key'], label=group['name'], items=items,
                               current=any(item['current'] for item in items)))
    return result


def plan_page(key):
    """查询静态规划；参数：规划标识；返回：页面资料，未知或已有入口返回None。"""
    item = ITEMS.get(key)
    if not item or not item['planned']:
        return None
    data = menu_data()
    row = data['items'][key]
    related = [dict(ITEMS[value], label=data['items'][value]['name'],
                    href=url_for(ITEMS[value]['endpoint'], **ITEMS[value]['values']))
               for value in item['related'] if menu_service.enabled(data, value)]
    return dict(item, label=row['name'], group=data['groups'][row['group_key']]['name'], related_links=related)
