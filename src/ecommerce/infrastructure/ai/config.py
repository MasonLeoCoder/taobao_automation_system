"""独立AI配置；本机密钥不参与对象展示或公共状态返回。"""
from dataclasses import dataclass, field
import os
import re
import tomllib


class AIConfigError(ValueError):
    """可展示且不包含配置原文的配置错误。"""


@dataclass(frozen=True)
class AISettings:
    """调用配置；参数：服务商、默认模型、允许模型、密钥变量和超时。"""
    provider: str = 'deepseek'
    model: str = 'deepseek-flash'
    models: tuple = ('deepseek-flash', 'deepseek-v4-pro')
    api_key_env: str = 'DEEPSEEK_API_KEY'
    timeout_seconds: int = 20
    analysis_timeout_seconds: int = 180
    api_key: str = field(default='', repr=False)


def load_config(root):
    """加载独立config/ai.toml；参数：项目根路径；返回：校验配置，不触发网络。"""
    path = root / 'config/ai.toml'
    try:
        values = tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        if set(values) - {'ai'}:
            raise ValueError()
        data = values.get('ai', {})
        if not isinstance(data, dict) or set(data) - set(AISettings.__dataclass_fields__):
            raise ValueError()
        config = AISettings(**data)
        if not isinstance(config.api_key, str):
            raise ValueError()
        if config.provider != 'deepseek':
            raise ValueError()
        if not isinstance(config.models, (tuple, list)) or not config.models or len(config.models) > 20:
            raise ValueError()
        if any(not isinstance(m, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', m) for m in config.models):
            raise ValueError()
        if config.model not in config.models:
            raise ValueError()
        if not isinstance(config.api_key_env, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', config.api_key_env):
            raise AIConfigError('api_key_env只能填写环境变量名称，例如DEEPSEEK_API_KEY。密钥请填写在api_key的双引号内。')
        if type(config.timeout_seconds) is not int or not 1 <= config.timeout_seconds <= 30:
            raise ValueError()
        if type(config.analysis_timeout_seconds) is not int or not 30 <= config.analysis_timeout_seconds <= 600:
            raise ValueError()
        return config
    except AIConfigError:
        raise
    except (OSError, ValueError, TypeError):
        raise ValueError('AI配置无效，请检查config/ai.toml；支持deepseek，默认模型须在模型列表内，连接检查超时1～30秒，分析超时30～600秒。配置内容未显示。') from None


def api_key(config):
    """读取密钥；参数：AI配置；返回：优先本机配置，空白时回退环境变量，禁止日志输出。"""
    return config.api_key.strip() or os.environ.get(config.api_key_env, '').strip()
