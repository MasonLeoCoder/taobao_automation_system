"""加载配置并统一解析项目路径。"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


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
    return Settings(root, workspace, session, profile, values.get("shop_name", "我的店铺"), headless, timeout, host, port)


def prepare_workspace(settings: Settings) -> None:
    """建立运行目录；参数：Settings；返回：无。"""
    for relative in (
        "input/products", "input/images", "output/raw", "output/processed", "output/reports",
        "storage/sessions", "storage/browser_profiles", "storage/database", "logs/screenshots",
    ):
        (settings.workspace / relative).mkdir(parents=True, exist_ok=True)
