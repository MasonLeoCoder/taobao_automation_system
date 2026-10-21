"""命令解析与入口调度。"""
import argparse
import json
import sys
from ecommerce.shared.config import load_settings, prepare_workspace
from ecommerce.shared.logging import setup_logging


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
    taobao = commands.add_parser("taobao", help="淘宝关键词搜索、列表采集、详情页入口与任务控制")
    taobao_actions = taobao.add_subparsers(dest="action", required=True)
    for action, description in (("pause", "暂停当前任务"), ("stop", "停止当前任务"), ("continue", "向当前提示发送继续"),
                                ("confirm", "确认当前原生弹窗"), ("cancel", "取消当前原生弹窗")):
        taobao_actions.add_parser(action, help=description)
    search = taobao_actions.add_parser("search", help="通过淘宝首页搜索并确认首屏结果")
    search.add_argument("--keyword", required=True, help="搜索关键词，1～100 个字符")
    search.add_argument("--no-wait", action="store_true", help="搜索确认并保存记录后关闭浏览器，默认保留窗口")
    collect = taobao_actions.add_parser("collect", help="按销量排序后采集第一页已加载商品，保存 JSON/CSV")
    collect.add_argument("--keyword", required=True, help="搜索关键词，1～100 个字符")
    collect.add_argument("--limit", type=int, default=None, help="最多保存的有效商品数，须为正整数；默认不限制当前页条数")
    collect.add_argument("--no-wait", action="store_true", help="保存商品记录后关闭浏览器，默认保留窗口")
    detail = taobao_actions.add_parser("detail", help="输入商品 ID 或链接打开详情页，供 DOM 字段核对")
    detail.add_argument("--input", required=True, help="纯数字商品 ID，或完整淘宝/天猫详情链接")
    detail.add_argument("--video", action="store_true", help="采集并下载主图区域的视频；无视频则返回无视频并跳过")
    detail.add_argument("--main-images", action="store_true", help="按图集顺序下载五张主图，保存至商品目录的 1-1主图")
    detail.add_argument("--skus", action="store_true", help="采集标题、售出件数、逐项SKU价格及图片，图片按规格名称保存至 sku图")
    detail.add_argument("--no-wait", action="store_true", help="保存入口记录后关闭浏览器，默认保留窗口")
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
    web = commands.add_parser("web", help="启动本地网页")
    web.add_argument("--port", type=int, help="本地服务端口")
    commands.add_parser("upload", help="兼容入口：product open --start")
    commands.add_parser("download", help="兼容入口：旧下载尚未实现")
    return root


def main(argv: list[str] | None = None) -> int:
    """执行命令；参数：可选参数列表；返回：进程退出码。"""
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 0
    try:
        settings = load_settings(args.config)
        prepare_workspace(settings)
        setup_logging(settings)
        if args.command == "taobao":
            if args.action in {"search", "collect", "detail"}:
                from ecommerce.cli.commands.collection import execute, execute_collect, execute_detail
                if args.action == "search":
                    result = execute(settings, args.keyword, wait=not args.no_wait)
                elif args.action == "collect":
                    result = execute_collect(settings, args.keyword, limit=args.limit, wait=not args.no_wait)
                else:
                    result = execute_detail(settings, args.input, wait=not args.no_wait, video=args.video, main_images=args.main_images, skus=args.skus)
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
            initialize(settings.database)
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
