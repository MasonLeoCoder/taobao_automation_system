"""加载配置并统一解析项目路径。"""

import os
import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_TAOBAO_TIMING = {
    "before_search": (2, 4), "search": (2, 4), "scroll": (1.5, 3), "page": (4, 8), "keyword": (8, 15),
}


@dataclass(frozen=True)
class Settings:
    """单店铺配置，所有文件路径均为绝对路径。"""

    root: Path
    workspace: Path
    session_path: Path
    profile_path: Path
    shop_name: str = "我的店铺"
    headless: bool = False
    timeout_ms: int = 60000
    host: str = "127.0.0.1"
    port: int = 8000
    taobao_session_path: Path | None = None
    taobao_profile_path: Path | None = None
    taobao_timing: dict = field(default_factory=lambda: dict(DEFAULT_TAOBAO_TIMING))
    taobao_browser_channel: str | None = None
    taobao_browser_mode: str = "playwright"

    def for_platform(self, platform: str) -> "Settings":
        """选择平台的独立登录资料；千牛继续兼容原有路径。"""
        if platform == "qianniu":
            return self
        if platform != "taobao":
            raise ValueError(f"不支持的登录平台：{platform}")
        session = (self.taobao_session_path or self.workspace / "storage/sessions/taobao.json").resolve()
        profile = (self.taobao_profile_path or self.workspace / "storage/browser_profiles/taobao").resolve()
        qianniu_profile = self.profile_path.resolve()
        if session == self.session_path.resolve() or (
            profile == qianniu_profile or profile in qianniu_profile.parents or qianniu_profile in profile.parents
        ):
            raise ValueError("淘宝与千牛必须使用不同的登录状态文件和独立的浏览器资料目录")
        if session == qianniu_profile or qianniu_profile in session.parents or (
            self.session_path.resolve() == profile or profile in self.session_path.resolve().parents
        ):
            raise ValueError("一个平台的登录状态文件不能位于另一个平台的浏览器资料目录内")
        return replace(self, session_path=session, profile_path=profile)

    @property
    def database(self) -> Path:
        """返回 SQLite 文件路径；参数：无。"""
        return self.workspace / "storage/database/ecommerce.sqlite3"

    @property
    def log_dir(self) -> Path:
        """返回日志目录；参数：无。"""
        return self.workspace / "logs"


def load_settings(config_path: str | None = None, root: Path | None = None) -> Settings:
    """加载配置；参数：可选配置路径、项目根目录；返回：Settings。"""
    root = Path(root or os.environ.get("ECOMMERCE_ROOT") or Path(__file__).resolve().parents[3]).resolve()

    def resolve(value: str) -> Path:
        """解析配置路径；参数：路径字符串；返回：绝对路径。"""
        path = Path(value).expanduser()
        return (root / path).resolve() if not path.is_absolute() else path.resolve()

    selected = config_path or os.environ.get("ECOMMERCE_CONFIG")
    config = resolve(selected or "config/settings.toml")
    if selected and not config.is_file():
        raise ValueError(f"配置文件不存在：{config}")
    values = tomllib.loads(config.read_text(encoding="utf-8-sig")) if config.exists() else {}
    workspace = resolve(values.get("workspace", "workspace"))
    browser = values.get("browser", {})
    taobao = values.get("taobao", {})
    taobao_channel = taobao.get("browser_channel")
    if taobao_channel is not None and taobao_channel not in ("chrome", "msedge"):
        raise ValueError("taobao.browser_channel 仅支持 chrome 或 msedge；不设置时使用自带 Chromium")
    taobao_mode = taobao.get("browser_mode", "playwright")
    if taobao_mode not in ("playwright", "native_cdp"):
        raise ValueError("taobao.browser_mode 仅支持 playwright 或 native_cdp")
    if taobao_mode == "native_cdp" and (taobao_channel != "chrome" or browser.get("headless", False)):
        raise ValueError("native_cdp 模式需要 browser_channel=chrome 且 headless=false")
    web = values.get("web", {})
    legacy_state = root / "state.json"
    legacy_profile = root / "storage/taobao_browser_profile"
    session = resolve(browser["session_path"]) if "session_path" in browser else (
        legacy_state if legacy_state.is_file() else workspace / "storage/sessions/qianniu.json"
    )
    profile = resolve(browser["profile_path"]) if "profile_path" in browser else (
        legacy_profile if legacy_profile.is_dir() else workspace / "storage/browser_profiles/qianniu"
    )
    timeout = browser.get("timeout_ms", 60000)
    port = web.get("port", 8000)
    headless = browser.get("headless", False)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("browser.timeout_ms 必须为正整数")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("web.port 必须为 1–65535 的整数")
    if type(headless) is not bool:
        raise ValueError("browser.headless 必须为布尔值")
    host = web.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("当前 Web 仅支持本机访问，请使用 127.0.0.1、localhost 或 ::1")
    timing = dict(DEFAULT_TAOBAO_TIMING)
    configured_timing = taobao.get("pacing", {})
    if not isinstance(configured_timing, dict) or set(configured_timing) - set(timing):
        raise ValueError("taobao.pacing 仅支持 before_search、search、scroll、page、keyword")
    for action, bounds in configured_timing.items():
        if not isinstance(bounds, list) or len(bounds) != 2 or any(
            type(number) not in {int, float} or not math.isfinite(number) or number < 0 for number in bounds
        ) or bounds[0] > bounds[1]:
            raise ValueError(f"taobao.pacing.{action} 必须为非负有限秒数 [最小值, 最大值]")
        timing[action] = tuple(bounds)
    settings = Settings(root, workspace, session, profile, values.get("shop_name", "我的店铺"), headless, timeout, host, port,
                        resolve(taobao["session_path"]) if "session_path" in taobao else None,
                        resolve(taobao["profile_path"]) if "profile_path" in taobao else None, timing, taobao_channel, taobao_mode)
    settings.for_platform("taobao")  # 配置冲突在启动前报错，避免混用会话。
    return settings


def prepare_workspace(settings: Settings) -> None:
    """建立运行目录；参数：Settings；返回：无。"""
    for relative in (
        "input/products", "input/images", "output/raw", "output/processed", "output/reports",
        "storage/sessions", "storage/browser_profiles", "storage/database", "logs/screenshots",
    ):
        (settings.workspace / relative).mkdir(parents=True, exist_ok=True)
