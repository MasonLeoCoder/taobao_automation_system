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
    product = commands.add_parser("product", help="商品入口导航")
    product.add_argument("action", choices=("open",))
    product.add_argument("--start", action="store_true", help="尝试点击开启按钮，旧定位问题待验证")
    product.add_argument("--no-wait", action="store_true", help="完成后立即关闭浏览器")
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
            result = execute(settings, args.action)
        else:
            from ecommerce.cli.commands.product import execute
            legacy = args.command == "upload"
            if legacy:
                print("旧命令 upload 对应 product open --start；尚不支持完整上架。", file=sys.stderr)
            result = execute(settings, legacy or args.start, legacy or not args.no_wait)
        print(f"[{result.status}] {result.message}")
        if result.task_id:
            print(f"任务编号：{result.task_id}")
        return result.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"执行失败：{error}", file=sys.stderr)
        return 1
