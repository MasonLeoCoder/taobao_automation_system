# 四类交叉分析

service处理只读预览/明确费用创建；store校验2～4类同商品固定版本并冻结正文/范围/依据；planner复用综合容量与预算核心，完整章节优先分组，直接1次或N＋1；runtime复用调用收据/持久化/零自动重试；report区分三项必需章节与可选章节，报告归属/已有图号/连续文字定位不冒充语义验证。

公共提示在prompts/report.txt，组稿提示在prompts/group.txt，组合重点由kinds.py维护并冻结。只有文本报告和相关片段发送，没有本轮视觉调用。页面沿原module=composite与/analysis/composite URL，新任务analysis_type=cross/rule_version=cross-v1，旧composite/images不列入新候选和历史，不删除或迁移。

正式接口只由用户页面确认产生费用；单元测试及浏览器脚本使用隔离库/模拟客户端。详见docs/变更记录/四类交叉分析.md及字段取值说明。
