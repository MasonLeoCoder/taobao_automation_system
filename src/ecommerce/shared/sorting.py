"""列表排序参数的白名单与默认方向。"""


def sorting(sort_by, order, allowed, default, default_order='asc'):
    """校验排序选择；参数：字段、方向、白名单及默认值；返回：规范化选择。"""
    field = sort_by or default
    direction = order or default_order
    if field not in allowed or direction not in {'asc', 'desc'}:
        raise ValueError('排序字段或方向无效')
    return {'sort': field, 'order': direction}
