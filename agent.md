# Agent Instructions - 电商自动化系统

## 目标
本项目用于使用 Playwright + Python 构建电商自动化系统，主要包括：
- 千牛后台自动化
- 生意参谋数据下载
- Excel数据处理
- AI内容生成

---

## 技术栈
- Python 3.11+
- Playwright (浏览器自动化)
- Pandas (数据处理)
- OpenAI API / Codex (代码生成辅助)

---

## 编码规范
- 所有浏览器操作必须使用 Playwright
- 所有自动化流程必须拆分为函数
- 禁止写一整段混乱脚本
- 每个模块必须可独立运行

---

## 系统架构强制约束（核心规则）
### 系统必须分层（禁止混合逻辑）

所有代码必须严格分为以下层级：

#### Main Layer（系统控制层）
- 负责任务调度
- 负责流程编排
- 只允许调用，不允许写业务逻辑

---

#### Automation Layer（业务流程层）
- login / navigate / download / upload
- 负责完整业务流程
- 必须由 main.py 调用

---

#### Browser Layer（执行层）
- Playwright 操作层
- click / fill / wait / goto
- 禁止写业务逻辑

---

#### Data Layer（数据层）
- Excel / CSV / Pandas
- 数据清洗与处理

---

#### AI Layer（生成层）
- Codex / GPT
- 只负责内容生成，不参与流程控制

---

### 调用顺序强制规则

所有任务必须按照以下顺序执行：

Main → Automation → Browser → Data → AI

禁止反向调用

---

### 数据流向规则（必须单向）

数据必须按以下方向流动：

Browser → Automation → Data → AI → Output

禁止回流（例如 AI 控制 Browser）

---

### 跨层禁止规则（非常重要）

以下行为禁止：

- AI Layer 直接操作浏览器
- Data Layer 调用 Playwright
- Browser Layer 写业务逻辑
- Automation Layer 写 UI细节

---

### 模块职责唯一性（强制）
每个模块只能做一件事：

- login.py → 只负责登录
- download.py → 只负责下载
- process.py → 只负责数据处理

禁止混合功能

---

### main.py 约束（核心）
main.py 只允许：

- 调度函数
- 控制流程顺序
- 不允许写具体业务逻辑

---


## 自动化流程标准

所有业务流程必须遵循：

1. login（登录模块）
2. navigate（页面导航）
3. wait（等待元素）
4. action（点击/输入）
5. download（数据下载）
6. process（数据处理）

---

## 登录管理规则
- 必须使用 storage_state.json
- session必须复用
- session失效必须重新登录

---

## 稳定性要求
- 必须 wait_for_selector
- 禁止无等待点击
- 必须加入随机延迟
- 所有关键操作必须 retry（最多3次）

---

## 异常处理
必须支持：

- 页面失败自动 reload
- session失效自动重登
- 操作失败自动 retry
- 网络异常自动恢复

---

## 数据输出
- 输出必须存入 /output/
- 文件必须按日期命名：

YYYY-MM-DD_task.xlsx

- 禁止覆盖历史数据

---

## 日志系统
- 必须记录所有关键操作
- 分级：INFO / WARNING / ERROR
- 输出到 /logs/run.log

---

## Codex 使用规则
仅用于：
- 生成函数代码
- 优化模块逻辑
- 数据处理代码
- AI内容生成

禁止：
- 生成完整系统脚本
- 混合多个业务逻辑
---

## 编码规范
- 必须函数化
- 必须模块拆分
- 必须有 docstring
- 禁止大段脚本

---

## 注释规范
所有函数必须包含：
- 功能说明
- 参数说明（类型+用途）
- 返回值说明

---

## 禁止行为
- 无等待点击
- 无 session 操作
- 硬编码 selector（不稳定）
- 单文件写全部逻辑
- 无异常处理

---

## 目标原则
稳定性 > 可维护性 > 自动化效率 > 执行速度
