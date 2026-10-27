"""浏览器生命周期和登录状态存取。"""

import json
import logging
import os
import time
from contextlib import contextmanager
from uuid import uuid4
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import AutomationError


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
    """单次导出并原子保存登录状态；参数：上下文、配置；返回：无，失败保留原文件并抛错。"""
    logger=logging.getLogger(__name__)
    state=context.storage_state()
    try:
        if not isinstance(state,dict) or not isinstance(state.get('cookies'),list) or not isinstance(state.get('origins'),list):
            raise ValueError('登录状态结构不合法')
        payload=json.dumps(state,ensure_ascii=False,allow_nan=False)
    except (TypeError,ValueError) as error:
        message='登录状态格式无效，未替换原登录文件'
        logger.error(message)
        raise AutomationError(message) from error
    destination=settings.session_path
    temporary=destination.with_name('.'+destination.name+'-'+uuid4().hex+'.tmp')
    created=False
    attempts=0
    stage='创建登录状态目录'
    try:
        destination.parent.mkdir(parents=True,exist_ok=True)
        stage='写入临时登录文件'
        with temporary.open('x',encoding='utf-8',newline='\n') as handle:
            created=True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        stage='替换登录状态文件'
        for attempts in range(1,4):
            try:
                temporary.replace(destination)
                return
            except OSError as error:
                if getattr(error,'winerror',None) not in {5,32,33} or attempts==3:
                    raise
                delay=(.2,.5)[attempts-1]
                logger.warning('登录状态文件暂未替换：目标=%s；临时=%s；尝试=%s/3；系统错误=%s；%.1f秒后重试',
                    destination,temporary,attempts,_file_error_code(error),delay)
                time.sleep(delay)
    except OSError as error:
        message=(f'登录状态保存失败：{stage}；目标={destination}；临时={temporary}；'
            f'替换尝试={attempts}/3；系统错误={_file_error_code(error)}；原登录文件未替换，请检查文件占用或写入权限')
        logger.error(message)
        raise AutomationError(message) from error
    finally:
        if created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                logger.warning('本次登录临时文件未能清理：%s；系统错误=%s',temporary,_file_error_code(error))


def _file_error_code(error):
    """提取系统错误代码，避免输出登录内容；参数：文件异常；返回：错误代码文本。"""
    code=getattr(error,'winerror',None)
    return f'WinError {code}' if code is not None else f'errno {error.errno}'


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
