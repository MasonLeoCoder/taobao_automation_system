"""从淘宝首页的账号菜单读取完整昵称。"""
import re

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .login import NICK_SELECTORS, is_authenticated
from .verification import checkpoint


_ACCOUNT_MENU_NICK = r"""({prefix, shortened, trigger}) => {
    const visible = el => !!el.getClientRects().length &&
        getComputedStyle(el).visibility === 'visible' && !!el.closest('body');
    const directText = el => Array.from(el.childNodes)
        .filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
    const label = el => (el.innerText || '').trim();
    const exact = nodes => nodes.filter(el => !nodes.some(other => other !== el && el.contains(other)));
    const actions = Array.from(document.querySelectorAll('a, button, [role="button"], [role="menuitem"], span'))
        .filter(visible);
    const logout = exact(actions.filter(el => /^退出(?:登录)?$/.test(label(el))));
    const manage = exact(actions.filter(el => label(el) === '账号管理'));
    if (logout.length !== 1 || manage.length !== 1) return null;
    for (let panel = logout[0].parentElement; panel && panel !== document.body; panel = panel.parentElement) {
        if (panel.contains(trigger)) return null;
        if (!panel.contains(manage[0])) continue;
        const values = [...new Set(Array.from(panel.querySelectorAll('div, a, span, b, strong, p, h2, h3, h4'))
            .filter(visible).map(directText).filter(value => value && !/(?:\.{3}|…)$/.test(value) &&
                (shortened ? value.startsWith(prefix) && value.length > prefix.length : value === prefix)))];
        if (values.length === 1) return values[0];
        if (values.length > 1) return null;
        // 账号管理与退出可能在同一行，昵称位于外一层的用户信息卡。
        if (panel.querySelectorAll('a, button').length > 10) return null;
    }
    return null;
}"""


def read_buyer_nickname(page):
    """展开登录入口并回读账号卡片，读不到唯一完整昵称时返回空值。"""
    if not is_authenticated(page):
        return None
    candidates = [(item, item.inner_text(timeout=1000).strip())
                  for item in page.locator(', '.join(NICK_SELECTORS)).all() if item.is_visible()]
    values = {value for _, value in candidates if value}
    if len(values) != 1:
        return None
    visible_nick = values.pop()
    prefix = re.sub(r'(?:\.{3}|…)+$', '', visible_nick).strip()
    if not prefix:
        return None
    try:
        checkpoint('打开淘宝账号菜单')
        trigger = next(item for item, value in candidates if value == visible_nick)
        handle = trigger.element_handle(timeout=1000)
        trigger.hover(timeout=3000)
        result = page.wait_for_function(_ACCOUNT_MENU_NICK,
            arg={'prefix': prefix, 'shortened': prefix != visible_nick, 'trigger': handle}, timeout=4000).json_value()
        checkpoint('核对淘宝完整昵称')
    except PlaywrightTimeoutError:
        return None
    return result if isinstance(result, str) and 0 < len(result) <= 255 else None
