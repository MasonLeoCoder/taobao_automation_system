"""从千牛店铺信息页读取当前店铺与登录者的显示信息。"""

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .verification import checkpoint, qianniu_operation


SHOP_INFORMATION_URL = "https://myseller.taobao.com/home.htm/shop-manage/shop-center"
IDENTITY_TIMEOUT_MS = 15_000

_READ_IDENTITY = r"""() => {
    const visible = element => element.getClientRects().length > 0 &&
        getComputedStyle(element).visibility === 'visible';
    const matching = selector => [...document.querySelectorAll(selector)].filter(visible);
    const labels = matching('[class*="label--"]');
    const shopIdLabels = labels.filter(label =>
        /^店铺\s*ID\s*[：:]?$/.test(label.innerText.trim()));
    const accounts = matching('[class*="accountWrap--"]');
    if (shopIdLabels.length !== 1 || accounts.length !== 1) return null;

    const account = accounts[0];
    const shopName = account.querySelector(':scope > [class*="shopName--"]');
    const loginName = account.querySelector(':scope > [class*="nameWrap--"]');
    if (!shopName || !loginName || !visible(shopName) || !visible(loginName)) return null;

    const shopId = shopIdLabels[0].parentElement.innerText
        .match(/^\s*店铺\s*ID\s*[：:]?\s*([0-9]+)\s*$/)?.[1];
    const shopNameText = shopName.innerText.split('\n')[0].trim();
    const loginParts = loginName.innerText.trim().split(/\s+/);
    if (!shopId || !shopNameText || !loginParts[0] || loginParts.length > 2) return null;
    const ownerLabels = labels.filter(label =>
        /^淘宝账号名\s*[：:]?$/.test(label.innerText.trim()));
    const shopLoginName = ownerLabels.length === 1
        ? ownerLabels[0].parentElement.innerText.match(/^\s*淘宝账号名\s*[：:]?\s*([^\s:：]+)\s*$/)?.[1]
        : null;
    const nickname = loginParts[0];
    let fullLoginName = null;
    if (shopLoginName && loginParts.length === 1 && nickname === shopLoginName) {
        fullLoginName = shopLoginName;
    } else if (shopLoginName && loginParts.length === 2 && nickname !== shopLoginName && !/[:：]/.test(nickname)) {
        fullLoginName = shopLoginName + ':' + nickname;
    }
    return {shopId, shopName: shopNameText, loginDisplayName: loginParts.join('-'), fullLoginName};
}"""


@dataclass(frozen=True)
class ShopIdentity:
    """店铺信息页核对到的店铺和登录者，完整登录名可用于后续比对。"""

    shop_id: str
    shop_name: str
    login_display_name: str
    login_name: str | None = None


def read_visible_identity(page: Page) -> ShopIdentity | None:
    """只读当前页面的明确身份信息，保留商品编辑现场。"""
    location = urlsplit(page.url)
    if page.is_closed() or location.scheme != 'https' or location.hostname not in {
        'myseller.taobao.com', 'upload.taobao.com', 'item.upload.taobao.com',
    }:
        return None
    data = page.evaluate(_READ_IDENTITY)
    return ShopIdentity(data['shopId'], data['shopName'], data['loginDisplayName'],
                        data['fullLoginName']) if data else None


def read_editor_account(page: Page) -> tuple[str, str] | None:
    """只读商品编辑页顶栏的店铺名及登录者名称。"""
    if page.is_closed():
        return None
    location = urlsplit(page.url)
    if (location.scheme, location.netloc, location.path) != (
        'https', 'item.upload.taobao.com', '/sell/v2/publish.htm',
    ):
        return None
    account = page.locator('[class*="UserArea_accountWrap__"]:visible')
    if account.count() != 1:
        return None
    shop = account.locator(':scope > [class*="UserArea_shopName__"]:visible')
    member = account.locator(':scope > [class*="UserArea_nameWrap__"] > [class*="UserArea_userName__"]:visible')
    if shop.count() != 1 or member.count() != 1:
        return None
    name, nickname = shop.inner_text(timeout=1000).strip(), member.inner_text(timeout=1000).strip()
    return (name, nickname) if name and nickname else None


@qianniu_operation
def read_shop_identity(page: Page, timeout_ms: int = IDENTITY_TIMEOUT_MS) -> ShopIdentity | None:
    """只读核对千牛店铺和登录者；尚未取得完整页面信息时返回空值。"""
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        response = page.goto(SHOP_INFORMATION_URL, wait_until="commit", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        checkpoint("打开千牛店铺信息页")
        return None
    if response is not None and response.status >= 400:
        return None

    target = urlsplit(SHOP_INFORMATION_URL)
    while time.monotonic() < deadline and not page.is_closed():
        before_check = time.monotonic()
        checkpoint("读取千牛店铺信息")
        deadline += time.monotonic() - before_check
        current = urlsplit(page.url)
        if current.scheme != target.scheme or current.netloc != target.netloc:
            return None
        if current.path.rstrip("/") == target.path:
            identity = read_visible_identity(page)
            if identity is not None:
                return identity
        page.wait_for_timeout(250)
    return None
