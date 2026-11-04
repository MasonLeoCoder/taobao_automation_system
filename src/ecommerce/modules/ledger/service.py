"""经营账本输入规则与公开业务入口。"""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from ecommerce.infrastructure.storage import ledger as repository


def amount(value, *, allow_zero=False):
    """校验人民币金额；参数：输入与是否允许零；返回：精确两位小数。"""
    text = str(value).strip()
    if not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', text):
        raise ValueError('金额须为正数，最多两位小数，上限9999999999.99')
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError('金额格式不正确') from None
    if number < 0 or (not allow_zero and number == 0):
        raise ValueError('记账金额必须大于0')
    return number.quantize(Decimal('.01'))


def month(value):
    """解析统计月份；参数：YYYY-MM；返回：月初日期和下月月初。"""
    if not re.fullmatch(r'\d{4}-\d{2}', str(value)):
        raise ValueError('请选择有效月份')
    try:
        start = datetime.strptime(value, '%Y-%m').date()
        if not 1000 <= start.year <= 9998:
            raise ValueError()
        end = start.replace(year=start.year+1, month=1) if start.month == 12 else start.replace(month=start.month+1)
    except ValueError:
        raise ValueError('月份须在1000年至9998年之间') from None
    return start, end


def integer(value, minimum=0):
    """校验整数；参数：输入、最小值；返回：数据库安全整数。"""
    if not re.fullmatch(r'\d{1,10}', str(value)) or not minimum <= int(value) <= 2147483647:
        raise ValueError('编号、排序或版本不正确')
    return int(value)


def save_entry(settings, form):
    """验证并保存账目；参数：配置、表单；返回：无。"""
    identifier = str(form.get('id', ''))
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('账目编号不正确，请刷新页面')
    direction = form.get('direction')
    if direction not in {'expense', 'income'}:
        raise ValueError('请选择收入或支出')
    try:
        text = str(form.get('occurred_at', ''))
        occurred = datetime.fromisoformat(text)
        if occurred.tzinfo is not None or not 1000 <= occurred.year <= 9998:
            raise ValueError()
    except ValueError:
        raise ValueError('请填写有效的北京时间，年份在1000至9998之间') from None
    note = str(form.get('note', '')).strip()
    if len(note) > 500:
        raise ValueError('备注最多500字')
    values = dict(occurred_at=occurred, direction=direction,
                  category_id=integer(form.get('category_id', ''), 1), amount=amount(form.get('amount', '')), note=note)
    repository.save_entry(settings, identifier, integer(form.get('revision', '0')), values)


def save_category(settings, form):
    """保存可维护分类；参数：配置、表单；返回：无。"""
    name = str(form.get('name', '')).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError('分类名称须为1至80字')
    repository.save_category(settings, integer(form.get('id', '0')),
        dict(name=name, sort_order=integer(form.get('sort_order', '0')), is_enabled=form.get('is_enabled') == '1'))


def save_limit(settings, form):
    """保存独立月度止损线；参数：配置、表单；返回：无，不继承或拦截支出。"""
    start, _ = month(form.get('month', ''))
    repository.save_limit(settings, start, amount(form.get('amount', ''), allow_zero=True))


def overview(settings, selected_month, category='', direction='', status='active', page='1'):
    """查询月度概览与分页明细；参数：配置、月份及筛选；返回：页面数据。"""
    start, end = month(selected_month)
    if direction not in {'', 'income', 'expense'} or status not in {'active', 'void', ''}:
        raise ValueError('筛选条件不正确')
    data = repository.overview(settings, start, end, integer(category, 1) if category else None,
        direction, status, integer(page, 1))
    data['shares'] = category_shares(data['groups'], data['totals'])
    return data


def category_shares(groups, totals):
    """分别计算收入和支出的分类占比；参数：整月分类及方向总额；返回：按金额降序的两组占比。"""
    result = {direction: [] for direction in ('expense', 'income')}
    for group in groups:
        direction = group['direction']
        total = totals[direction]
        percent = (group['amount'] * Decimal(100) / total).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if total else Decimal('0.00')
        result[direction].append({**group, 'percent': percent})
    for rows in result.values():
        rows.sort(key=lambda row: (-row['amount'], row['name']))
    return result
