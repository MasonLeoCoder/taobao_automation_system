"""入口共用的功能目录，状态只描述已经实现的能力。"""

FEATURES = (
    {"key": "account", "name": "账号与会话", "status": "available", "label": "已迁移", "description": "扫码登录、保存会话与在线检查；验证及未知弹窗暂停等待人工处理。", "command": "account login"},
    {"key": "product", "name": "商品管理", "status": "partial", "label": "部分可用", "description": "粘毛器单商品：Excel读取、主图/详情图/视频、属性和SKU填写、提交到仓库及人工异常检查；全程检测验证和弹窗，未知窗口转人工；批量与草稿保存待接入。", "command": "product open"},
    {"key": "collection", "name": "商品采集", "status": "planned", "label": "待实现", "description": "后续支持关键词查找和指定竞品链接采集。", "command": None},
    {"key": "selection", "name": "选品分析", "status": "planned", "label": "待实现", "description": "后续比较价格、需求与竞争，整理候选商品。", "command": None},
    {"key": "reports", "name": "后台报表", "status": "planned", "label": "待实现", "description": "生意参谋与万相台报表下载、归档。", "command": None},
    {"key": "analysis", "name": "经营分析", "status": "planned", "label": "待实现", "description": "后续展示销量、流量、推广及利润趋势。", "command": None},
)
