"""命令解析与入口调度。"""
import argparse
import json
import sys
from ecommerce.shared.config import load_settings, prepare_workspace
from ecommerce.shared.logging import setup_logging


DETAIL_STEPS = ("basic_info", "video", "main_images", "skus", "parameters", "detail_images", "review_summary", "reviews", "questions")


def parser() -> argparse.ArgumentParser:
    """建立命令结构；参数：无；返回：解析器。"""
    root = argparse.ArgumentParser(description="电商自动化工作台 · 单店铺")
    root.add_argument("--config", help="配置路径，相对项目根目录解析")
    commands = root.add_subparsers(dest="command")
    account = commands.add_parser("account", help="店铺账号与会话")
    account.add_argument("action", choices=("login", "check"))
    account.add_argument("--platform", choices=("qianniu", "taobao"), default="qianniu",
                         help="登录平台：qianniu 千牛（默认），taobao 淘宝买家端；会话独立保存")
    account.add_argument("--no-wait", action="store_true", help="仅淘宝 login：保存会话后自动关闭浏览器，默认保留窗口")
    taobao = commands.add_parser("taobao", help="淘宝关键词搜索、列表及详情采集与任务控制")
    taobao_actions = taobao.add_subparsers(dest="action", required=True)
    for action, description in (("pause", "暂停当前任务"), ("stop", "停止当前任务"), ("continue", "向当前提示发送继续"),
                                ("confirm", "确认当前原生弹窗"), ("cancel", "取消当前原生弹窗")):
        taobao_actions.add_parser(action, help=description)
    search = taobao_actions.add_parser("search", help="通过淘宝首页搜索并确认首屏结果")
    search.add_argument("--keyword", required=True, help="搜索关键词，1～100 个字符")
    search.add_argument("--no-wait", action="store_true", help="搜索确认并保存记录后关闭浏览器，默认保留窗口")
    collect = taobao_actions.add_parser("collect", help="按销量逐页采集搜索商品，默认仅第一页，每页独立保存 JSON/CSV")
    collect.add_argument("--keyword", required=True, help="搜索关键词，1～100 个字符")
    collect.add_argument("--limit", type=int, default=None, help="每页最多保存的有效商品数，须为正整数；默认不限制当前页条数")
    collect.add_argument("--page", type=int, default=1, help="从第一页起最多采集几页，须为正整数，默认1；--limit为每页上限")
    collect.add_argument("--with-details", action="store_true", help="本页列表入库后逐个采集默认九个详情模块，同任务成功商品详情复用，再翻下一页")
    collect.add_argument("--no-wait", action="store_true", help="保存商品记录后关闭浏览器，默认保留窗口")
    detail = taobao_actions.add_parser("detail", help="采集商品详情；不指定功能参数时采集全部已实现项目",
        description="输入商品 ID 或完整详情链接。默认采集全部已实现的详情项目；指定一个或多个功能参数时，只执行所选项目。")
    detail.add_argument("--input", required=True, help="纯数字商品 ID，或完整淘宝/天猫详情链接")
    detail.add_argument("--basic-info", action="store_true", help="独立采集标题、店铺、详情销量及页面展示价格，不遍历SKU")
    detail.add_argument("--video", action="store_true", help="采集并下载主图区域的视频；无视频则返回无视频并跳过")
    detail.add_argument("--main-images", action="store_true", help="读取缩略图DOM图片，按完整地址去重、依次编号保存至 1-1主图；同来源未完成批次可补采")
    detail.add_argument("--skus", action="store_true", help="只采集规格组合、逐项SKU价格及图片，图片按规格名称保存至 sku图")
    detail.add_argument("--parameters", action="store_true", help="动态读取参数信息，保留完整原文、同名项和原始HTML")
    detail.add_argument("--detail-images", action="store_true", help="按图文详情顺序下载全部图片，数字编号保存至商品目录的详情图")
    detail.add_argument("--review-summary", action="store_true", help="只采集评价数量、好评率和总结标签，不进入全部评价")
    detail.add_argument("--reviews", action="store_true", help="采集全部评价列表，默认最多100条有效评价，跳过模板好评，媒体只存链接")
    detail.add_argument("--questions", action="store_true", help="采集问大家，默认最多50个问题，每题采集页面可提供的全部回答，媒体只存链接")
    detail.add_argument("--all", dest="all_reviews", action="store_true", help="取消已启用的评价/问题数量上限；默认全功能模式下同时取消两者上限")
    detail.add_argument("--no-wait", action="store_true", help="所选详情步骤完成后关闭浏览器，默认保留窗口")
    product = commands.add_parser("product", help="商品入口导航")
    product.add_argument("action", choices=("open",))
    product.add_argument("--start", action="store_true", help="尝试点击开启按钮，旧定位问题待验证")
    product.add_argument("--no-wait", action="store_true", help="完成后立即关闭浏览器")
    product.add_argument("--submit", action="store_true", help="填写后提交到仓库，失败时提示音并等待人工检查")
    product.add_argument("--keyword", help="进入发布页面后搜索指定关键词")
    product.add_argument("--category", help="点击搜索结果中的完整类目路径，使用 > 分隔")
    product.add_argument("--brand", help="选择指定品牌并点击确认下一步，需同时提供类目")
    product.add_argument("--main-images-dir", help="按编号1～5上传五张1:1主图，相对路径基于项目根目录")
    product.add_argument("--portrait-images-dir", help="按顺序上传五张3:4主图，相对路径基于项目根目录")
    product.add_argument("--detail-images-dir", help="按数字编号上传全部详情图，长图使用平台完整分割")
    product.add_argument("--video-file", help="上传指定MP4商品视频，相对路径基于项目根目录")
    product.add_argument("--product-info-file", help="读取固定格式商品Excel，填写已接入的店铺分类、标题、商品属性及SKU信息")
    product.add_argument("--sku-images-dir", help="SKU图片目录，按销售规格名称匹配扩展名；默认Excel所在目录的sku图文件夹")
    commands.add_parser("status", help="显示当前功能状态")
    commands.add_parser("tasks", help="查看最近 50 条任务记录")
    logs = commands.add_parser('logs',help='分页查询 MySQL 任务和运行日志')
    logs.add_argument('--task-id',help='完整的 32 位任务编号；指定后按任务序号分页')
    logs.add_argument('--level',choices=('DEBUG','INFO','WARNING','ERROR','CRITICAL'))
    logs.add_argument('--since',help='开始时间，须含时区，例如 2026-10-23T00:00:00+08:00')
    logs.add_argument('--until',help='结束时间，须含时区')
    logs.add_argument('--after',type=int,default=0,help='上一页返回的 next_cursor，默认从 0 开始')
    logs.add_argument('--limit',type=int,default=100,help='每页数量，1–500')
    collections=commands.add_parser('collections',help='查询已入库商品、采集批次和明细')
    collection_actions=collections.add_subparsers(dest='action',required=True)
    for name,description in [('products','商品及来源'),('batches','实际批次、状态和覆盖范围'),('records','单批次明细')]:
        command=collection_actions.add_parser(name,help=description)
        command.add_argument('--after',type=int,default=0,help='上一页 next_cursor，默认 0')
        command.add_argument('--limit',type=int,default=50,help='每页 1–200，默认 50')
        if name=='products':
            command.add_argument('--keyword',help='标题文字或完整商品编号')
        elif name=='batches':
            command.add_argument('--product-id',help='平台原商品编号')
            command.add_argument('--module',choices=('list',*DETAIL_STEPS))
            command.add_argument('--include-quarantined',action='store_true',help='同时查询缺失文件等原因隔离的批次')
        else:
            command.add_argument('--batch-id',type=int,required=True,help='批次查询返回的内部编号')
            command.add_argument('--kind',choices=('basic_info','items','skus','parameters','reviews','questions','answers','media','checkpoints','review_summary'))
            command.add_argument('--question-id',type=int,help='仅 kind=answers：按问题内部编号筛选')
            command.add_argument('--include-quarantined',action='store_true')
    database = commands.add_parser("database", help="MySQL 基础连接、显式升级、字段字典与独立库验证")
    database_actions = database.add_subparsers(dest="action", required=True)
    database_actions.add_parser("check", help="只读核对连接、版本、表字段备注及关联约束")
    database_actions.add_parser("upgrade", help="使用迁移账号显式升级 MySQL 表结构")
    database_actions.add_parser('replay-events',help='补写属于当前库的本地事件流水；不执行浏览器动作')
    dictionary = database_actions.add_parser("dictionary", help="按同版结构生成完整中文字段字典")
    dictionary.add_argument("--output", default="docs/数据库字段字典.md", help="字典输出路径，相对项目根目录；重新生成会更新该文档")
    verify = database_actions.add_parser("verify", help="仅在 _verify 验证库用合成数据验证，结束回滚")
    verify.add_argument("--report", help="新建验证报告路径；默认在 workspace/output/reports 生成唯一文件，不覆盖已有报告")
    web = commands.add_parser("web", help="启动本地网页")
    web.add_argument("--port", type=int, help="本地服务端口")
    commands.add_parser("upload", help="兼容入口：product open --start")
    commands.add_parser("download", help="兼容入口：旧下载尚未实现")
    return root


def main(argv: list[str] | None = None) -> int:
    """执行命令；参数：可选参数列表；返回：进程退出码。"""
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "taobao" and args.action == "detail":
        # 数量和窗口参数不选择采集功能；未选功能才默认启用全部。
        if not any(getattr(args, step) for step in DETAIL_STEPS):
            for step in DETAIL_STEPS:
                setattr(args, step, True)
        if args.all_reviews and not (args.reviews or args.questions):
            cli.error("--all 必须与 --reviews 或 --questions 一起使用")
    if args.command is None:
        cli.print_help()
        return 0
    try:
        settings = load_settings(args.config)
        prepare_workspace(settings)
        setup_logging(settings)
        if args.command == "database":
            from ecommerce.cli.commands.database import execute as execute_database
            return execute_database(settings,args)
        if args.command=='logs':
            if settings.task_backend!='mysql':
                raise ValueError('当前任务源尚未启用 MySQL 日志查询')
            print(json.dumps(settings.task_database.list_events(task_id=args.task_id,level=args.level,since=args.since,
                until=args.until,after=args.after,limit=args.limit),ensure_ascii=False,indent=2))
            return 0
        if args.command=='collections':
            from ecommerce.modules.collection.queries import query
            values=vars(args).copy()
            for key in ['config','command','action']:
                values.pop(key,None)
            print(json.dumps(query(settings,args.action,**values),ensure_ascii=False,indent=2))
            return 0
        if args.command == "taobao":
            if args.action in {"search", "collect", "detail"}:
                from ecommerce.cli.commands.collection import execute, execute_collect, execute_detail
                if args.action == "search":
                    result = execute(settings, args.keyword, wait=not args.no_wait)
                elif args.action == "collect":
                    result = execute_collect(settings, args.keyword, limit=args.limit, pages=args.page, wait=not args.no_wait, with_details=args.with_details)
                else:
                    result = execute_detail(settings, args.input, wait=not args.no_wait, basic_info=args.basic_info, video=args.video, main_images=args.main_images, skus=args.skus, parameters=args.parameters, detail_images=args.detail_images, review_summary=args.review_summary, reviews=args.reviews, all_reviews=args.all_reviews, questions=args.questions)
                print(f"[{result.status}] {result.message}")
                if result.task_id:
                    print(f"任务编号：{result.task_id}")
                return result.exit_code
            else:
                from ecommerce.modules.account.taobao_control import request_control
                print(request_control(settings, args.action))
                return 0
        if args.command == "download":
            print("旧 download 只有登录骨架，报表下载尚未实现；登录请使用 account login。", file=sys.stderr)
            return 2
        if args.command == "status":
            from ecommerce.shared.features import FEATURES
            for feature in FEATURES:
                print(f"{feature['name']} [{feature['label']}] {feature['description']}")
            return 0
        if args.command == "tasks":
            from ecommerce.infrastructure.storage.database import initialize
            from ecommerce.modules.monitoring.service import recent_tasks
            initialize(settings.task_database)
            print(json.dumps(recent_tasks(settings), ensure_ascii=False, indent=2))
            return 0
        if args.command == "web":
            from ecommerce.web.app import create_app
            port = args.port if args.port is not None else settings.port
            if not 1 <= port <= 65535:
                raise ValueError("端口必须为 1–65535")
            create_app(settings).run(host=settings.host, port=port, debug=False, use_reloader=False)
            return 0
        if args.command == "account":
            from ecommerce.cli.commands.account import execute
            if args.no_wait and (args.platform != "taobao" or args.action != "login"):
                cli.error("account --no-wait 仅支持 login --platform taobao")
            result = execute(settings, args.action, args.platform, wait=not args.no_wait)
        else:
            from ecommerce.cli.commands.product import execute
            legacy = args.command == "upload"
            if legacy:
                print("旧命令 upload 对应 product open --start；尚不支持完整上架。", file=sys.stderr)
            result = execute(settings, legacy or args.start, legacy or not args.no_wait, getattr(args, "keyword", None), getattr(args, "category", None), getattr(args, "brand", None), getattr(args, "main_images_dir", None), getattr(args, "portrait_images_dir", None), getattr(args, "detail_images_dir", None), getattr(args, "video_file", None), getattr(args, "product_info_file", None), submit=getattr(args, "submit", False), sku_images_dir=getattr(args, "sku_images_dir", None))
        print(f"[{result.status}] {result.message}")
        if result.task_id:
            print(f"任务编号：{result.task_id}")
        return result.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"执行失败：{error}", file=sys.stderr)
        return 1
