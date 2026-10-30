# 已有评价报告离线收尾

2026-10-30用户批准：汇总既有语义问题及统计展示、重复证据、解决动作、淘汰范围、询价顺序和首页优化，一次收尾；不新增AI调用，不改正式Web/MySQL。本工具独立于冻结的tests/prompt_trials_v1_2，不被正式业务导入。

本轮源试验120548-17389404，收尾125412-b2d26f89。最终可读修订0004，新增AI调用0/Token0，20项脱敏离线和8项桌面/手机检查通过，10项助手建议/原反证全部待用户复核。程序检查不等于语义或实物验收。

## 职责与限制

- core.py：严格来源身份、表达与性能未验证边界、倾向/来源去重、资料说明、相同观点集合索引、建议动作结构及人工决定绑定。不会自动判断自然语言推理成立；不合格反证的具体意见由助手另行逐项提供。
- runner.py：校验65份冻结文件，新建修订，追加人工决定，只读核对两库。没有付费调用入口。
- render.py：四份安全转义的静态HTML，无脚本或外部资源；默认待复核，来源/倾向与助手意见可追溯，原稿独立链接保留。
- test_core.py：20项临时目录、脱敏样例及模拟检查，不联网、不读取真实评价、不写数据库。
- qa.py：已有返回回放禁止创建AI客户端，核对来源指纹、程序数字、原文、阶段隔离、状态和页面。首次检查保存日志，拒绝覆盖同修订离线日志；复测请另存修订。

助手意见保存在本机assistant-assessment.json，绑定baseline_digest，并非通用自动纠错规则或新模型结果。新商品/新来源须单独内容复核，不能照搬本例意见。原提取疑点未被放宽，900001/900002表达线索可见，但仍不计原基础事实统计，更未升级成已验证性能。

## 核对和重新输出

在项目CMD、已启用项目虚拟环境的条件下：

```cmd
python -m unittest tests.report_closeout.test_core -v
python -m tests.report_closeout.runner publish --root workspace/output/reports/review_report_closeout/20261030/125412-b2d26f89
```

publish会输出新修订目录，如revisions/0005，不覆盖0001～0004。需重新检查新修订时，将下面revision改为实际返回目录：

```cmd
python -m tests.report_closeout.qa --root workspace/output/reports/review_report_closeout/20261030/125412-b2d26f89 --revision workspace/output/reports/review_report_closeout/20261030/125412-b2d26f89/revisions/0005
```

## 登记人工决定

仅在用户已逐项明确审核决定时使用，助手不得自行补记“已采用”。以下为用法示例，不是本轮已执行记录：

```cmd
python -m tests.report_closeout.runner decide --root workspace/output/reports/review_report_closeout/20261030/125412-b2d26f89 --item "complete_report:p1:proposal" --decision "已采用" --reviewer "本人" --comment "已核对该方向建议，采用作为后续验证安排，不代表实物验证完成" --user-confirmed
python -m tests.report_closeout.runner publish --root workspace/output/reports/review_report_closeout/20261030/125412-b2d26f89
```

item来自报告内“查看复核编号”，decision支持待复核/已采用/不采用。每次决定保存新的decisions/000001.json等，后决定覆盖展示状态但不删前记录；单条采用不会让整张卡片自动通过。来源或候选内容身份变化则拒绝沿用原决定。reviewer是本地明确填写人，不提供账户身份认证；已有人工记录与未确认的助手建议分开保留。

本轮没有任何实际人工决定，decisions目录为空/不存在。测试中的填写人、采用及撤回均为临时脱敏模拟。

字段说明见[数据库字段取值说明的本地收尾补充](../../docs/数据库字段取值说明.md)，实现和实际证据见[收尾记录](../../docs/测试记录/评价报告离线收尾实施记录.md)。所有真实输入、助手逐项意见、请求/响应、HTML与截图保存在Git忽略的workspace。
