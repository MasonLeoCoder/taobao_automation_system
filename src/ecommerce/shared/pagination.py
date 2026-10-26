"""网页共用的固定页大小与页码边界。"""
PAGE_SIZE = 20


def pagination(total, page=1):
    """计算页码范围；参数：总条数、页码；返回：分页信息，越界时拒绝。"""
    if type(page) is not int or page < 1:
        raise ValueError('页码须为正整数')
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise LookupError('页码超出范围')
    return {'total': total, 'page': page, 'pages': pages, 'page_size': PAGE_SIZE,
            'has_previous': page > 1, 'has_more': page < pages,
            'first_record': (page - 1) * PAGE_SIZE + 1 if total else 0,
            'last_record': min(page * PAGE_SIZE, total)}
