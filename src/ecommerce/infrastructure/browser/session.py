"""浏览器生命周期和登录状态存取。"""

import json
import logging
from contextlib import contextmanager
from uuid import uuid4
from ecommerce.shared.config import Settings


def _read_session_state(settings: Settings) -> dict | None:
    """读取已保存会话；参数：配置；返回：有效状态，无文件或格式错误返回空值。"""
    if not settings.session_path.is_file():
        return None
    try:
        state = json.loads(settings.session_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list) or not isinstance(state.get("origins", []), list):
            raise ValueError("登录状态结构不合法")
        return state
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("登录状态格式无效，将检查持久化会话或重新扫码")
        return None


def load_session(context, settings: Settings) -> bool:
    """注入状态文件；参数：浏览器上下文、配置；返回：是否读取有效文件。"""
    state = _read_session_state(settings)
    if state is None:
        return False
    try:
        origins = {item["origin"]: {entry["name"]: entry["value"] for entry in item.get("localStorage", [])} for item in state.get("origins", [])}
        context.add_cookies(state.get("cookies", []))
        context.add_init_script("(() => {const values = " + json.dumps(origins) + "; const data = values[location.origin]; if(data) for(const [key,value] of Object.entries(data)) localStorage.setItem(key,value);})();")
        return True
    except (ValueError, TypeError, KeyError):
        logging.getLogger(__name__).warning("登录状态格式无效，将检查持久化会话或重新扫码")
        return False


def restore_missing_session_cookies(context, settings: Settings) -> int:
    """补回关闭浏览器后丢失的临时 Cookie；参数：上下文、配置；返回：补回数量。"""
    state = _read_session_state(settings)
    if state is None:
        return 0
    try:
        def key(cookie):
            """按浏览器 Cookie 身份匹配，分区 Cookie 单独处理。"""
            return cookie["domain"], cookie["path"], cookie["name"], cookie.get("partitionKey")

        existing = {key(cookie) for cookie in context.cookies()}
        missing = [cookie for cookie in state.get("cookies", [])
                   if cookie.get("expires", -1) == -1 and key(cookie) not in existing]
    except (ValueError, TypeError, KeyError, AttributeError):
        logging.getLogger(__name__).warning("登录状态 Cookie 格式无效，未补回临时会话")
        return 0
    if missing:
        # 不覆盖现有 Cookie，不回写旧 localStorage，不延长平台设置的有效期。
        context.add_cookies(missing)
        logging.getLogger(__name__).info("已从保存的会话补回 %s 条缺失的临时 Cookie", len(missing))
    return len(missing)


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
