"""网页日志的中文展示，原始任务名称和步骤代码保留在查询结果中。"""
TASK_NAMES = {
    'maintenance.list_images.once': '历史列表图片补充（一次性）',
    'collection.taobao.search': '淘宝关键词搜索',
    'collection.taobao.collect': '淘宝列表采集',
    'collection.taobao.detail.open': '淘宝商品详情采集',
    'collection.taobao.pagination.probe': '淘宝翻页检查',
    'account.taobao.login': '淘宝登录',
    'account.taobao.check': '淘宝登录状态检查',
    'account.login': '千牛登录', 
    'account.check': '千牛登录状态检查',
    'product.open': '千牛商品填写', 
    'product.submit': '千牛商品填写并提交',
}
STEP_NAMES = {
    'search': '关键词搜索', 
    'list': '列表商品采集', 
    'detail_open': '打开商品详情',
    'video': '商品视频采集', 
    'main_images': '商品主图采集', 
    'basic_info': '基本信息采集',
    'skus': 'SKU 规格采集',
    'parameters': '商品参数采集', 
    'detail_images': '详情图片采集',
    'review_summary': '评价概览采集', 
    'reviews': '评价明细采集', 
    'questions': '问大家采集',
}
EVENT_STEPS = {
    'task_started': '任务开始', 
    'task_finished': '任务结束', 
    'session_ended': '浏览器会话结束',
    'collection_saved': '保存采集资料', 
    'collection_module_skipped': '问大家入口寻找',
    'storage_wait': '等待数据入库',
    'storage_resumed': '数据存储已恢复',
}
LEVELS = {
    'DEBUG': ('调试', 'debug'), 
    'INFO': ('信息', 'info'), 
    'WARNING': ('警告', 'warning'),
    'ERROR': ('错误', 'error'), 
    'CRITICAL': ('严重错误', 'critical'),
}
EVENT_TYPES = {
    'list_images_processed': '列表主图处理完成',
    'task_started': '任务开始',
    'task_finished': '任务结束',
    'list_collection_summary': '列表采集汇总已保存',
    'list_detail_interrupted': '列表详情采集中断',
    'progress': '执行进度更新',
    'collection_saved': '采集数据已入库',
    'collection_module_skipped': '模块已跳过',
    'state_changed': '执行状态变化',
    'blocked': '执行遇到阻碍',
    'task_progress': '任务状态更新',
    'document_requested': '页面导航请求',
    'operation_finished': '单步操作结束',
    'operation_started': '单步操作开始',
    'operation_failed': '单步操作失败',
    'operation_verified_after_error': '异常后操作结果已核实',
    'session_ended': '浏览器会话结束',
    'session_halted': '浏览器会话中断',
    'storage_wait': '等待数据入库',
    'storage_resumed': '数据存储已恢复',
    'started': '执行开始',
    'log': '普通日志',
    'paced': '操作间隔等待',
    'input_unavailable': '人工输入不可用',
    'pagination_probe_ready': '翻页检查已就绪',
    'pagination_probe_click': '翻页检查点击',
    'pagination_probe_verified': '翻页检查已核实',
    'list_collection_requested': '列表采集已发起',
    'list_page_saved': '当前页列表已保存',
    'pagination_click': '执行翻页点击',
    'pagination_verified': '翻页结果已核实',
    'list_detail_started': '列表详情采集开始',
    'list_detail_saved': '列表详情采集已保存',
}


def present_log_event(event):
    """补充日志展示标签；参数：原事件；返回：不修改原记录的展示副本。"""
    name, step = event.get('task_name'), event.get('step')
    level, color = LEVELS.get(event.get('level'), (event.get('level') or '未提供', 'unknown'))
    return {**event,
        'event_type_label': EVENT_TYPES.get(event.get('event_type'), event.get('event_type') or '类型未记录'),
        'task_label': TASK_NAMES.get(name, name or ('任务名称未记录' if event.get('task_id') else '无可靠任务归属')),
        'step_label': STEP_NAMES.get(step, step) if step else EVENT_STEPS.get(event.get('event_type'), '步骤未记录'),
        'level_label': level, 'level_class': color}
