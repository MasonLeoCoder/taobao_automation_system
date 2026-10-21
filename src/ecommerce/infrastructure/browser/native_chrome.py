"""普通方式启动独立 Chrome，通过本机 CDP 连接；不改写页面环境。"""
import logging
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import json

from ecommerce.infrastructure.browser.session import load_session
from ecommerce.shared.exceptions import NeedsAttention


def find_chrome():
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        path = Path(base) / "Google/Chrome/Application/chrome.exe"
        if path.is_file():
            return path
    raise ValueError("未找到系统 Chrome，普通启动连接模式需要已安装的 Chrome")


def available_local_port():
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        return temporary.getsockname()[1]


def wait_for_endpoint(process, endpoint, timeout_ms):
    opener = build_opener(ProxyHandler({}))
    expected = urlsplit(endpoint)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise NeedsAttention("Chrome 启动进程已退出，请确认淘宝资料未被其他窗口占用")
        try:
            with opener.open(endpoint + "/json/version", timeout=min(1, max(.05, deadline - time.monotonic()))) as response:
                value = json.load(response)
            target = urlsplit(value.get("webSocketDebuggerUrl", ""))
            if target.scheme != "ws" or target.hostname not in {"127.0.0.1", "localhost"} or target.port != expected.port:
                raise NeedsAttention("本机 Chrome 连接入口与预期端口不一致，未操作页面")
            return
        except (OSError, ValueError):
            time.sleep(.1)
    raise NeedsAttention("等待本机 Chrome 连接入口超时，未执行页面操作")


@contextmanager
def browser_session(settings, *, restore_state=True):
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    if settings.headless:
        raise ValueError("普通启动连接模式目前需要可见 Chrome，请关闭 headless")
    if settings.taobao_browser_channel != "chrome":
        raise ValueError("普通启动连接模式目前仅支持 browser_channel=chrome")
    executable = find_chrome()
    settings.profile_path.mkdir(parents=True, exist_ok=True)
    port = available_local_port()
    endpoint = f"http://127.0.0.1:{port}"
    process = subprocess.Popen([
        str(executable), f"--user-data-dir={settings.profile_path}", "--start-maximized", "--no-first-run",
        f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", "about:blank",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = None
    owner_confirmed = False
    logger = logging.getLogger(__name__)
    try:
        wait_for_endpoint(process, endpoint, settings.timeout_ms)
        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(endpoint, timeout=settings.timeout_ms, no_defaults=True)
            session = browser.new_browser_cdp_session()
            info = session.send("SystemInfo.getProcessInfo")["processInfo"]
            owners = [int(item["id"]) for item in info if item["type"] == "browser"]
            if owners != [process.pid]:
                raise NeedsAttention("无法确认连接的是本次启动的 Chrome，未读取或操作页面")
            owner_confirmed = True
            try:
                if len(browser.contexts) != 1:
                    raise NeedsAttention("Chrome 默认浏览器会话数量异常，未继续操作")
                context = browser.contexts[0]
                context.set_default_timeout(settings.timeout_ms)
                context.set_default_navigation_timeout(settings.timeout_ms)
                if restore_state:
                    load_session(context, settings)
                page = context.pages[0] if context.pages else context.new_page()
                logger.info("淘宝使用普通 Chrome 启动及本机连接，版本 %s", browser.version)
                yield context, page
            finally:
                # 只对本次创建、已核对进程身份的 Chrome 请求正常关闭，保存持久化资料。
                try:
                    session.send("Browser.close")
                except PlaywrightError:
                    pass
    finally:
        if process.poll() is None:
            if owner_confirmed:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("本次 Chrome 未能正常退出，仅结束本次启动进程")
                    process.terminate()
                    process.wait(timeout=5)
            else:
                # 未确认连接身份时不向所连接的浏览器发关闭指令。
                process.terminate()
                process.wait(timeout=5)
