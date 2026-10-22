# 电商自动化工作台

单店铺 Python 自动化项目，提供统一命令行入口与本机 Web 页面。
当前已接通粘毛器类目的单商品资料读取、自动填写及提交到仓库；商品草稿、报表下载、选品和经营分析按模块逐步接入。

CMD 命令、参数说明和一个完整示例见 [简短使用指南](D:/VSCodeProject/taobao_automation_system/docs/使用指南.md)。
详情页完整采集命令和参数表见 [使用指南：淘宝单商品详情页入口](docs/使用指南.md#淘宝单商品详情页入口powershell)。

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
python main.py account login             # 千牛登录，复用会话，必要时人工扫码
python main.py account check             # 在线检查千牛会话
python main.py account login --platform taobao # 淘宝买家端独立登录，按页面选择登录方式
python main.py account check --platform taobao # 在线检查淘宝会话
python main.py taobao search --keyword "粘毛器" # 首页搜索，确认首屏后保留窗口
python main.py taobao collect --keyword "粘毛器" # 按销量采集当前第一页，默认不限制条数，保存 JSON/CSV
python main.py taobao collect --keyword "粘毛器" --limit 20 # 显式指定时最多保存 20 件有效商品
python main.py taobao detail --input "1007759256325" # 默认采集全部已实现详情项目，评价100条、问题50个
python main.py taobao detail --input "https://item.taobao.com/item.htm?id=1007759256325" # 链接同样默认采集全部项目
python main.py taobao detail --input "1007759256325" --all # 全部项目，并取消评价和问题数量上限
python main.py taobao detail --input "1007759256325" --video --parameters # 显式指定时只采集视频和参数
python main.py taobao detail --input "1050158363561" --main-images # 直接读取页面主图列表，按实际数量保存到 1-1主图；重跑可补采未完成项
python main.py taobao detail --input "1050158363561" --skus # 标题、销量、逐项价格及sku图；孤立不可选警告可继续后续模块
python main.py taobao detail --input "1050158363561" --parameters # 动态读取参数信息，保留原文、同名项和原始HTML
python main.py taobao detail --input "1050158363561" --detail-images # 图文详情全部图片按序保存到商品目录的详情图
python main.py taobao detail --input "1050158363561" --review-summary # 评价数量、好评率与总结标签
python main.py taobao detail --input "1050158363561" --reviews # 默认100条有效评价，模板好评跳过，媒体只存链接
python main.py taobao detail --input "1050158363561" --reviews --all # 取消上限，采集页面可加载的全部评价
python main.py taobao detail --input "1050158363561" --questions # 默认50个问题，每题采集页面可提供的全部回答
python main.py taobao detail --input "1050158363561" --questions --all # 取消问题数量上限
python main.py taobao pause              # 在另一个终端请求暂停当前淘宝任务
python main.py taobao stop               # 请求停止，不启动新浏览器
python main.py taobao continue           # 向当前人工提示发送继续，需先完成页面处理
python main.py product open              # 打开发布入口，回车关闭浏览器
python main.py product open --keyword "粘毛器" # 搜索后停留在结果页，回车关闭浏览器
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
- 商品任务支持搜索、类目和品牌选择、两组主图、详情图、视频、Excel基础信息和SKU填写；默认完成后停留检查。追加 `--submit` 会检查并提交到仓库，异常时按人工提示处理。网页自身可能自动保存。

主图调试命令（完成后按回车关闭浏览器）：

```powershell
python main.py product open --keyword "粘毛器" --category "宠物/宠物食品及用品 > 猫/狗美容护理工具 > 粘毛器" --brand "无品牌/无注册商标" --main-images-dir "workspace/input/products/CJ001/1-1主图"
```

主图目录必须恰好包含编号 1～5 的五张 png/jpg/jpeg 图片，每张不超过 20MB。
上传素材会附加本次批次标识以避免选错同名图片，本地文件不改名；重复运行会再次上传一批素材。
主图位置已有图片时停止，不自动覆盖。每张填入后核对图片地址，全部完成后保存截图。

上传 3:4 主图时，在上述命令后增加：

```powershell
--portrait-images-dir "workspace/input/products/CJ001/3-4主图"
```

3:4 目录支持编号 1～5；也支持编号 1～4 加唯一一张未编号图片，该图片放在第 5 位。
先完成 1:1，再上传 3:4，分别核对各区域五张图片和顺序。
真实验证已完成两组主图上传及逐张顺序核对。上传窗口超出视口的问题已修正；选择图片过程中可能出现滑块验证。
有界面的命令遇到选图滑块时保留浏览器，响一次提示音并显示当前步骤，等待人工反馈。
手动完成验证后，在终端输入“继续”或“已完成”并回车；空回车不会放行。
人工等待没有超时，没有反馈一直暂停；即使弹窗自行消失，也需要明确反馈。
收到反馈后再次检查验证是否消失，仍存在则继续等待，同一次验证不重复响铃。
在聊天中由助手运行时，可以回复“验证已完成，继续”，由助手向等待中的任务转交反馈。
无人值守模式转为需要人工处理，不自动操作滑块。

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

淘宝买家端和千牛独立登录。`account` 不指定 `--platform` 时仍默认千牛，商品发布也继续使用千牛。
淘宝默认使用 `workspace/storage/sessions/taobao.json` 和 `workspace/storage/browser_profiles/taobao`，
不导入千牛的 Cookie、状态文件或旧版浏览器目录。可在配置 `[taobao]` 中设置 `session_path`、`profile_path`，两个平台不能共用资料路径。
已有淘宝持久化资料时优先复用其中的新状态，导出的会话文件仅用于初始化新资料，避免旧文件覆盖刚完成的人工验证。
淘宝登录打开买家端页面，由用户自行选择扫码、短信或密码并完成验证；不会自动刷新二维码，等待不设超时。
只有在淘宝首页识别到可见账号昵称，且在线复核通过后才保存状态；仅进入首页或存在本地文件不会视为登录成功。
淘宝登录成功后会立即提示会话已保存，并保留浏览器窗口，直到用户手动关闭。
需要保存后立即关闭时使用 `account login --platform taobao --no-wait`。
登录前可关闭淘宝浏览器或按 Ctrl+C 取消。淘宝登录检查不会自动要求扫码，检查结束后关闭窗口，状态未确认时返回需要人工处理。
浏览器任务仍串行执行；请先结束千牛任务再启动淘宝登录。

淘宝登录与检查已接入独立的验证及弹窗守卫。遇到常见验证、未知对话框/遮罩、意外新标签页或相关访问异常时记录现场并提示人工处理。
须明确输入“继续”，且页面复查通过后才恢复；输入“停止”结束任务。原生确认框先选择“确认”或“取消”，再输入“继续”。
暂停期间保留浏览器，不设人工等待超时，也不自动重放操作。终端输入关闭时继续保持暂停，可从另一个终端发送 `taobao continue`；原生弹窗用 `taobao confirm`/`taobao cancel`，先明确决定后再继续。
跨进程反馈绑定当前提示编号，旧反馈不会放行下一次验证，也可通过 `taobao stop` 请求停止。
事件记录在 `workspace/logs/taobao`，截图在 `workspace/logs/screenshots`；任务列表会显示执行中的人工等待状态。
关键词搜索入口和首屏校验已接入；2026-10-21 使用普通 Chrome 启动连接模式完成一次真实的“粘毛器”自动搜索、首屏复核及摘要保存，日志确认仅提交一次且未出现验证阻断。本次成功不代表后续不会验证。
搜索摘要在 `workspace/output/raw/taobao_search`，提交前按 `[taobao.pacing].before_search` 停顿并重新核对关键词，首屏确认后按 `.search` 停顿并复核；两者默认均为 2～4 秒。
搜索动作和实际主页面导航分别记入任务事件，便于检查重复提交。默认保留窗口，增加 `--no-wait` 可结束后关闭。
`taobao collect --keyword "粘毛器"` 默认搜索后点击“销量”，确认页签选中且列表更新，再读取第一页已加载的标题、展示价格及条件、店铺、销量提示原文、地区和可确认的商品 ID/链接，保存 JSON/中文 CSV 至 `workspace/output/raw/taobao_products`。已选销量时不重复点击，排序不明或未确认列表更新时暂停待人工，不按综合排序继续采集。`taobao search` 仍只搜索并保存摘要。
2026-10-21 已在真实搜索页完成 20 件字段采集，并单独通过 Playwright 点击销量、确认页签及列表更新；整合后的正式流程已接入本地模拟页面测试。广告标记未识别时明确记录，不猜测。
新版以 `#content_items_wrapper` 直接子格子为边界，按已确认的 DOM 读取完整标题、显示主图、价格、销量提示、店铺、榜单/标签、视频及图标信息；广告图片命中则标注，调查卡跳过，同ID不同位置保留，并区分字段缺失原因及核对整页分类数量。
默认不限制当前页商品条数，只有传入 `--limit 正整数` 时才限制有效商品数量，无固定 50 件上限。调查卡及结构与标题均匹配的“大家都在搜”推荐词卡单独统计并跳过，不占商品数量。需要时同页分段滚动加载主图，不翻页、不打开详情，数量不足按实保存，不同批次保留历史。验证和页面条件异常继续暂停待人工，明确继续并复核后恢复。
PowerShell 命令、字段含义、验证处理和文件位置见 [使用指南](docs/使用指南.md) 的“淘宝关键词搜索本地测试”和“淘宝第一页商品采集”两节。详情及翻页仍待接入；采集间隔和恢复接口见 [淘宝采集执行约定](docs/淘宝采集约定.md)。

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
    collection/                 淘宝关键词搜索、第一页字段与 JSON/CSV 导出
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
固定格式商品 Excel 读取已接入，用于基础信息及多条 SKU；更多业务数据导入导出按实际需求接入。

## 验证

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据库和模拟业务依赖，不操作真实店铺。
淘宝守卫的真实浏览器测试显式启用，所有网页及响应均由本地模拟，不访问淘宝：

```powershell
$env:TAOBAO_BROWSER_TESTS="1"
.venv\Scripts\python.exe -m unittest tests.integration.test_taobao_guard_browser -v
.venv\Scripts\python.exe -m unittest tests.integration.test_taobao_search_browser -v
.venv\Scripts\python.exe -m unittest tests.integration.test_taobao_collection_browser -v
```

覆盖配置兼容、跨进程锁、异常结果、遗留任务、网页状态、HTML 转义、外部目录启动及单浏览器编排。
已在真实千牛页面验证当前粘毛器类目的素材上传、字段填写及提交到仓库。自动测试使用临时数据与本地页面，更多类目仍需逐项适配和验证。

## 迁移记录

原来的 `automation/`、`browser/`、`data/`、`ai/`、`utils/` 代码按职责迁入 `src/ecommerce`；未实现的原样返回占位函数已移除。
原始代码和未提交差异备份在 `workspace/backups/before-refactor/`，原有手工计时脚本归档为该备份目录中的 `test.py`。
旧 `logs/`、`output/`、`storage/` 已清理；根目录 `state.json` 保留以尝试恢复登录状态。浏览器资料将自动建立在 `workspace/storage/browser_profiles/`，必要时重新扫码。新版日志写入 `workspace/logs`。
打包生成的 `build/`、`workspace/build/` 和源码缓存已清理，后续运行或打包可能再次生成。`src/*.egg-info/` 是可编辑安装的元数据，保留供当前安装使用。
尚未实现业务按优先级：Excel → 千牛草稿；生意参谋/万相台报表及网页分析；多页/详情采集与选品分析。

### 千牛全局验证守卫

账号登录、登录检查、商品导航、搜索、类目/品牌、两种主图上传共用 `platforms/qianniu/session.py`。会话启动时为已有页面和新标签页安装守卫，扫描所有 iframe。页面操作前的可操作性检查、业务步骤前后和异常出口均检查验证；登录长轮询也设检查点。没有运行中的 Playwright 操作（例如终端等候关闭浏览器）时不会后台持续轮询。

遇到已识别验证：记录当前步骤 → 提示音/终端提示 → 阻塞等待“继续”或“已完成” → 重新检测 → 验证消失后恢复。等待没有人工超时，空回车不会继续。无交互回调或输入已关闭时返回 `needs_attention`。人工处理超过原操作时限时，可恢复子步骤在原位置重新核对，接着向下执行；不会因此退出浏览器或重跑整段上传、发布。

关键源文件：
- `platforms/qianniu/verification.py`：检测规则、VerificationGuard、VerificationRequest、VerificationHandler、步骤装饰器。
- `platforms/qianniu/session.py`：千牛唯一会话入口。
- `cli/interaction.py`：提示音和人工反馈。

后续新增操作使用此会话并加 `@qianniu_operation`；长轮询调用 `checkpoint("步骤名称")`。可在会话参数 `verification_handler=` 注入实现 `__call__(request: VerificationRequest) -> None` 的处理器；默认仅人工处理，未实现自动解验证。处理器需阻塞到处理结束或抛错，返回后框架仍复查。自动处理器若无法处理，应转人工或抛出 `NeedsAttention`，不得无间隔反复尝试。

覆盖的是集中登记的滑块/验证组件特征，并非对任意未知验证界面的保证。Playwright 定位器处理器会将处理耗时计入原操作超时；平台断言统一从 `verification` 导入 `expect`，只重新执行失败的只读核对。等待、填写和有完成判定的局部动作用 `page_step`，先核对已生效结果再决定是否恢复该局部动作。没有新反馈不会反复尝试。不要用后台线程调用同步 Playwright，也不要为延长人工等待关闭全部浏览器操作超时。

扩展回调可能在 Playwright 操作回调中执行，接口用于人工交互或外部处理服务，不允许在回调内再调用带自动等待的 Locator 操作（会互相等待），也不允许跨线程操作同步 Page。若后续需要用 Playwright 执行复杂验证流程，应另行设计独立控制方式，不能直接塞入此回调。检测异常、无人工入口或输入通道关闭时，中途拦截会关闭本任务浏览器，确保原点击不会继续；正常人工等待保持浏览器打开。

### 千牛全局弹窗守卫

同一会话自动接入 `platforms/qianniu/popups.py`，无需新增命令参数。按可见对话框、遮罩、iframe及新标签页检查；隐藏iframe不作活动弹窗处理。只自动关闭明确识别的“SKU管理页引导”，关闭失败转人工。图片、详情、视频素材选择器和创建规格面板按当前操作及结构放行；其中出现额外未知对话框仍需人工处理。

未知弹窗先保存 `workspace/logs/screenshots/popup-*.json` 及同名截图，播放提示音，等待明确反馈后复查。原生对话框只记录JSON，终端必须输入“确认”或“取消”，不会自动接受。尚未识别的遮挡若导致子步骤超时或断言失败，也保存现场并等待检查；输入“继续”后留在原浏览器，恢复当前子步骤，再执行后面的流程。条件仍不满足则再次等待人工处理，不假报成功、不跳过核对。现场文件写入失败不会取消人工等待。检测范围受已登记的页面结构限制，并非保证识别所有弹窗。

视频上传后若进入额外优化页面，先等待人工关闭，再核对选择器已关闭且商品封面与本次素材一致，随后继续填表；本次素材名、上传调用和已完成的确认动作不会重跑。文件选择请求在局部恢复期间复用，实际 `set_files`/`set_input_files` 不在重放范围内。浏览器已关闭、输入通道关闭或未接入局部恢复的代码错误仍如实报告中断。

本地浏览器回归测试（不登录、不访问店铺）：PowerShell 设置 `$env:QIANNIU_BROWSER_TESTS='1'` 后运行 `.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests`。

### 上传全部详情图

在现有 `product open` 命令后增加 `--detail-images-dir "workspace/input/products/CJ001/详情图"`。流程在两组主图之后上传详情图，按数字文件名排序（1、2、…、10），没有固定五张限制；每批最多50张，逐批核对，失败不自动重复上传。

详情图支持 jpg/jpeg/png，数字编号必须唯一，每张不超过当前详情选择器要求的6MB。详情编辑区已有内容时停止，避免追加重复内容。高宽比大于2的图片使用千牛自带“确认全部分割”，完整保留各段，并核对分段顺序与地址。源图数量可能与最终模块数量不同：CJ001 的22张源图中，第9张分为两段，最终23个图片模块。

源码：`src/ecommerce/platforms/qianniu/product_details.py`；文件检查：`src/ecommerce/modules/product/assets.py`。上传过程使用公共验证守卫；不点击“提交宝贝信息”。

### 商品视频

现有命令追加 `--video-file "workspace/input/products/CJ001/test001.mp4"`。商品视频步骤在详情图后执行。已有视频时停止，避免覆盖或重复添加；素材上传成功后选择本次唯一名称的视频，核对选中数量和商品页封面后才报告完成。

源码：`src/ecommerce/platforms/qianniu/product_video.py`。自动展开页面的可选字段；视频素材名控制在17字符以免平台截短；悬停后勾选，并兼容确认按钮中的空格。保持全局人工验证守卫，不提交商品。CJ001 的文件为9:16，千牛标注此比例在商品详情页不展示，可用于首页推荐、微详情等场域。

### 固定格式商品资料与店铺分类

命令追加 `--product-info-file "workspace/input/products/CJ001/产品信息.xlsx"`。Excel“分类”列 → 页面“店铺中分类”，按完整名称选择并核对。当前填写已确认的店铺分类、两种标题、五个下拉商品属性、销售规格，以及每个SKU对应的价格、数量和商家编码，再填写一口价和商品级商家编码；SKU分类默认选择“单品”。

固定格式：`Sheet1` 第1行基础信息表头、第2行该商品的值；空行后以“销售规格”为首列表头，下面是规格记录。读取保留字段名、数值、空值及文本编码，只读文件；表头重复、公式、缺少工作表、空分类在浏览器启动前报错。

店铺分类重复执行时先检查是否已勾选，避免点击取消；未找到、重名、不可选择或超过20项时记录WARNING并跳过，其他能匹配的继续勾选。源码：`modules/product/workbook.py`、`platforms/qianniu/product_shop_category.py`。本步骤仍使用全局人工验证守卫。

### 宝贝标题

`--product-info-file` 读取“宝贝标题”列并填写页面同名字段，位于视频步骤后；当前CJ001取 `Sheet1!B2`。不使用AI生成或修改标题。字段为空时启动前报错；页面已有不同标题时停止核对，同值不重复填写；填写后比对输入框完整值并核对页面60字符计数，避免截断却报告成功。

源码：`src/ecommerce/platforms/qianniu/product_basic.py`。仍使用全局人工验证守卫，不提交商品。

### 店铺分类多选

Excel“分类”单元格可写 `家居清洁;猫毛清洁;除味除臭;测试无`，推荐分号分隔，同时支持中文分号、逗号、顿号、换行。去除首尾空白及重复项，按输入顺序匹配。

能唯一匹配的勾选，已选项保持；未找到、重名或不可选项逐项写入 `workspace/logs/run.log` 的WARNING日志并继续。选择上限20项包含页面原有选择，超过上限的记录跳过。每项选择回读检查，最后日志及任务结果返回实际已选/跳过数量。可恢复的定位或点击超时等待人工处理后继续；其他真实错误如实报告，不伪装成分类缺失。

### 导购标题

`--product-info-file` 将“导购标题”列填入页面同名字段，紧接宝贝标题步骤。与宝贝标题共用输入、回读和页面字符计数检查，导购标题要求20～30字符（10～15个汉字），低于最小长度或有平台错误不报告成功。空值在启动前报错，已有不同内容时停止核对，同值不重复填写。当前CJ001读取 `Sheet1!C2`；不会根据“品牌+品类词+利益点”另行生成标题，使用Excel原文。两种标题的公开操作均接入全局人工验证守卫。

### 适用场景

`--product-info-file` 在两个标题之后读取“适用场景”列，点击页面输入框并精确选择同名选项。列缺失、空值、未匹配、同名有歧义时按本次打开的下拉框实际顺序选择第一项。不会把“地板”写成固定默认值，也不选平台推荐值代替第一项。

回退写WARNING日志，包含Excel原值、原因和第一项名称；选择后回读核对并写INFO实际选择。没有可用选项或发生点击、验证、平台校验异常时停止处理，不伪报成功。源码：`src/ecommerce/platforms/qianniu/product_attributes.py`，仍接入全局验证守卫。

### 适用宠物类型

在适用场景后读取Excel“适用宠物类型”列，按完整名称精确选择；列缺失、空值、未匹配或同名有歧义时选择当前下拉框第一项，并记录原值、回退原因、实际选项。当前CJ001的“通用”可匹配；第一项由页面读取，未写死“长毛猫”。

与适用场景共用 `product_attributes.py` 的匹配/回退逻辑；只读选择器核对显示标签及其title，普通输入选择器核对input值。选择后验证弹窗关闭及平台错误，所有公开操作继续使用全局人工验证守卫。

### 握柄形态

在适用宠物类型后读取Excel“握柄形态”列，复用属性选择逻辑：精确匹配优先；缺失、空值、未匹配时选择本次下拉列表第一项，并记录原值、回退原因、实际选择。当前CJ001的“折叠柄”可精确匹配；第一项由页面读取，不写死。选择后回读验证，继续使用全局人工验证守卫。

### 粘面材质

在握柄形态后读取Excel“粘面材质”列，复用属性选择逻辑。精确匹配优先；缺失、空值或未匹配时选择当前下拉框第一项，并记录原值、回退原因和实际选择。当前CJ001“硅胶”可精确匹配；默认选项按页面顺序读取，不写死。只读选择器回读标签及title，继续保留全局人工验证守卫。

### 粘性强度

在粘面材质后读取Excel“粘性强度”列，复用相同属性选择逻辑：精确匹配优先；缺失、空值或未匹配时选择当前下拉框第一项，WARNING记录原值、回退原因和将选的第一项，回读后INFO记录实际选择。

当前CJ001“强粘”精确匹配；页面顺序为中粘、弱粘、强粘，因此回退时选中粘，与平台推荐“强粘”无关。默认值仍按页面读取，不写死。保留全局人工验证守卫，遇到没有可用选项或真实操作异常时停止。

### 销售规格

在粘性强度后读取销售规格表中每一行的“销售规格”列，按行顺序统计并填写全部名称。无规格记录、名称为空或重复时在浏览器启动前报错，不按分隔符拆分单个规格名称。

点击“创建规格”并选择“自定义填写规格”；填写一项并回读，再通过加号添加下一项，最后一项之后不增加空行。核对面板数量、全部名称及顺序后点击“确认创建”，再核对商品页生成的规格行。相同已创建列表只核对，不重复创建；不同列表停止，不覆盖或追加。若大量规格受页面虚拟滚动影响而无法完整回读，则停止核对，不报告完成。

源码：`src/ecommerce/platforms/qianniu/product_sku.py`。每项及最终结果写入日志，操作和逐项循环继续使用全局验证守卫；创建名称后进入下面的SKU明细步骤。

### SKU明细

创建规格后，将Excel销售规格表中的“价格”“数量”“商家编码”按“销售规格”名称匹配到商品页对应行，页面与Excel行顺序不同时也按名称填写。SKU分类默认精确选择“单品”，选项不存在或不唯一时停止并记录日志。

启动前校验所有SKU：名称唯一、价格大于0且最多两位小数（不自动四舍五入）、数量为非负整数、商家编码为非空文本（保留前导零）。页面与Excel必须一一匹配，缺失、重复或无法完整读取时停止；所有单元格的数量与字符上限在填写前检查。

文本框逐项填写并用Tab失焦回读；数值允许平台补末尾小数零，商家编码按完整文本核对。分类核对选中标签及title。每行日志记录规格、价格、数量、编码、分类，最后记录完成项数。入口仍为 `--product-info-file`，源码 `product_sku.py` 的 `fill_sku_details`；继续使用全局人工验证守卫。

### 一口价

SKU明细后读取Excel基础数据的“一口价”列，覆盖页面同名输入框；已有值也先由Playwright的fill清除并替换为Excel值。空值、非数值、非正数、超过两位有效小数在浏览器启动前报错，不自动舍入。

填写后用Tab失焦，允许平台补末尾小数零，并核对完整金额及平台错误。日志记录原值、Excel金额、页面金额。源码：`src/ecommerce/platforms/qianniu/product_basic.py` 的 `fill_fixed_price`；继续使用全局人工验证守卫。

### 商品级商家编码

一口价后读取Excel基础信息第1行“商家编码”表头对应的第2行值，填写页面 `#sell-field-outerId`。SKU商家编码仍各自读取销售规格表中的对应列。两处字段独立映射，基础信息商家编码缺失或为空时启动前报错。

固定格式使用“商家编码”表头，不需要“货号”列，发布流程不读取或填写货号，也不产生货号缺列警告。文本保留前导零，填写前检查64字符上限；已有值不同时替换，已有相同值只核对。失焦后检查完整值、字符计数及平台错误，日志记录原值和实际编码。当前CJ001基础信息商家编码为 `TEST001`，位于 `Sheet1!I2`。

源码：`src/ecommerce/platforms/qianniu/product_basic.py` 的 `fill_merchant_code`；继续使用全局人工验证守卫。

### 启用多件优惠

商品级商家编码之后，勾选“多件优惠”区域的“启用”复选框。使用check确保勾选，重复执行不会取消已有勾选；核对折扣类型和折扣输入框已启用，平台必填错误已消失。此步骤保留页面原有优惠类型和折扣值，只操作启用状态，日志记录当前类型、折扣值。

源码：`src/ecommerce/platforms/qianniu/product_basic.py` 的 `enable_multi_item_discount`；继续使用全局人工验证守卫。

### 上架时间：放入仓库

启用多件优惠后，将“上架时间”单选项设为“放入仓库”。使用check确保选中，核对该项已选、整个区域仅有一个选中项、平台错误已消失。日志记录的是上架方式选择完成；商品尚未提交，不表示商品已经进入仓库。

源码：`src/ecommerce/platforms/qianniu/product_basic.py` 的 `select_warehouse_listing`；继续使用全局人工验证守卫。

### 浏览器窗口显示

可见浏览器以最大化窗口启动，禁用固定视口，让页面随实际窗口尺寸变化。避免页面被固定为1000像素高、实际窗口却更矮，导致底部固定按钮落在窗口外。视口只在浏览器生命周期中配置，无头运行使用1440×1000固定视口；上传步骤不再强制修改页面尺寸。

配置位于 `src/ecommerce/infrastructure/browser/session.py`；调整窗口不会刷新商品页或提交商品。

### 提交宝贝信息及失败人工检查

现有填写命令追加 `--submit`，完成填写并核对“放入仓库”后点击“提交宝贝信息”。每次程序只点击一次，通过提交结果页的成功提示或字段错误判定结果；跳转、按钮消失、素材发布成功都不能单独代表商品提交成功。

点击前检查可见的真实字段错误，并过滤详情图裁剪说明；不把普通提示框的“超出将被裁剪”当作失败。提交前有错误时提示音并等待人工修正，未收到“继续”不会点击；反馈后重新检查，通过才进行首次提交。日志区分“提交前检查未通过（尚未点击提交）”与“提交后检测到错误”。提交后暂停的是程序后续操作，已经发送给千牛的请求仍可能完成；反馈后只读核对，不能因此再次提交。

明确失败或结果未确认时，写ERROR日志并保存截图，有界面模式播放提示音并保持浏览器打开，等待人工反馈，没有自动超时或自动重复提交。请手动检查、修改并完成页面提交，再输入“继续”；程序只读复核结果，仍未确认则继续提醒和等待。无界面或输入通道关闭时返回needs_attention。

商品提交成功后，继续检查结果页上的附加异常，例如“多件优惠创建失败”或“导购标题存在异常”。这类情况保留已成功提交的商品ID，同时写ERROR日志、保存截图、播放提示音并等待人工检查；无需再次提交商品。收到反馈后只读核对，仍显示的提示保留在日志及返回说明中，不将人工反馈当作问题已解决。

页面操作：`src/ecommerce/platforms/qianniu/product_submit.py`；日志和暂停编排：`src/ecommerce/modules/product/service.py` 的 `submit_with_review`；提示音及终端反馈：`src/ecommerce/cli/interaction.py` 的 `prompt_submission_review`。提交操作也经过全局人工验证守卫。
