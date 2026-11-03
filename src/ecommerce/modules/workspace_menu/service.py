"""菜单配置校验和分组状态继承；不依赖Web或执行任务。"""
from ecommerce.infrastructure.storage import web_menus


class MenuConfigurationError(RuntimeError):
    """菜单配置不完整或与程序不匹配，不能回退默认配置放行。"""


def snapshot(mysql_settings, defaults, *, database=True):
    """读取并校验完整快照；参数：连接、程序目录默认行、是否使用MySQL；返回：配置字典。"""
    source = web_menus.read(mysql_settings) if database else defaults
    expected_groups, expected_items = defaults
    groups = {row['group_key']: dict(row) for row in source[0]}
    items = {row['menu_key']: dict(row) for row in source[1]}
    known_groups = {row['group_key'] for row in expected_groups}
    known_items = {row['menu_key']: row['construction_status'] for row in expected_items}
    if set(groups) != known_groups or set(items) != set(known_items):
        raise MenuConfigurationError('菜单配置不完整或含未登记标识，请核对数据库版本和配置；未启用代码默认值')
    for row in [*groups.values(), *items.values()]:
        if (not isinstance(row['name'], str) or not row['name'].strip() or len(row['name']) > 80
                or type(row['sort_order']) is not int or row['sort_order'] < 0
                or row['is_enabled'] not in (True, False) or row['is_visible'] not in (True, False)):
            raise MenuConfigurationError('菜单名称、顺序或状态无效，请核对配置')
    for key, row in items.items():
        if row['group_key'] not in groups or row['construction_status'] != known_items[key]:
            raise MenuConfigurationError('菜单归属或建设状态与当前程序不一致，请核对配置')
    return {'groups': groups, 'items': items}


def enabled(data, key):
    """判断功能有效启用状态；参数：快照、功能标识；返回：布尔值，未知标识不放行。"""
    item = data['items'].get(key)
    return bool(item and item['is_enabled'] and data['groups'][item['group_key']]['is_enabled'])


def visible(data, key):
    """判断侧栏是否展示；参数：快照、功能标识；返回：同时满足启用与两级显示状态。"""
    item = data['items'].get(key)
    return bool(enabled(data, key) and item['is_visible'] and data['groups'][item['group_key']]['is_visible'])
