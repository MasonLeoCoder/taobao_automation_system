"""首版 MySQL 结构快照：22 张业务表、中文备注和数据库约束；发布后不改写。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

REVISION = "0001_mysql_foundation"
MODULES = ("list", "video", "main_images", "skus", "parameters", "detail_images", "review_summary", "reviews", "questions")


def build_metadata():
    """构造不可变版本的表定义；参数：无；返回：含备注的 MetaData。"""
    metadata = sa.MetaData()
    number = lambda: mysql.BIGINT(unsigned=True)
    code = lambda size=64: sa.String(size, collation="ascii_bin")
    instant = lambda: mysql.DATETIME(fsp=6)

    def col(name, datatype, comment, *, nullable=False, default=None, primary=False, contract=None):
        """声明中文字段；参数：名称、类型、备注及约束；返回：Column。"""
        return sa.Column(name, datatype, nullable=nullable, primary_key=primary,
                         server_default=sa.text(default) if default is not None else None,
                         comment=comment, info={"json_contract": contract} if contract else {})

    def identity():
        """生成内部主键；参数：无；返回：自增编号字段。"""
        return sa.Column("id", number(), primary_key=True, autoincrement=True, comment="内部编号：数据库自增主键，不代替平台业务编号")

    def time(name, title, *, nullable=True):
        """声明 UTC 时间；参数：字段名、中文名、可空性；返回：时间字段。"""
        return col(name, instant(), title+"：UTC 时间，微秒精度；空值表示未发生或无法可靠确定" if nullable else title+"：UTC 时间，微秒精度", nullable=nullable)

    def payload(name="payload", meaning="源记录扩展信息"):
        """声明扩展结构；参数：字段名、业务口径；返回：JSON 字段。"""
        return col(name, mysql.JSON(), meaning+"：JSON 对象或数组；SQL NULL 表示源未提供，原始文件另行保留", nullable=True,
                   contract=meaning+"；按所属源 schema_version 解释，未知键保留；缺失、null、空数组及零不混用")

    def imported():
        """声明导入归属；参数：无；返回：可空导入编号字段。"""
        return col("import_run_id", number(), "导入执行编号：关联 import_runs.id；实时记录为空", nullable=True)

    def table(name, title, *columns):
        """登记带表备注的表；参数：英文名、中文用途、字段；返回：Table。"""
        return sa.Table(name, metadata, *columns, comment=title,
                        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")

    def unique(tab, name, *columns):
        """登记唯一约束；参数：表、约束名、字段；返回：无。"""
        tab.append_constraint(sa.UniqueConstraint(*columns, name=name))

    def check(tab, name, expression):
        """登记检查约束；参数：表、约束名、表达式；返回：无。"""
        tab.append_constraint(sa.CheckConstraint(expression, name=name))

    def fk(tab, name, columns, targets):
        """登记限制更新删除的外键；参数：表、名称、引用列与目标；返回：无。"""
        tab.append_constraint(sa.ForeignKeyConstraint(columns, targets, name=name, ondelete="RESTRICT", onupdate="RESTRICT", use_alter=True))

    imports = table("import_runs", "历史导入执行表：记录预览或导入的状态、范围、核对统计与解析版本",
        identity(), time("started_at", "导入开始时间", nullable=False), time("finished_at", "导入结束时间"),
        col("status", code(24), "导入状态：preview、running、committed、failed、partial"),
        payload("scope", "导入范围和源目录"), payload("counts", "各目标表新增、跳过、冲突与失败计数"),
        col("manifest_sha256", code(), "来源清单哈希：SHA-256 十六进制，用于核对输入版本"),
        col("parser_version", code(48), "解析器版本：解释来源格式与规则的程序版本"))
    check(imports,"ck_import_status","status IN ('preview','running','committed','failed','partial')")

    tasks = table("tasks", "任务记录表：保存任务生命周期、结果、执行资源和进程归属",
        col("id", code(32), "任务编号：32 位任务标识，保留原历史编号", primary=True),
        col("name", code(96), "任务类型：业务入口名称，例如 collection.taobao.detail.open"),
        col("status", code(24), "任务状态：running、succeeded、failed、cancelled、needs_attention"),
        time("started_at", "任务开始时间", nullable=False), time("finished_at", "任务结束时间"),
        col("message", sa.Text(), "任务结果说明：原始结论或等待处理原因", nullable=True),
        col("origin", code(16), "记录来源：live 实时或 historical 历史导入"),
        col("execution_key", code(), "执行资源键：浏览器资料目录的规范化指纹；历史未知时为空", nullable=True),
        col("owner_pid", number(), "所属进程编号：本机进程 ID，须结合进程启动时间核对；历史为空", nullable=True),
        time("owner_process_started_at", "所属进程启动时间"),
        col("last_event_sequence", number(), "已提交事件序号：同任务事务内分配，初始为 0", default="0"), imported())
    check(tasks,"ck_task_status","status IN ('running','succeeded','failed','cancelled','needs_attention')")
    check(tasks,"ck_task_origin","origin IN ('live','historical')")
    sa.Index("ix_tasks_execution_status",tasks.c.execution_key,tasks.c.status)
    sa.Index("ix_tasks_status_started",tasks.c.status,tasks.c.started_at)

    products = table("products", "商品身份表：统一平台商品编号，标题仅为带来源的显示缓存",
        identity(), col("platform",code(32),"商品平台：例如 taobao，区分大小写"),
        col("platform_product_id",code(64),"平台商品编号：字符串原值，不能转换为浮点数"),
        col("display_title",sa.Text(),"显示标题：带来源的查询缓存，不覆盖各历史观察原文",nullable=True),
        col("title_source_batch_id",number(),"标题来源批次编号：关联 collection_batches.id；未知时为空",nullable=True))
    unique(products,"uq_product_identity","platform","platform_product_id")

    runs = table("collection_runs", "商品采集运行表：某商品一次所选模块的整体汇总，不代替模块结果状态",
        identity(),col("source_run_key",code(96),"原运行汇总编号：保留 collection_summary 的原批次编号"),
        col("task_id",code(32),"所属任务编号：关联 tasks.id；来源缺失时为空",nullable=True),
        col("product_id",number(),"商品内部编号：关联 products.id"),
        col("status",code(32),"运行汇总状态：保留 complete_with_warnings、partial、failed 等原语义"),
        time("started_at","运行开始时间"),time("finished_at","运行结束时间"),
        payload("warnings","各模块警告及对应模块名"),payload("error","运行错误类型与摘要"),imported())
    unique(runs,"uq_run_source","source_run_key")
    unique(runs,"uq_run_product","id","product_id")
    unique(runs,"uq_run_task","id","task_id")

    steps = table("collection_steps", "采集模块步骤表：记录模块选择、未执行原因、处理状态及放行依据",
        identity(),col("run_id",number(),"所属运行编号：关联 collection_runs.id"),
        col("module",code(32),"模块名称：video、main_images、skus、parameters、detail_images、review_summary、reviews、questions"),
        col("requested",sa.Boolean(),"是否启用该模块：1 启用，0 未启用"),
        col("status",code(32),"步骤状态：保留未启用、未开始、被阻断、运行及各模块实际结束状态"),
        col("continuation_allowed",sa.Boolean(),"是否允许后续模块继续：不代表本模块结果完整",default="0"),
        time("started_at","步骤开始时间"),time("finished_at","步骤结束时间"),
        payload("warnings","步骤警告"),payload("blocking_issues","阻止继续的业务问题"),payload("payload","未执行原因、模块摘要与结果位置等扩展信息"))
    unique(steps,"uq_step_module","run_id","module")
    unique(steps,"uq_step_ownership","id","run_id","module")

    batches = table("collection_batches", "采集模块批次表：保存实际采集结果、范围、覆盖依据、派生来源和发布状态",
        identity(),col("platform",code(32),"采集平台：与所属商品平台一致"),
        col("source_batch_key",code(96),"原模块批次编号：历史原值或带来源标记的导入键"),
        col("task_id",code(32),"所属任务编号：关联 tasks.id；未知时为空",nullable=True),
        col("run_id",number(),"所属运行编号：关联 collection_runs.id；独立模块或旧数据可空",nullable=True),
        col("step_id",number(),"所属模块步骤编号：关联 collection_steps.id；无整次汇总时可空",nullable=True),
        col("product_id",number(),"商品内部编号：关联 products.id；列表批次无单一商品时为空",nullable=True),
        col("module",code(32),"模块名称：list 或已接入的八种详情模块"),
        col("status",code(32),"原结果状态：执行完成不等于平台全部数据已采集"),
        col("scope",sa.String(128),"原采集范围名称：保留来源口径",nullable=True),
        col("scope_key",code(),"采集范围键：带版本的规范化条件 SHA-256，区分模式、上限、筛选和排序"),
        col("collection_mode",code(24),"采集模式：原 limited、all 等值；未知为空",nullable=True),
        col("requested_limit",number(),"本次记录数量上限：条或题；空值含义需结合模式区分无限制和未知",nullable=True),
        col("answer_limit",number(),"每题回答数量上限：条；空值需结合模式解释",nullable=True),
        col("selection_filter",sa.String(255),"页面实际筛选条件：未知为空，不能默认全部",nullable=True),
        col("sort_label",sa.String(128),"页面实际排序名称：未知为空",nullable=True),
        col("coverage_status",code(32),"数据覆盖状态：limit_reached、visible_end_confirmed、manual_count_confirmed、confirmed_empty、incomplete、unknown"),
        col("stop_reason",code(64),"采集停止原因：保留原 limit_reached、stable_bottom 等值",nullable=True),
        col("end_confirmed",sa.Boolean(),"可见末尾是否确认：空值为来源未知，不等于平台全量",nullable=True),
        payload("completion_evidence","末尾或人工数量确认的证据与方法"),
        time("captured_at","来源记录标注的采集时间"),time("observed_at","实际采集观察时间"),time("confirmed_at","人工确认时间"),
        col("derived_from_batch_id",number(),"来源批次内部编号：关联本表 id，缺失时为空",nullable=True),
        col("upstream_batch_key",code(96),"来源批次原编号：即使父批次缺失仍保留",nullable=True),
        col("derivation_kind",code(32),"派生方式：manual_confirmation、resume_reuse 等；非派生为空",nullable=True),
        col("visibility",code(16),"发布可见性：staging 暂存、published 已发布、quarantined 隔离",default="'staging'"),
        col("source_url",sa.Text(),"采集来源链接：受控保存原必要路由信息",nullable=True),
        col("schema_version",sa.Integer(),"来源数据格式版本：未声明时为空",nullable=True),
        payload("summary","批次计数、缺失项和采集口径摘要"),
        payload("result_payload","商品信息、规格组、兼容原结构的业务扩展；大体积原始证据保留在文件"),imported())
    unique(batches,"uq_batch_source","platform","module","source_batch_key")
    unique(batches,"uq_batch_module","id","module")
    unique(batches,"uq_batch_task","id","task_id")
    unique(batches,"uq_batch_current_owner","id","product_id","module","scope_key")
    check(batches,"ck_batch_module","module IN ("+",".join(repr(m) for m in MODULES)+")")
    check(batches,"ck_batch_visibility","visibility IN ('staging','published','quarantined')")
    check(batches,"ck_batch_coverage","coverage_status IN ('limit_reached','visible_end_confirmed','manual_count_confirmed','confirmed_empty','incomplete','unknown')")
    check(batches,"ck_batch_step_run","step_id IS NULL OR run_id IS NOT NULL")
    check(batches,"ck_batch_run_product","run_id IS NULL OR product_id IS NOT NULL")
    sa.Index("ix_batch_product_scope_time",batches.c.product_id,batches.c.module,batches.c.scope_key,batches.c.captured_at,batches.c.id)

    current = table("product_current_sections", "商品当前有效结果表：按商品、分区与采集范围指向有效历史批次",
        col("product_id",number(),"商品内部编号：关联 products.id",primary=True),
        col("section",code(32),"当前结果分区：product_info 和 sku_details 均对应 skus 模块",primary=True),
        col("scope_key",code(),"采集范围键：必须与目标批次一致",primary=True),
        col("module",code(32),"对应模块名称：用于复合外键核对分区映射和商品归属"),
        col("batch_id",number(),"当前有效批次编号：关联 collection_batches.id"),time("published_at","原有效结果发布时间"),
        col("publication_basis",sa.String(255),"发布依据：原分区恢复或已通过的完整性校验，不能由导入顺序推断"))
    check(current,"ck_current_section_module","(section IN ('product_info','sku_details') AND module='skus') OR (section=module AND section IN ('video','main_images','parameters','detail_images','review_summary','reviews','questions'))")

    pages = table("search_records", "页面搜索与入口记录表：保存搜索或详情入口的真实观察，不等于详情采集完成",
        identity(),col("task_id",code(32),"所属任务编号：关联 tasks.id；未知时为空",nullable=True),
        col("product_id",number(),"商品内部编号：关联 products.id；搜索页或身份未知时为空",nullable=True),
        col("record_type",code(24),"记录类型：search 搜索或 detail_open 详情入口"),
        col("keyword",sa.String(100),"搜索关键词：详情入口无关键词时为空",nullable=True),
        col("page_number",sa.Integer(),"页面页码：从 1 开始，未知为空",nullable=True),
        col("sort_label",sa.String(128),"观察到的页面排序：未知为空",nullable=True),
        col("status",code(64),"来源实际状态：入口打开不提升为采集成功"),
        col("source_url",sa.Text(),"来源页面链接：公开必要路由",nullable=True),time("captured_at","页面检查或搜索记录时间"),
        payload("payload","输入类型、入口字段采集标记、页面核对、截图及原始搜索摘要"),imported())
    check(pages,"ck_page_type","record_type IN ('search','detail_open')")
    sa.Index("ix_search_keyword_time",pages.c.keyword,pages.c.captured_at,pages.c.id)

    def observation(name,title,module,*columns,primary_batch=False):
        """构造属于指定模块的明细；参数：表名、用途、模块和字段；返回：Table。"""
        cols=[] if primary_batch else [identity()]
        cols.extend([col("batch_id",number(),"所属模块批次编号：关联 collection_batches.id",primary=primary_batch),
                     col("module",code(32),"固定模块名称："+module+"，用于数据库约束核对",default=repr(module))])
        tab=table(name,title,*cols,*columns)
        fk(tab,"fk_"+name+"_batch",["batch_id","module"],["collection_batches.id","collection_batches.module"])
        check(tab,"ck_"+name+"_module","module="+repr(module))
        if not primary_batch:
            unique(tab,"uq_"+name+"_owner","id","batch_id")
        return tab

    listing = observation("list_observations","列表商品观察表：保留商品在某次列表中的位置、展示字段与缺失状态","list",
        col("product_id",number(),"商品内部编号：关联 products.id；身份未确认时为空",nullable=True),
        col("slot_index",sa.Integer(),"原始列表格子位置：同批次唯一，重复商品不同位置不合并"),
        col("product_index",sa.Integer(),"有效商品序号：保留来源顺序"),
        col("title",sa.Text(),"商品展示标题：本批列表原文"),
        col("price_text",sa.Text(),"展示价格原文：保留条件与说明",nullable=True),
        col("price_value",mysql.DECIMAL(20,6),"已解析价格：币种见扩展中的 currency_symbol；空值不是 0",nullable=True),
        col("price_label",sa.String(255),"价格口径标签：例如优惠前；不同口径不直接比较",nullable=True),
        col("sales_text",sa.String(255),"销量提示原文：模糊数量不转换为精确销量",nullable=True),
        col("shop_name",sa.String(255),"店铺显示名称：缺失时为空",nullable=True),
        col("location_text",sa.String(255),"页面地区原文：缺失时为空",nullable=True),
        col("ad_status",code(32),"广告识别状态：未识别不等于确认非广告"),
        col("identity_status",code(32),"商品身份确认状态：保留原 verified 或 unconfirmed 等值"),
        payload("field_states","字段提供、未提供、未识别和不适用状态映射"),
        payload("payload","商品链接、主图链接、榜单、属性服务标签、广告依据及其他列表字段"))
    unique(listing,"uq_list_slot","batch_id","slot_index")
    sa.Index("ix_list_product_batch",listing.c.product_id,listing.c.batch_id)
    sa.Index("ix_list_shop_batch",listing.c.shop_name,listing.c.batch_id)

    sku = observation("sku_observations","商品规格观察表：本批 SKU 组合、价格与图片确认状态，不制造全局 SKU 身份","skus",
        col("position",sa.Integer(),"候选组合序号：同批次唯一"),col("platform_sku_id",code(96),"平台 SKU 编号：来源未明确提供时为空",nullable=True),
        col("sku_name",sa.Text(),"SKU 完整名称：显示用途，不能单独作为跨批次身份"),
        payload("properties","规格组合数组：每项保留 group、name、value_id"),
        col("price_text",sa.Text(),"本轮价格展示原文：缺失时为空",nullable=True),
        col("price_value",mysql.DECIMAL(20,6),"本轮已解析价格：币种见 currency；未确认时为空，不补 0",nullable=True),
        col("price_label",sa.String(255),"本轮价格口径：例如优惠前",nullable=True),
        col("currency",code(16),"币种：来源明确的货币代码或标记，未知为空",nullable=True),
        col("price_status",code(48),"价格确认状态：collected、unavailable_unconfirmed 等来源值"),
        col("stock_status",code(48),"库存或可选状态：未知不等于无货",nullable=True),
        col("combination_confirmed",sa.Boolean(),"规格组合是否已在页面核对：1 是，0 否"),
        col("image_status",code(48),"图片处理状态：下载、复用、失败或无图等实际结果",nullable=True),
        time("captured_at","SKU 采集时间"),time("price_checked_at","本轮价格核对时间"),
        payload("payload","多价格展示数组、数量、不可选证据、规格身份与图片异常"))
    unique(sku,"uq_sku_position","batch_id","position")
    sa.Index("ix_sku_price",sku.c.batch_id,sku.c.price_value)

    params = observation("parameter_observations","商品参数观察表：保留参数名称、值、原顺序与识别状态","parameters",
        col("position",sa.Integer(),"参数原始序号：同批次唯一"),col("name",sa.String(255),"参数名称：规范化显示值"),
        col("name_raw",sa.Text(),"参数名称原文：未做语义合并",nullable=True),col("value",sa.Text(),"参数值：保留来源原文"),
        col("status",code(32),"参数识别状态：保留原值"),payload("payload","参数来源、DOM 路径和原文片段"))
    unique(params,"uq_parameter_position","batch_id","position")

    overview = observation("review_summaries","评价概览表：保存评价数量口径、好评率、统计周期和总结标签","review_summary",
        col("count_raw",sa.String(255),"评价数量原文：例如 1万+",nullable=True),
        col("count_value",number(),"解析数量：仅按原解析口径使用，未知为空",nullable=True),
        col("count_lower_bound",number(),"数量下界：模糊数量的保守下界，未知为空",nullable=True),
        col("count_is_exact",sa.Boolean(),"数量是否精确：未知为空，模糊数量不能标精确",nullable=True),
        col("positive_rate_raw",sa.String(255),"好评率原文：未知为空",nullable=True),
        col("positive_rate_percent",mysql.DECIMAL(9,6),"好评率百分数：单位百分比，98 表示 98%；未知为空",nullable=True),
        col("period_raw",sa.String(255),"好评率统计时间范围原文：未知为空",nullable=True),
        payload("tags","评价总结标签数组：index、label、count_raw、count、status 等"),
        payload("payload","概览来源状态、原始展示、口径及诊断摘要"),primary_batch=True)
    check(overview,"ck_review_rate_range","positive_rate_percent IS NULL OR positive_rate_percent BETWEEN 0 AND 100")

    def identity_fields(kind):
        """声明评价问答身份；参数：对象种类；返回：字段列表。"""
        return [col("position",sa.Integer(),"批次内原始序号：从 1 开始"),
                col(kind+"_key",code(160),"批次内记录键：平台编号或指纹加出现次数，不强制跨批次合并"),
                col("platform_"+kind+"_id",code(96),"平台原始编号：未提供时为空",nullable=True),
                col("identity_method",code(48),"身份识别方式：平台编号或 fingerprint_occurrence"),
                col("identity_exact",sa.Boolean(),"身份是否可精确确认：指纹记录必须保留否"),
                col("author",sa.String(255),"用户显示名称：来源未提供时为空，不代表真实实名",nullable=True),
                col("date_raw",sa.String(255),"页面日期原文：相对日期不强行推算成精确时间",nullable=True)]

    reviews = observation("review_observations","用户评价观察表：保存评价、追评及批次内身份依据","reviews",
        *identity_fields("review"),col("sku_text",sa.Text(),"购买规格原文：来源未提供时为空",nullable=True),
        col("content",sa.Text(),"评价正文：保留原文，不合并匿名用户"),
        payload("append_reviews","追加评价数组：保留正文、日期与原有媒体字段"),time("captured_at","评价实际采集时间"),
        payload("payload","模板过滤口径、未展示媒体说明及兼容原评价扩展"))
    questions = observation("question_observations","商品问题观察表：保存问题、身份依据及回答采集完整性","questions",
        *identity_fields("question"),col("question",sa.Text(),"问题正文：保留原文"),
        col("answers_complete",sa.Boolean(),"本题回答是否确认完整：只针对页面可提供范围"),
        col("answer_stop_reason",code(64),"回答停止依据：如 exact_answer_count、explicit_end、not_expanded"),
        time("captured_at","问题实际采集时间"),payload("payload","回答展示数量、预览、完整性依据及问题扩展"))
    for tab,kind in [(reviews,"review"),(questions,"question")]:
        unique(tab,"uq_"+kind+"_position","batch_id","position")
        unique(tab,"uq_"+kind+"_key","batch_id",kind+"_key")

    answers = table("answer_observations","商品回答观察表：按内部问题主键保存回答、身份、规格和标签",
        identity(),col("question_id",number(),"所属问题内部编号：关联 question_observations.id，不是平台问题编号"),
        col("batch_id",number(),"所属问答批次编号：与父问题批次一致，用于复合外键校验"),
        *identity_fields("answer"),col("content",sa.Text(),"回答正文：保留原文"),
        col("sku_text",sa.Text(),"购买规格原文：来源未提供时为空",nullable=True),
        time("captured_at","回答实际采集时间"),payload("payload","用户身份标签、购买时间标签、媒体说明及原回答扩展"))
    unique(answers,"uq_answer_position","question_id","position")
    unique(answers,"uq_answer_key","question_id","answer_key")
    unique(answers,"uq_answer_owner","id","batch_id")

    files = table("file_assets","本地文件索引表：记录数据根目录下文件的位置、属性、校验和可用性",
        identity(),col("root_key",code(32),"文件根目录配置键：例如 workspace，不保存固定盘符作为关联身份"),
        col("relative_path",sa.Text(),"相对文件路径：展示原路径，禁止越过配置根目录"),
        col("path_key",code(),"规范路径键：根目录规则下相对路径 SHA-256；Windows 按大小写不敏感规则规范化"),
        col("size_bytes",number(),"文件大小：单位字节；未核实时为空",nullable=True),
        col("sha256",code(),"文件内容哈希：SHA-256 十六进制；未核实时为空",nullable=True),
        col("content_type",sa.String(128),"文件 MIME 类型：来源未知时为空",nullable=True),
        col("width",sa.Integer(),"媒体宽度：单位像素；不适用或未知为空",nullable=True),
        col("height",sa.Integer(),"媒体高度：单位像素；不适用或未知为空",nullable=True),
        col("duration_seconds",mysql.DECIMAL(16,6),"视频时长：单位秒；不适用或未知为空",nullable=True),
        col("availability",code(24),"文件可用状态：present、missing、unverified"),time("verified_at","最近文件核对时间"))
    unique(files,"uq_file_path","root_key","path_key")
    sa.Index("ix_file_sha256",files.c.sha256)
    check(files,"ck_file_availability","availability IN ('present','missing','unverified')")

    media = table("media_references","媒体与证据引用表：关联源链接、用途、采集归属和可选本地文件",
        identity(),col("reference_key",code(),"引用去重键：归属、用途及顺序的规范化 SHA-256"),
        col("task_id",code(32),"所属任务编号：关联 tasks.id；没有任务上下文时为空",nullable=True),
        col("batch_id",number(),"所属模块批次编号：关联 collection_batches.id；仅任务级证据时可空",nullable=True),
        *[col(name,number(),title+"：关联 "+target+".id；最多填写一种具体明细归属",nullable=True) for name,title,target in [
            ("sku_observation_id","所属 SKU 观察编号","sku_observations"),
            ("review_observation_id","所属评价观察编号","review_observations"),
            ("question_observation_id","所属问题观察编号","question_observations"),
            ("answer_observation_id","所属回答观察编号","answer_observations")]],
        col("role",code(48),"引用用途：主图、SKU 图、详情图、视频、截图、HTML 或买家媒体等规范代码"),
        col("position",sa.Integer(),"用途内显示顺序：保留原始顺序"),
        col("source_url",sa.Text(),"原始媒体或证据链接：受控保存，Web 不直接推送签名参数",nullable=True),
        col("file_asset_id",number(),"本地文件编号：关联 file_assets.id；仅链接时为空",nullable=True),
        col("download_status",code(24),"下载状态：link_only、downloaded、reused、failed、not_applicable"),
        payload("payload","媒体来源、封面、追评归属、复用校验与未识别说明"))
    unique(media,"uq_media_reference","reference_key")
    owner_sum=" + ".join("("+n+" IS NOT NULL)" for n in ("sku_observation_id","review_observation_id","question_observation_id","answer_observation_id"))
    check(media,"ck_media_one_detail",owner_sum+" <= 1")
    check(media,"ck_media_owner","task_id IS NOT NULL OR batch_id IS NOT NULL OR "+owner_sum+" > 0")
    check(media,"ck_media_detail_batch","("+owner_sum+") = 0 OR batch_id IS NOT NULL")
    check(media,"ck_media_local_file","download_status NOT IN ('downloaded','reused') OR file_asset_id IS NOT NULL")

    checkpoints = table("collection_checkpoints","采集检查点表：保留去重键、逐项状态与复用依据，不直接代表可断点续跑",
        identity(),col("batch_id",number(),"所属模块批次编号：关联 collection_batches.id"),
        col("checkpoint_type",code(48),"检查点类型：如 review_seen、image_item、sku_phase"),
        col("checkpoint_key",code(192),"同类型检查点键：保留批次内身份或来源规范化指纹"),
        col("source_sequence",number(),"原处理顺序：未知为空",nullable=True),
        payload("payload","去重跳过标记、阶段、下载状态、来源清单及复用信息"),time("updated_at","检查点更新时间"))
    unique(checkpoints,"uq_checkpoint_key","batch_id","checkpoint_type","checkpoint_key")

    sources = table("import_sources","导入来源文件表：登记一次导入使用的文件版本、格式和解析状态",
        identity(),col("import_run_id",number(),"所属导入执行编号：关联 import_runs.id"),
        col("root_key",code(32),"来源根目录配置键：与配置的数据目录对应"),
        col("relative_path",sa.Text(),"来源文件相对路径：保留文件版本位置"),
        col("sha256",code(),"来源内容哈希：SHA-256；文件追加变化不能导致旧业务记录重复导入"),
        col("size_bytes",number(),"来源文件大小：单位字节"),
        col("source_kind",code(48),"来源类型：任务库、模块元数据、明细、进度库、事件或文本日志等"),
        col("schema_version",sa.Integer(),"来源声明的格式版本：未声明时为空",nullable=True),
        col("parse_status",code(24),"解析状态：pending、parsed、failed、skipped 等"))
    unique(sources,"uq_import_source_owner","id","import_run_id")
    mappings = table("import_row_mappings","导入记录映射表：保留每份源记录与目标记录的逻辑映射及内容指纹",
        identity(),col("source_id",number(),"来源文件编号：关联 import_sources.id"),
        col("source_record_key",sa.Text(),"源记录位置或身份：行号、JSON 路径或旧主键"),
        col("source_record_hash",code(),"源记录键哈希：SHA-256，用于稳定的有界唯一索引"),
        col("logical_key",code(),"业务逻辑身份哈希：不包含本次导入编号，支持跨次核对"),
        col("target_table",code(64),"目标表名：受控名单中的业务表，属于逻辑关联而非物理外键"),
        col("target_key",sa.Text(),"目标主键：规范化文本，复合主键使用明确字段的 JSON 对象"),
        col("target_key_hash",code(),"目标主键哈希：SHA-256，碰撞或内容不符须隔离核对"),
        col("payload_fingerprint",code(),"源业务内容指纹：规范化内容 SHA-256，变更不能静默覆盖"),
        col("result",code(24),"映射结果：inserted、matched、skipped、conflict 等"))
    unique(mappings,"uq_import_mapping","source_id","source_record_hash","target_table","target_key_hash")
    sa.Index("ix_mapping_logical",mappings.c.logical_key)
    issues = table("import_issues","导入问题记录表：保存输入缺失、内容冲突、格式异常和处理结果",
        identity(),col("import_run_id",number(),"所属导入执行编号：关联 import_runs.id"),
        col("source_id",number(),"相关来源文件编号：关联 import_sources.id；全局问题可空",nullable=True),
        col("source_record_key",sa.Text(),"相关源记录位置或身份：无法定位时为空",nullable=True),
        col("severity",code(16),"问题级别：info、warning、error"),col("code",code(64),"稳定问题代码：供分类查询与人工处理"),
        col("message",sa.Text(),"问题说明：不包含密码、Cookie 或令牌"),
        col("resolution",sa.Text(),"处理结论：未处理时为空",nullable=True))

    events = table("task_events","任务事件日志表：统一保存任务进度、日志、异常与人工提示，支持稳定任务游标",
        identity(),col("task_id",code(32),"所属任务编号：关联 tasks.id；无法可靠关联的历史日志为空",nullable=True),
        col("task_sequence",number(),"任务内提交序号：同任务锁内分配；无任务归属时为空",nullable=True),
        col("run_id",number(),"关联运行编号：关联 collection_runs.id；无该上下文时为空",nullable=True),
        col("batch_id",number(),"关联模块批次编号：关联 collection_batches.id；无该上下文时为空",nullable=True),
        col("event_type",code(64),"事件类型：log 或来源原事件代码，不由文本猜测控制动作"),
        col("level",code(16),"日志级别：DEBUG、INFO、WARNING、ERROR、CRITICAL；源未提供可空",nullable=True),
        col("logger_name",sa.String(255),"日志模块名称：历史源未提供时为空",nullable=True),
        col("step",sa.String(255),"业务步骤名称：无上下文时为空",nullable=True),
        col("message",sa.Text(),"事件提示或多行日志正文：保留必要摘要，不记录密钥",nullable=True),
        col("state",code(32),"来源执行状态：与事件类型分开，未知为空",nullable=True),
        time("occurred_at","事件发生时间"),time("recorded_at","事件入库时间",nullable=False),
        col("time_raw",sa.String(128),"来源时间原文：无法确认时区时保留且不补造 UTC",nullable=True),
        col("time_basis",code(32),"时间依据：explicit_offset、utc、unknown 等，说明是否可靠"),
        payload("details","进度、阻断类型、场景位置与来源记录定位等结构化上下文"),
        col("origin",code(16),"事件来源：live 实时或 historical 历史导入"),
        col("event_key",code(96),"事件幂等键：本地流水与数据库补写共用，不能使用每次重试的新编号"),imported())
    unique(events,"uq_event_key","event_key")
    unique(events,"uq_event_task_sequence","task_id","task_sequence")
    check(events,"ck_event_task_sequence","(task_id IS NULL AND task_sequence IS NULL) OR (task_id IS NOT NULL AND task_sequence IS NOT NULL AND task_sequence>0)")
    check(events,"ck_event_origin","origin IN ('live','historical')")
    sa.Index("ix_event_time",events.c.occurred_at,events.c.id)
    sa.Index("ix_event_level_time",events.c.level,events.c.occurred_at,events.c.id)

    # 外键统一在所有表创建后添加，正确处理标题来源、派生来源等环形建表依赖。
    for tab in (tasks,events,runs,batches,pages):
        fk(tab,"fk_"+tab.name+"_import",["import_run_id"],["import_runs.id"])
    for tab in (events,runs,batches,pages,media):
        fk(tab,"fk_"+tab.name+"_task",["task_id"],["tasks.id"])
    for tab in (runs,batches,pages,listing,current):
        fk(tab,"fk_"+tab.name+"_product",["product_id"],["products.id"])
    fk(products,"fk_product_title_batch",["title_source_batch_id"],["collection_batches.id"])
    fk(steps,"fk_step_run",["run_id"],["collection_runs.id"])
    fk(batches,"fk_batch_run",["run_id"],["collection_runs.id"])
    fk(batches,"fk_batch_run_product",["run_id","product_id"],["collection_runs.id","collection_runs.product_id"])
    fk(batches,"fk_batch_run_task",["run_id","task_id"],["collection_runs.id","collection_runs.task_id"])
    fk(batches,"fk_batch_step",["step_id"],["collection_steps.id"])
    fk(batches,"fk_batch_step_owner",["step_id","run_id","module"],["collection_steps.id","collection_steps.run_id","collection_steps.module"])
    fk(batches,"fk_batch_source",["derived_from_batch_id"],["collection_batches.id"])
    fk(current,"fk_current_batch_owner",["batch_id","product_id","module","scope_key"],["collection_batches.id","collection_batches.product_id","collection_batches.module","collection_batches.scope_key"])
    fk(answers,"fk_answer_question_owner",["question_id","batch_id"],["question_observations.id","question_observations.batch_id"])
    fk(checkpoints,"fk_checkpoint_batch",["batch_id"],["collection_batches.id"])
    fk(sources,"fk_import_source_run",["import_run_id"],["import_runs.id"])
    fk(mappings,"fk_import_mapping_source",["source_id"],["import_sources.id"])
    fk(issues,"fk_import_issue_run",["import_run_id"],["import_runs.id"])
    fk(issues,"fk_import_issue_source",["source_id","import_run_id"],["import_sources.id","import_sources.import_run_id"])
    for tab in (events,media):
        fk(tab,"fk_"+tab.name+"_batch",["batch_id"],["collection_batches.id"])
        fk(tab,"fk_"+tab.name+"_batch_task",["batch_id","task_id"],["collection_batches.id","collection_batches.task_id"])
    fk(events,"fk_event_run",["run_id"],["collection_runs.id"])
    fk(events,"fk_event_run_task",["run_id","task_id"],["collection_runs.id","collection_runs.task_id"])
    for column,parent in [("sku_observation_id",sku),("review_observation_id",reviews),("question_observation_id",questions),("answer_observation_id",answers)]:
        fk(media,"fk_media_"+parent.name,[column,"batch_id"],[parent.name+".id",parent.name+".batch_id"])
    fk(media,"fk_media_file",["file_asset_id"],["file_assets.id"])
    return metadata
