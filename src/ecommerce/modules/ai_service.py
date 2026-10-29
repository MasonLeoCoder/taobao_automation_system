"""Web与命令行共用的AI配置状态及固定文本连接检查。"""
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.ai.client import create_client


def configuration(settings):
    """读取无密钥的展示信息；参数：应用配置；返回：服务商与模型状态。"""
    config = load_config(settings.root)
    return {'provider': config.provider, 'model': config.model, 'models': list(config.models),
            'api_key_env': config.api_key_env, 'key_configured': bool(api_key(config)),
            'timeout_seconds': config.timeout_seconds}


def check_connection(settings, *, provider=None, model=None):
    """发送一次固定短文本，可能计费；参数：配置和选择；返回：安全摘要，不返回模型原文。"""
    config = load_config(settings.root)
    result = create_client(config, provider=provider, model=model).complete('请只回复：连接正常', max_tokens=32)
    return {'status': 'succeeded', 'message': '连接成功，已收到完整文字回复。',
            'provider': result['provider'], 'model': result['model'], 'usage': result['usage']}
