"""第十八版冻结模型：生成与引用分开、本地检查审计，仅更新原生备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v17 import build_metadata as previous_metadata,NEW_TABLES

REVISION='0018_report_checks'
PREVIOUS_REVISION='0017_direct_review_report'
COMMENT_UPDATES={
 ('analysis_tasks','status'):'queued待启动、running执行、succeeded本轮正文完整生成或本地空白说明、partial有可读部分但正文未完成、failed失败、interrupted中断；V6引用质量另在报告validation，不因引用疑点重复付费；旧状态仅明确本地检查后更新',
 ('analysis_calls','status'):'inflight请求已记账、succeeded完整返回且V6总稿章节齐全、invalid返回中断/空稿/缺章节、unknown结果或用量未知、failed执行失败；引用匹配另行检查，不改变原调用历史状态；状态不代表语义或性能已验证',
 ('analysis_reports','content'):'reviews-report-v6新增generation正文完成状态、validation独立引用校验/逐项原因/位置/原文与次数、inspection规则版本/北京时间/来源/响应哈希及前报告身份；每次本地检查追加版本并原子更新任务，原响应/旧报告保留；旧检查不在GET重写，coverage引用来源数与引文次数分开',
}


def build_metadata():
    """继承已冻结结构，仅更新三条语义备注；参数：无；返回：完整模型。"""
    metadata=previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items():metadata.tables[table].c[column].comment=comment
    return metadata
