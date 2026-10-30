"""模型容量、本地Token估算及保守费用上界；不联网、不创建AI客户端。"""
from functools import lru_cache
import hashlib
import math
from pathlib import Path
from importlib.metadata import version

SOURCE='https://api-docs.deepseek.com/quick_start/pricing/'
TOKENIZER_SOURCE='https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'
TOKENIZER_HASH='89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614'
MODELS={
 'deepseek-flash':{'context_tokens':1000000,'max_output_tokens':393216,'family':'deepseek-v4',
     'input_usd_per_million':0.3,'output_usd_per_million':1.2},
 'deepseek-v4-pro':{'context_tokens':1000000,'max_output_tokens':393216,'family':'deepseek-v4',
     'input_usd_per_million':1.32,'output_usd_per_million':3.96},
}


def capability(config,model):
    """读取已核对模型资料及显式覆盖；参数：配置、模型；返回：可冻结能力快照。"""
    if model not in config.models:raise ValueError('模型不在本机允许列表中。')
    value={**MODELS.get(model,{}),**config.capabilities.get(model,{})}
    if not all(type(value.get(k)) is int and value[k]>0 for k in ('context_tokens','max_output_tokens')):
        raise ValueError('该模型尚无可靠容量配置，请先补充模型能力，未套用其他模型的限制。')
    value.update(provider=config.provider,model=model,verified_on=value.get('verified_on','2026-10-30'),
        source=value.get('source',SOURCE),mode='non-thinking',capacity_note='官方1M按保守十进制1000000规划；输出与输入共同占用上下文。')
    return value


@lru_cache(maxsize=4)
def tokenizer(path,stamp):
    """只加载已核对的数据文件，不执行下载代码；参数：路径、修改时间；返回：本地分词器或None。"""
    p=Path(path)
    if hashlib.sha256(p.read_bytes()).hexdigest()!=TOKENIZER_HASH:return None
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(p))


class Counter:
    """整次消息的本地估算器；计数始终标为预计，未知模型使用UTF8字节保守量。"""
    def __init__(self,root,config,profile):
        """选择本地工具，不下载资料；参数：根目录、配置、能力；返回：实例。"""
        self.tool=None;path=Path(config.tokenizer_file)
        path=path if path.is_absolute() else root/path
        try:
            if profile.get('family')=='deepseek-v4' and path.exists():self.tool=tokenizer(str(path.resolve()),path.stat().st_mtime_ns)
        except (OSError,ValueError,ImportError):self.tool=None
        self.identity={'method':'official-v4-local-estimate' if self.tool else 'utf8-conservative-estimate',
            'tokenizer_sha256':TOKENIZER_HASH if self.tool else None,'tool_version':version('tokenizers') if self.tool else None,
            'source':TOKENIZER_SOURCE if self.tool else None,'exact':False}

    def count(self,messages):
        """包含消息正文和边界开销；参数：system/user消息；返回：预计Token。"""
        if self.tool:return sum(len(self.tool.encode(m['content']).ids)+64 for m in messages)+64
        return sum(len(m['content'].encode('utf-8'))+64 for m in messages)+64

    def measure(self,messages,output):
        """分别计算模型容量和计费预留；参数：消息、输出上限；返回：估算快照。"""
        count=self.count(messages);margin=max(2048,math.ceil(count*(0.10 if self.tool else 0.20)))
        return {**self.identity,'input_tokens':count,'output_tokens':output,'safety_tokens':margin,'reserved_tokens':count+output+margin}


def cost_ceiling(profile,input_tokens,output_tokens):
    """按未命中缓存及高峰价估计USD上界；参数：能力、预留输入输出；返回：估计或None。"""
    if not all(isinstance(profile.get(k),(int,float)) for k in ('input_usd_per_million','output_usd_per_million')):return None
    return {'currency':'USD','amount':round((input_tokens*profile['input_usd_per_million']+output_tokens*profile['output_usd_per_million'])/1000000,6),
            'basis':'已核对高峰未命中缓存单价的用量上界估算，不是预扣款或账单；不做汇率换算。','price_verified_on':profile['verified_on']}
