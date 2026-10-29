# 可选 AI 服务接入

已接入DeepSeek官方文本接口。`config.py`读取独立AI配置，`client.py`定义统一文本契约及适配，业务入口为`modules.ai_service`，Web与CLI共用。默认deepseek-flash，可选deepseek-v4-pro；以官方当前模型文档为准。

密钥优先从被Git忽略的config/ai.toml中api_key读取，空白时兼容环境变量；密钥不参与对象展示，不保存到日志、页面或任务；官方地址固定HTTPS，拒绝重定向，无自动重试。仅实现配置状态与固定短文本连接测试，尚未实现商品分析、图片输入、结果持久化或其他服务商。Web检查在有界后台线程执行，状态仅保留当前进程。
