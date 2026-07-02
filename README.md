# Taobao Automation System

基于 Python + Playwright 的电商自动化项目。

## 项目结构

- `main.py`：主控制层，只负责任务调度和流程编排。
- `automation/`：业务流程层，存放登录、导航、下载、上传等完整业务流程。
- `browser/`：浏览器执行层，只封装 Playwright 的底层操作。
- `data/`：数据层，负责 CSV、Excel、Pandas 数据处理和输出路径管理。
- `ai/`：AI 生成层，只放内容生成相关能力。
- `config/`：项目共享配置。
- `utils/`：日志、重试、随机延迟等通用工具。
- `logs/`：运行日志目录。
- `output/`：生成结果输出目录。
- `storage/`：浏览器登录状态文件目录。

## 运行方式

```bash
python main.py
```

当前流程会优先检查可复用的 Playwright 登录状态文件：

```text
storage/storage_state.json
```

如果该文件不存在或格式无效，流程会安全停止，并把原因记录到 `logs/run.log`。
