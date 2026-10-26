"""稳定的SQL排序表达式；空值始终排在最后。"""
from ecommerce.shared.sorting import sorting


def sorted_statement(statement, columns, identity, sort_by=None, order=None, default='position', default_order='asc'):
    """构造白名单排序；参数：查询、字段映射、稳定编号及选择；返回：有序查询和展示选择。"""
    spec = sorting(sort_by, order, columns, default, default_order)
    column = columns[spec['sort']]
    direction = spec['order']
    return statement.order_by(None).order_by(column.is_(None).asc(),
        column.desc() if direction == 'desc' else column.asc(),
        identity.desc() if direction == 'desc' else identity.asc()), spec
