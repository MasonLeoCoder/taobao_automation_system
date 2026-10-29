"""独立AI配置；只保存环境变量引用，不读取或持久化密钥值。"""
from dataclasses import dataclass
import re
import tomllib


@dataclass(frozen=True)
class AISettings:
    """调用配置；参数：服务商、默认模型、允许模型、密钥变量和超时。"""
    provider: str = 'deepseek'
    model: str = 'deepseek-flash'
    models: tuple = ('deepseek-flash', 'deepseek-v4-pro')
    api_key_env: str = 'DEEPSEEK_API_KEY'
    timeout_seconds: int = 20


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
        if config.provider != 'deepseek':
            raise ValueError()
        if not isinstance(config.models, (tuple, list)) or not config.models or len(config.models) > 20:
            raise ValueError()
        if any(not isinstance(m, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', m) for m in config.models):
            raise ValueError()
        if config.model not in config.models:
            raise ValueError()
        if not isinstance(config.api_key_env, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', config.api_key_env):
            raise ValueError()
        if type(config.timeout_seconds) is not int or not 1 <= config.timeout_seconds <= 30:
            raise ValueError()
        return config
    except (OSError, ValueError, TypeError):
        raise ValueError('AI配置无效，请检查config/ai.toml；支持deepseek，默认模型须在模型列表内，超时为1～30秒。配置内容未显示。') from None
