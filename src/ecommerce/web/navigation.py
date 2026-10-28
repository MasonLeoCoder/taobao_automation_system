"""站内返回地址与采集页面上下文，拒绝外部跳转地址。"""
from urllib.parse import urlsplit, unquote
from flask import request


def safe_return(value,default='/collections'):
    """校验返回链接；参数：来源地址、默认地址；返回：仅站内采集或任务页面地址。"""
    if not value or len(value)>8192: return default
    decoded=unquote(value)
    if any(char in decoded for char in ('\\','\r','\n')): return default
    parsed=urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith(('/collections','/tasks','/logs','/analysis/searches')):
        return default
    return value


def current_location():
    """读取当前站内地址；参数：无；返回：带筛选排序页码的地址。"""
    return request.full_path.rstrip('?')
