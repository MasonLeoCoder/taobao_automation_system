# 独立评价提示词 V1.2 试验

2026-10-30，用户批准V1.2集中修改及离线通过后的同样例真实复测。本轮试验120548-17389404已完成，最多4次授权全部使用，共57831 Tokens。保留语义问题，未通过正式接入或用户人工验收，不自动新增版本或请求。

## 范围和文件

本目录独立于冻结的tests/prompt_trials（V1.1），正式Web/CLI不导入。本轮不修改src、MySQL、业务来源或旧任务。

- contracts.py：完整子结构及固定约束子集检查，不是通用JSON Schema实现；服务商使用已有JSON模式，不保证严格执行schema。
- processing.py：严格解析、精确原文、字段疑点、局部保留、事件范围去重及报告关联。V1.2统计只返statistic_ids，反证定位到finding_id+fact_id；存在关联不等于语义正确。
- runner.py：固定提示/结构/4份受测核心代码，35项离线门禁，只读来源及两库快照；最多4次、400000保守Token预留，意图先保存，未知结果也占名额，禁止重发、自动重试或提高额度。报告输入仅省略null/空集合，不删非空文字。
- test_offline.py：35项脱敏离线行为检查，使用模拟客户端及临时目录，不调用AI或写正式库。
- render.py：安全转义、本地静态HTML，事实/推断/程序统计分别展示；从独立quality-review.json读取助手复核标注，不改模型原文。
- qa.py：核对实际证据、V1.1冻结指纹、两库不变、密钥排除和4页×1440/390显示；模型结构/关联错误单列，不能将其当作通过。

## 本地核对

```cmd
python -m unittest tests.prompt_trials_v1_2.test_offline -v
python -m tests.prompt_trials_v1_2.render workspace/output/reports/review_prompt_v1_2/20261030/120548-17389404
python -m tests.prompt_trials_v1_2.qa workspace/output/reports/review_prompt_v1_2/20261030/120548-17389404
```

以上不请求AI。原门禁日志已经固定，不能用runner offline覆盖。call仅在新一轮有明确授权和新目录时使用，当前4个阶段重复调用均拒绝。

原提示、请求、响应、真实/合成输入、处理审计、HTML、截图和两库内容指纹只保存在Git忽略的workspace。quality-review.json包含conclusion、reviewer、checks和annotations；annotations以phase、location定位显示说明，detail是助手复核意见，不是用户已确认或自动语义判定。

提示及契约见[提取V1.2](../../docs/操作参考/评价提示词V1.2/提取.md)、[报告V1.2](../../docs/操作参考/评价提示词V1.2/报告.md)。实际结果及剩余问题见[复测记录](../../docs/测试记录/评价提示词V1.2复测记录.md)、[收尾提案](../../docs/测试记录/评价提示词V1.2剩余问题与收尾方案.md)。
