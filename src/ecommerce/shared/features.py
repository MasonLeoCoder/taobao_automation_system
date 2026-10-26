"""入口共用的功能目录，状态只描述已经实现的能力。"""

FEATURES = (
    {"key": "account", "name": "账号与会话", "status": "available", "label": "可用", "description": "千牛扫码登录及验证守卫；淘宝独立登录和在线检查（--platform taobao），验证/弹窗/相关限流暂停待人工，明确继续并复核后恢复；支持暂停、停止、保留窗口及免扫码复用。", "command": "account login"},
    {"key": "product", "name": "商品管理", "status": "partial", "label": "部分可用", "description": "粘毛器单商品：Excel读取、主图/详情图/视频、属性和SKU填写（含两类SKU图片、搜索标题，缺失日志）、提交到仓库及人工异常检查；全程检测验证和弹窗，未知窗口转人工；批量与草稿保存待接入。", "command": "product open"},
    {"key": "collection", "name": "商品采集", "status": "partial", "label": "新版DOM采集已实测", "description": "关键词搜索后按销量排序，以列表格子读取完整字段，默认只采第一页，可用 --page 指定从第一页起的页数；--limit为每页上限，默认不限制每页条数。每页独立保存并确认入库后再翻页，跨页重复展示保留，已确认末页可提前结束。同页分段加载主图，广告标注、调查和推荐词卡跳过、同ID不同位置保留，JSON/CSV含位置、分类计数和字段状态。新版真实整页46条及46张主图已核对，CSV采集时间为北京时间。详情支持ID或链接入口、主图视频下载、直接读取缩略图DOM图片、按完整地址去重并依次编号保存主图、独立基本信息（--basic-info）及动态SKU价格和图片（--skus），图片按规格名保存至sku图，明确缺货价格使用指定文案；动态参数（--parameters）保留原文、同名项、完整性统计和原始HTML；图文详情图片（--detail-images）按序保存至详情图，保留长图原件、逐图进度及整批发布。评价概览（--review-summary）记录评价数量、好评率范围和总结标签，不进入全部评价；逐条评价（--reviews）默认100条有效记录，--all取消上限，模板过滤、买家媒体链接及追评按批保留，商家回复不采。问大家（--questions）默认50题、每题全部可见回答，--all取消问题上限，逐题展开、短回答保留及定期休息，入口最多滚动10次且连续3次位置和高度不变提前停止，仍无入口记录跳过并继续流程，问大家JSON/CSV按问题绑定回答。资料按商品ID合并并保留历史和验证暂停；结构化结果已接入本机MySQL，CLI及采集资料页面支持商品、批次、评价问答历史查询，原文件保留。列表可显式--with-details逐个采集默认九模块，本地模拟和验证库已核对，真实平台验收待确认；Web商品采集支持关键词、页数、每页上限及连带详情配置；任意ID/链接可多选九模块，评价与问大家分别选择默认上限或不限数量；后台进度、验证反馈、停止和刷新恢复已接入，实时日志推送待接入。", "command": "taobao collect --keyword \"粘毛器\""},
    {"key": "selection", "name": "选品分析", "status": "planned", "label": "待实现", "description": "后续比较价格、需求与竞争，整理候选商品。", "command": None},
    {"key": "reports", "name": "后台报表", "status": "planned", "label": "待实现", "description": "生意参谋与万相台报表下载、归档。", "command": None},
    {"key": "analysis", "name": "经营分析", "status": "planned", "label": "待实现", "description": "后续展示销量、流量、推广及利润趋势。", "command": None},
)
