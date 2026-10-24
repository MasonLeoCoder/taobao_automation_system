"""第三版来源解释契约：迁移与实时写入共用，后续版本不得改写历史语义。"""
import json
from urllib.parse import parse_qs, urlsplit


ORIGIN_COLUMNS = {
    'collection_batches': ('search_method', 'search_keyword', 'page_number', 'search_image_file_id', 'search_options'),
    'search_records': ('search_method', 'search_image_file_id'),
}


def origin_values(table_name, module, record, source_url=None):
    """解释有依据的搜索来源；参数：目标表、模块、原记录及补充链接；返回：新增列的值。"""
    if not isinstance(record, dict):
        record = {}
    searchable = (table_name == 'search_records' and module == 'search') or (
        table_name == 'collection_batches' and module == 'list')
    declared = 'search_method' in record
    method = record.get('search_method')
    if method not in (None, 'keyword', 'image', 'unknown'):
        raise ValueError('搜索方式不属于受支持的代码')
    keyword = record.get('keyword') if searchable or declared else None
    if keyword is not None and (not isinstance(keyword, str) or len(keyword) > 100):
        raise ValueError('来源搜索词必须是100字以内的文本')
    if isinstance(keyword, str) and not keyword.strip():
        keyword = None
    image = record.get('search_image')
    if image is not None and not isinstance(image, dict):
        raise ValueError('搜索图片必须保留文件属性对象')
    if image and declared and method != 'image':
        raise ValueError('搜索输入图片与搜索方式不一致')
    if not declared and searchable:
        if image:
            method = 'image'
        elif keyword:
            # 旧关键词流程仅在明确搜索/列表模块内解释，不从商品标题反推。
            method = 'keyword'
        else:
            method = 'unknown'
    if method == 'keyword' and not keyword:
        raise ValueError('关键词搜索缺少实际来源搜索词')
    if method == 'keyword':
        url = urlsplit(source_url or record.get('source_url') or '')
        query = parse_qs(url.query).get('q', [])
        if url.hostname == 's.taobao.com' and query and any(value != keyword for value in query):
            raise ValueError('来源链接与已保存搜索词冲突')
    page = record.get('page_number') if searchable or declared else None
    if page is not None and (type(page) is not int or page < 1):
        raise ValueError('来源页码必须为大于等于1的整数')
    options = record.get('search_options') if searchable or declared else None
    if options is not None:
        if not isinstance(options, dict):
            raise ValueError('附加搜索条件必须为对象，未知时为空')
        json.dumps(options, ensure_ascii=False, allow_nan=False)
    values = {'search_method': method, 'search_image_file_id': None}
    if table_name == 'collection_batches':
        values.update(search_keyword=keyword, page_number=page, search_options=options)
    return values


def source_record(table_name, values):
    """取得原始业务记录；参数：目标表和列值；返回：原记录对象。"""
    payload = values.get('result_payload' if table_name == 'collection_batches' else 'payload') or {}
    record = payload.get('record') or {}
    if not isinstance(record, dict):
        raise ValueError('来源记录不是对象，不能可靠解释搜索来源')
    return record


def enrich_origin(table_name, values, old=None):
    """填充并核对新增来源列；参数：目标表、待写列、原行；返回：兼容旧快照的列值。"""
    record = source_record(table_name, values)
    module = values.get('module', values.get('record_type'))
    derived = origin_values(table_name, module, record, values.get('source_url'))
    explicit = 'search_method' in record
    result = dict(values)
    for key, value in derived.items():
        if key in values:
            # 文件编号由当前库解析，原件不携带跨库内部主键。
            if key != 'search_image_file_id' and values[key] != value:
                raise ValueError('独立来源列与原记录冲突：' + key)
            value = values[key]
        previous = old[key] if old is not None and key in old else None
        if key == 'search_method' and value == 'unknown' and previous is not None and not explicit:
            value = previous
        if previous is not None and value is None and not explicit:
            value = previous
        if previous is not None and (value is not None or explicit) and previous != value:
            raise ValueError('同一来源记录的搜索条件冲突：' + key)
        result[key] = value
    return result
