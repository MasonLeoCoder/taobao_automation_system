"""逐条校验及有界本地规范化；不调用AI，不改写原始返回或评价。"""
import copy
import json
import re
from collections import Counter
from ecommerce.modules.review_analysis import rules

# 改变规范化或语义判断规则时递增版本，避免新旧审计混用。
POLICY = 'reviews-validation-v1'
SUPPLEMENT = '''\n本次是补处理，只处理下方reviews中列出的编号，不补回其他评价。
specific表示有可归类信息，包括品质笼统认可、已经回购、未来回购意愿；不等于性能已验证。
general仅用于没有任何可归类信息的“满意、好评”等，points必须为空；empty只用于初评及追评全部空白，points必须为空。
引文必须逐字复制原文，保留空格、标点、否定及条件；每个观点引用一段连续原文，不能删掉中间句子再拼接。
长度或大小合适不能直接证明替换芯兼容；没有明确安装、更换或适配反馈时，不生成此类主题。
商品背景和规格不是评价证据。只输出规定JSON，覆盖下方全部id各一次。\n补处理资料：\n'''


def whitespace_span(quote, original):
    """定位只差空白且唯一的连续原文；参数：引文与对应片段；返回：原文或None。"""
    compact, positions = [], []
    for index, char in enumerate(original):
        if not char.isspace():
            compact.append(char)
            positions.append(index)
    wanted = ''.join(char for char in quote if not char.isspace())
    if not wanted:
        return None
    text = ''.join(compact)
    index = text.find(wanted)
    if index < 0 or text.find(wanted, index + 1) >= 0:
        return None
    return original[positions[index]:positions[index+len(wanted)-1]+1]


def general_can_normalize(points):
    """只确认三类明确主题的类型冲突；参数：已通过原文校验的观点；返回：是否可规范化。"""
    if not points:
        return False
    for point in points:
        topic, quote = point['topic'], point['quote']
        if topic == '品质笼统认可' and any(word in quote for word in ('质量', '品质', '质感')):
            continue
        if topic == '回购经历' and re.search(r'一直.*[回复]购|[回复]购了|多次.*[买购]|买过|第[二三四五]次', quote):
            continue
        if topic == '回购意愿' and re.search(r'会.*[回复]购|再.*[回复]购|下次.*[买购]|还.*[买购]', quote):
            continue
        return False
    return True


def semantic_issue(point, source):
    """识别明确需要复核的主题推断；参数：原文已核验观点；返回：说明或空字符串。"""
    if point['sentiment']=='positive':
        original = rules.segments(source).get(point['segment'],'')
        positions = [match.start() for match in re.finditer(re.escape(point['quote']),original)]
        if positions and all(re.search(r'(?:不是|并不|并不是|不太|不|没有|没)$',original[max(0,index-5):index]) for index in positions):
            return '引文省略了紧邻的否定词，不能据此判为正面；请保留完整上下文复核。'
    if point['topic'] == '安装、更换与适配' and any(word in point['quote'] for word in ('长度', '大小', '厚度')):
        if not any(word in point['quote'] for word in ('安装', '更换', '替换', '换纸', '换芯', '适配', '匹配', '兼容', '通用', '接口', '装上', '装不', '装得')):
            return '引文只说明尺寸，不能直接支持安装、更换或兼容判断；需要复核主题。'
    return ''


def assess(text, sources, *, finish_reason='stop'):
    """独立检查所有评价并收集问题；参数：安全返回文字、来源及完成原因；返回：结果与审计。"""
    outcome = {'policy': POLICY, 'results': [], 'issues': [], 'adjustments': [], 'source_ids': [source['id'] for source in sources]}
    def issue(uid, code, field, message, **extra):
        """收集可展示问题；参数：编号、代码、字段及说明；返回：无。"""
        outcome['issues'].append({'review_id': uid, 'code': code, 'field': field, 'message': message, **extra})
    if finish_reason != 'stop':
        issue(None, 'incomplete_response', 'response', 'AI返回未完整结束，保留原始返回；未自动重试。')
        return outcome
    try:
        values = rules.decode(text).get('reviews')
        if not isinstance(values, list):
            raise ValueError('返回缺少reviews评价数组。')
    except ValueError as error:
        issue(None, 'invalid_response', 'response', str(error))
        return outcome
    counts = Counter(item['id'] for item in values if isinstance(item, dict) and type(item.get('id')) is int)
    lookup = {source['id']: source for source in sources}
    for item in values:
        if not isinstance(item, dict) or type(item.get('id')) is not int:
            issue(None, 'invalid_id', 'id', '返回含无效编号，不能归入其他评价。')
        elif item['id'] not in lookup:
            issue(None, 'unexpected_id', 'id', '返回包含不属于本次发送的评价编号。', returned_id=item['id'])
    by_id = {item['id']: item for item in values if isinstance(item, dict) and type(item.get('id')) is int}
    for source in sources:
        uid = source['id']
        if not counts[uid]:
            issue(uid, 'missing_review', 'id', 'AI未返回这条评价。')
            continue
        if counts[uid] != 1:
            issue(uid, 'duplicate_review', 'id', '同一编号重复返回，未擅自选择其中一条。')
            continue
        candidate = copy.deepcopy(by_id[uid])
        start_issues, changes = len(outcome['issues']), []
        if not isinstance(candidate.get('points'), list) or len(candidate['points']) > 100:
            issue(uid, 'invalid_points', 'points', '主题列表无效或超过每条100个观点限制。')
            continue
        segments = rules.segments(source)
        for index, point in enumerate(candidate['points']):
            field = f'points[{index}]'
            if isinstance(point, dict) and isinstance(point.get('quote'), str) and isinstance(point.get('segment'), str):
                original = segments.get(point['segment'], '')
                if point['quote'] not in original:
                    exact = whitespace_span(point['quote'], original)
                    if exact is not None:
                        changes.append({'review_id':uid,'field':field+'.quote','before':point['quote'],'after':exact,
                                        'reason':'仅补回原文空白；非空白字符完全一致且对应位置唯一。'})
                        point['quote'] = exact
            try:
                checked = rules.validate_point(point, source)
            except ValueError as error:
                issue(uid, 'invalid_point', field, str(error), returned_point=copy.deepcopy(point))
                continue
            warning = semantic_issue(checked,source)
            if warning:
                issue(uid, 'topic_needs_review', field+'.topic', warning, returned_point=checked)
        if len(outcome['issues']) != start_issues:
            continue
        if candidate.get('kind') == 'general' and general_can_normalize(candidate['points']):
            changes.append({'review_id':uid,'field':'kind','before':'general','after':'specific',
                            'reason':'存在已核对原文的明确品质认可或回购信息；仅规范为有可归类信息，不推断性能。'})
            candidate['kind'] = 'specific'
        try:
            result = rules.classification(json.dumps({'reviews':[candidate]}, ensure_ascii=False), [source])[0]
        except ValueError as error:
            issue(uid, 'invalid_classification', 'kind', str(error))
            continue
        outcome['results'].append(result)
        outcome['adjustments'].extend(changes)
    return outcome


def pending_issues(parameters, completed_ids):
    """读取尚未解决的最新问题；参数：任务参数、已保存编号；返回：按编号排列的问题列表。"""
    latest = {}
    for record in parameters.get('validation_history', []):
        for uid in record['source_ids']:
            latest.pop(uid, None)
        for item in record['issues']:
            uid = item['review_id']
            if uid is None and all(source_id in completed_ids for source_id in record['source_ids']):
                continue
            if uid not in completed_ids:
                latest.setdefault(uid, []).append({**item, 'call_id':record['call_id']})
    return [item for uid in sorted(latest, key=lambda value: -1 if value is None else value) for item in latest[uid]]
