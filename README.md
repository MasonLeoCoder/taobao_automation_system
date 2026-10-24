# 电商自动化工作台

单店铺 Python 自动化项目，统一命令行与本机 Web 共用业务逻辑。

从[使用指南](docs/使用指南.md)开始操作；查详细参数、数据库或历史记录，进入[文档索引](docs/README.md)。本轮人工测试的顺序、状态和证据统一维护在[功能逐项验收清单](docs/功能逐项验收清单.md)，程序完成不等于人工验收通过。

## 当前能力

| 模块 | 已接入范围 |
|---|---|
| 账号 | 淘宝与千牛独立登录、会话检查与复用、人工验证及弹窗处理 |
| 淘宝列表 | 关键词搜索、销量排序、按`--page`逐页采集并独立导出JSON/CSV；默认1页，`--limit`为每页上限 |
| 单商品详情 | 商品ID或链接入口；视频、主图、SKU、参数、详情图片、评价概览、评价明细、问大家八模块 |
| 数据与查询 | MySQL任务、日志、采集记录、文件索引；CLI/Web历史查询；命令行和本地日志保留 |
| 商品填写 | 已适配粘毛器类目Excel、主图/详情图/视频及SKU填写；审核本轮实际内容后可提交到仓库 |

列表商品可用`--with-details`逐个采集默认八个详情模块，已完成本地验证，真实平台验收待另行确认。平台以图搜图、Web来源筛选及实时日志推送尚未接入。商品草稿、批量发布、报表下载、选品和经营分析仍待实现。详细能力可执行 `python main.py status` 查询。

## 快速开始

要求 Python 3.11+。在项目根目录的CMD执行；已有虚拟环境和依赖时可直接使用其中的Python：

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe main.py --help
.venv\Scripts\python.exe main.py web --port 8000
```

打开 http://127.0.0.1:8000 查看概览、任务、日志和采集资料，页面支持手动刷新。当前Web只查询数据，不启动浏览器任务。按Ctrl+C结束本地服务。

可编辑安装后也可运行 `ecommerce --help`。普通wheel部署需用 `ECOMMERCE_ROOT` 指定配置和运行资料目录；配置、浏览器模式及本地模拟验证见[环境与会话配置](docs/操作参考/环境与会话配置.md)。

## 数据与配置

默认配置为 `config/settings.toml`，新环境可参考 `config/settings.example.toml`。`--config` 放在子命令之前，相对路径统一按项目根目录解析，敏感值通过环境变量提供。

本机正式库为 `taobao_automation`，独立验证库为 `taobao_automation_verify`；任务和结构化采集以MySQL为准。时间字段、命令行和网页统一北京时间（UTC+08:00）。图片、视频、HTML、原始JSON/CSV保留为文件，模块SQLite用于执行期缓存与导出核对。

新环境默认sqlite/files；使用MySQL前显式升级并配置后端，已有文件不自动导入。数据库与补写操作见[完整命令参考](docs/操作参考/命令参考.md#mysql-任务与日志cmd)。2026-10-23曾按用户要求清空旧数据及备份，之后测试已产生新记录；[重置基线](docs/历史记录/从零测试说明.md)仅记录清空时点，不代表当前为空库。

数据库参考分工：

- [字段取值说明](docs/数据库字段取值说明.md)：所有字段的实际值、中文含义、来源、空值和真实例子，唯一取值维护入口。
- [完整字段字典](docs/数据库字段字典.md)：字段类型、原生中文备注及约束，由同版模型生成。
- [表关系说明](docs/数据库表关系说明.md)：主外键、逻辑关联、上下游和更新/删除规则。

## 项目结构

```text
main.py                      统一启动入口
src/ecommerce/
  cli/                       命令解析、终端交互
  web/                       本机页面与查询接口
  modules/                   账号、商品、采集及查询业务
  platforms/qianniu/         千牛页面适配
  platforms/taobao/          淘宝搜索、列表和详情适配
  platforms/sycm/ wanxiang/  报表平台预留
  infrastructure/            浏览器、MySQL/SQLite、任务及可选AI
  shared/                    配置、时间、日志、异常与功能目录
config/                      配置示例及本机配置
docs/                        常用入口、规范、参考与历史记录
tests/                       单元测试、本地页面及独立库验证
workspace/                   输入、原文件、报告、日志与登录资料
```

查询：入口 → 业务查询 → 存储。执行：入口 → 统一任务执行器 → 业务流程 → 平台适配 → 浏览器。

## 开发与验证

开发约定见[agent.md](agent.md)。业务不直接读取终端输入，页面交互集中在平台模块；新能力真实验证后更新 `shared/features.py`。数据库变更新增有序迁移，维护原生中文备注、字段字典、取值说明和关联文档，不改写已执行迁移。

自动测试使用临时资料和模拟依赖，真实店铺测试单独确认：

```cmd
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

本地浏览器和独立MySQL测试须显式启用，见[本地验证说明](docs/操作参考/环境与会话配置.md#本地验证)。可选依赖使用 `pip install -e ".[data]"` 或 `pip install -e ".[ai]"` 按需安装。

workspace、登录资料和敏感配置不纳入版本库。同步浏览器任务经过统一执行器和资料目录锁，不自动重放发布动作。详细上传与字段规则见[千牛商品填写说明](docs/操作参考/千牛商品填写说明.md)，旧重构经过见[项目重构记录](docs/历史记录/项目重构记录.md)。
