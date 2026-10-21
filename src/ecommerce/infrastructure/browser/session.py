"""浏览器生命周期和登录状态存取。"""

import json
import logging
from contextlib import contextmanager
from uuid import uuid4
from ecommerce.shared.config import Settings


def load_session(context, settings: Settings) -> bool:
    """注入状态文件；参数：浏览器上下文、配置；返回：是否读取有效文件。"""
    if not settings.session_path.is_file():
        return False
    try:
        state = json.loads(settings.session_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list) or not isinstance(state.get("origins", []), list):
            raise ValueError("登录状态结构不合法")
        context.add_cookies(state.get("cookies", []))
        origins = {item["origin"]: {entry["name"]: entry["value"] for entry in item.get("localStorage", [])} for item in state.get("origins", [])}
        context.add_init_script("(() => {const values = " + json.dumps(origins) + "; const data = values[location.origin]; if(data) for(const [key,value] of Object.entries(data)) localStorage.setItem(key,value);})();")
        return True
    except (ValueError, TypeError, KeyError):
        logging.getLogger(__name__).warning("登录状态格式无效，将检查持久化会话或重新扫码")
        return False


def save_session(context, settings: Settings) -> None:
    """保存状态文件；参数：上下文、配置；返回：无。"""
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.session_path.with_suffix(".tmp")
    context.storage_state(path=str(temporary))
    temporary.replace(settings.session_path)


@contextmanager
def browser_session(settings: Settings, *, restore_state: bool = True, channel: str | None = None):
    """为任务复用浏览器；restore_state 控制是否注入导出的状态文件。"""
    from playwright.sync_api import sync_playwright
    settings.profile_path.mkdir(parents=True, exist_ok=True)
    launch_options = {"channel": channel} if channel is not None else {}
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_path), headless=settings.headless,
            no_viewport=not settings.headless,
            viewport={"width": 1440, "height": 1000} if settings.headless else None,
            args=[] if settings.headless else ["--start-maximized"],
            **launch_options,
        )
        try:
            context.set_default_timeout(settings.timeout_ms)
            context.set_default_navigation_timeout(settings.timeout_ms)
            if restore_state:
                load_session(context, settings)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield context, page
            except Exception:
                for current in context.pages[-1:]:
                    try:
                        destination = settings.log_dir / "screenshots" / f"failure-{uuid4().hex}.png"
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        current.screenshot(path=str(destination), timeout=5000)
                    except Exception:
                        logging.getLogger(__name__).warning("失败截图未能保存")
                raise
        finally:
            context.close()
