# Agent Instructions - 电商自动化系统

## 目标
本项目用于使用 Playwright + Python 构建电商自动化系统，主要包括：
- 千牛后台自动化
- 生意参谋数据下载
- Excel数据处理
- 商品信息生成

---

## 技术栈
- Python 3.10+
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
- 禁止重复手动登录
- session必须可复用

---

## 稳定性要求
- 必须使用 wait_for_selector
- 禁止只使用 sleep
- 必须加随机延迟（human-like behavior）

---

## 文件结构规范
automation/
  login.py
  download.py
  process.py
  utils.py

---

## 禁止行为
- 不允许硬编码 selector（除非稳定）
- 不允许无等待点击
- 不允许单文件写全部逻辑

---

## 目标原则
稳定性 > 速度 > 简洁性