# 电商自动化工作台

单店铺 Python 自动化项目，提供统一命令行入口与本机 Web 页面。
当前版本完成架构迁移；商品草稿、报表下载、选品和经营分析按模块逐步接入。

## 快速开始

要求 Python 3.11+。在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe main.py --help
.venv\Scripts\python.exe main.py web
```

浏览器打开 http://127.0.0.1:8000。网页提供概览、真实任务记录与模块状态；当前为只读展示，不从网页启动自动化。
按 Ctrl+C 停止本地服务。开发服务仅用于本机，远程部署另行增加服务器、登录和权限配置。

当前检出目录已有 `.venv`，可以直接使用其中的 Python。项目支持可编辑安装，安装后也可运行 `ecommerce --help`。
普通 wheel 部署需要显式设置 `ECOMMERCE_ROOT` 为存放配置和运行资料的目录。

## 当前命令

```powershell
python main.py status                    # 已实现与待实现的功能
python main.py account login             # 复用会话，必要时人工扫码
python main.py account check             # 在线检查会话，失效返回需人工处理
python main.py product open              # 打开发布入口，回车关闭浏览器
python main.py product open --start      # 额外尝试点击“开启”
python main.py product open --no-wait     # 完成入口导航后关闭浏览器
python main.py tasks                     # 最近 50 条任务记录
python main.py web --port 8000           # 本地工作台
```

没有子命令时显示帮助，不自动打开店铺。尚未实现的 `product draft` 等命令不注册。

- `upload` 兼容为 `product open --start`，保留旧入口行为及提示。
- `download` 原本只完成登录骨架，现在明确提示尚未实现并返回退出码 2；登录请使用 `account login`。
- 退出码：0 成功；1 失败；2 参数错误或功能未实现；3 需要人工处理或浏览器被占用；130 取消。
- “开启”按钮原有定位失败尚未通过真实页面验证。页面识别只接受登录元素证据，不再仅凭地址不含 login 判定成功。
- 当前商品任务仅导航到发布入口，不填写商品、不保存草稿、不正式发布。

## 配置与路径

默认读取 `config/settings.toml`，文件不存在时使用内置默认值。可复制 `config/settings.example.toml` 后修改。

```powershell
python main.py --config config/settings.toml status
```

`--config` 要放在子命令之前。也可设置 `ECOMMERCE_CONFIG`。所有相对路径均基于项目根目录，不依赖命令运行目录；`ECOMMERCE_ROOT` 可显式覆盖根目录。
配置只保存非敏感设置；未来接入 AI 或外部服务时通过环境变量传递密钥。

旧根目录 `state.json` 和 `storage/taobao_browser_profile` 存在时自动复用，不移动或删除。
新安装默认保存至 `workspace/storage/sessions` 和 `workspace/storage/browser_profiles`。
设置 `browser.session_path`、`browser.profile_path` 可覆盖自动选择。
文件存在仅代表本地有资料，不能证明会话在线有效。

## 架构

```text
main.py                         统一启动入口
src/ecommerce/
  cli/                          命令解析和终端交互
  web/                          Flask 页面、查询接口、模板和静态资源
  modules/
    account/                    登录流程
    product/                    商品业务流程
    monitoring/                 工作台与任务状态查询
    collection/                 采集（预留）
    selection/                  选品（预留）
    reports/                    报表（预留）
    analysis/                   分析（预留）
  platforms/
    qianniu/                    千牛页面操作
    taobao/ sycm/ wanxiang/      平台适配（预留）
  infrastructure/
    browser/                    浏览器生命周期、状态存取、通用操作
    storage/migrations/         SQLite 存储与版本迁移
    tasks/                      统一执行器、重试、跨进程互斥
    ai/                         AI 接入（预留）
  shared/                       配置、日志、异常及功能目录
config/                         本地配置与示例
workspace/                      输入、输出、登录资料、数据库、日志
  input/products/ images/       Excel 和图片
  output/raw/ processed/ reports/
  storage/sessions/ browser_profiles/ database/
  logs/screenshots/
tests/                          单元和集成测试
```

查询：入口 → 业务查询 → 存储。
执行：入口 → 任务执行器 → 业务流程 → 平台操作 → 浏览器。
业务也可使用存储能力或可选 AI 能力。

SQLite 是任务状态和执行结果的唯一来源，报表导出为派生结果。
浏览器资料目录上的操作系统文件锁防止本项目不同进程同时操作同一店铺。
关闭或崩溃后锁自动释放；锁文件保留是正常现象，不表示仍被占用。
异常退出的运行记录在下次取得店铺锁启动任务时标记为需要人工处理；不会自动重放业务。
本次只实现同步任务执行。未来网页启动长任务时需增加后台执行入口并复用执行器。

## 开发与扩展

1. 在业务模块添加明确的参数、返回值及业务规则；不要依赖命令行输入。
2. 将页面选择器与交互放在对应平台模块，通过公开方法调用。
3. 在 `cli/commands` 接入任务执行器；查询接口调用查询服务。
4. 实际完成并验证后更新 `shared/features.py` 的功能状态。
5. 新数据库变更新增有序 SQL 迁移，不修改已经发布的迁移。
6. 新增恢复机制时先定义如何识别已完成操作，再决定能否重试。

数据依赖可按需安装 `pip install -e ".[data]"`，AI 依赖使用 `pip install -e ".[ai]"`。
Excel、JSON 等具体业务读写能力尚未接入，不建立无实际用途的占位函数。

## 验证

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据库和模拟业务依赖，不操作真实店铺。
覆盖配置兼容、跨进程锁、异常结果、遗留任务、网页状态、HTML 转义、外部目录启动及单浏览器编排。
真实扫码登录和发布页面需另行人工验证，本次未执行商品发布。

## 迁移记录

原来的 `automation/`、`browser/`、`data/`、`ai/`、`utils/` 代码按职责迁入 `src/ecommerce`；未实现的原样返回占位函数已移除。
原始代码和未提交差异备份在 `workspace/backups/before-refactor/`，原有 `test.py` 手工计时脚本保留至 `tests/manual/timing_probe.py`。
旧 `logs/`、`output/`、`storage/` 的运行资料保留。新版日志写入 `workspace/logs`，旧日志不会伪装成新任务记录。
尚未实现业务按优先级：Excel → 千牛草稿；生意参谋/万相台报表及网页分析；关键词/竞品采集与选品。
