"""结构化查询的页码分页，不将全量记录载入内存。"""
import sqlalchemy as sa
from ecommerce.shared.pagination import PAGE_SIZE, pagination


def numbered_rows(connection, statement, page):
    """查询总量和当前页；参数：连接、有序查询、页码；返回：记录和分页信息。"""
    total = connection.scalar(sa.select(sa.func.count()).select_from(statement.order_by(None).subquery()))
    info = pagination(total, page)
    rows = [dict(row) for row in connection.execute(
        statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).mappings()]
    return rows, info
