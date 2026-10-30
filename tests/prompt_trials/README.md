# 独立评价提示词试验工具

适用：V1.1提取与竞品报告的离线和明确授权的真实小样例验证，不被正式Web/CLI导入，不修改MySQL。用户于2026-10-30批准修改、离线通过后真实复测；本轮最多4次已使用完，后续真实调用须明确新范围。

## 文件职责

- contracts.py：完整子字段、类型、枚举及可空定义，导出两份.schema.json。errors仅实现本试验使用的固定约束子集，不是完整通用JSON Schema库，不声称服务商执行严格schema模式。
- processing.py：精确引文与关联检查、字段级疑点、局部隔离、程序状态、分事件统计及引用并集。结构/引用通过不保证语义正确。
- runner.py：固定提示/结构/代码快照、25项离线门禁、最多4次意图及400000保守预留；只读前后数据库，响应先保存再解析，无自动重试。
- test_offline.py：脱敏、临时目录及模拟客户端，不联网、不读取业务原文、不写正式库。
- render.py：转义模型文本的本地HTML展示，事实/推断/程序数字分栏。
- qa.py：实际证据、旧版未改、数据库哈希、密钥排除及4页×桌面手机检查；不发起AI请求。

## 离线命令

在项目CMD并启用项目虚拟环境后执行：

```cmd
python -m unittest tests.prompt_trials.test_offline -v
```

该命令仅检查离线行为，不会调用DeepSeek。固定样例的预期行为不能替代真实模型效果验证。

本机已有报告可重新渲染和检查：

```cmd
python -m tests.prompt_trials.render workspace/output/reports/review_prompt_v1_1/20261030/112535-84593a0e
python -m tests.prompt_trials.qa workspace/output/reports/review_prompt_v1_1/20261030/112535-84593a0e
```

新一轮需新证据目录和明确授权。runner的prepare只读取原来源并固定新目录；offline运行回归生成门禁，历史门禁和请求已归档后不原地重跑覆盖；call必须指定阶段及--allow-paid才可能联网。已预约阶段永远拒绝重发，未知结果也占名额。finish核对两库前后状态，不写分析任务或报告。

样例数据、请求、响应和HTML都位于Git忽略的workspace，代码和模板不含真实评价数据或凭据。调用记录中的服务商response_id不是数据库analysis_calls.id。

当前两套提示词和本地文件字段说明见[提取V1.1](../../docs/操作参考/评价提示词V1.1/提取.md)、[报告V1.1](../../docs/操作参考/评价提示词V1.1/报告.md)，实际结果见[复测记录](../../docs/测试记录/评价提示词V1.1复测记录.md)。V1.2仅是待确认提案。
