"""采集停止依据的存储契约：代码与诊断原文分开，兼容旧快照。"""
import re


def stop_reason_fields(reason, *, field='stop_reason'):
    """转换停止原因；参数：原代码或诊断、字段名；返回：代码及必要的原文说明。"""
    if reason is None or isinstance(reason,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',reason):
        return {field:reason}
    if not isinstance(reason,str):
        raise ValueError('停止原因必须为文本或空值')
    return {field:'diagnostic_pause',field+'_message':reason}
