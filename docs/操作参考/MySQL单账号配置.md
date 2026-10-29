# MySQL单账号配置

2026-10-29用户确认：本项目使用一套MySQL账号和一份本地明文密码，日常采集、查询、后台任务和数据库升级全部共用。本机使用已有root管理员；此次没有创建、删除MySQL账号或修改授权，也没有执行新的商品采集。

## 配置位置

在`config/settings.toml`中填写，以下仅为占位示例：

```toml
[mysql]
host = "127.0.0.1"
port = 3306
database = "taobao_automation"
user = "root"
password = "请在本机填写管理员密码"
connect_timeout = 5
```

本地settings.toml已被Git忽略，示例文件只保留空密码占位；不要把实际密码加入示例或任务参数。管理员的权限范围也适用于运行程序。MySQL仍仅允许本机连接地址，不因此开放远程访问。

## 模板与实际配置

settings.example.toml现为MySQL正式库部署模板，显式使用task_backend=mysql、collection_backend=mysql、database=taobao_automation和root单账号；淘宝采用Chrome/native_cdp，新机器需安装Chrome。模板与本机实际配置采用相同的32个已启用配置项、分组和顺序，当前有效参数一致，仅实际配置中的密码不同。文件顶部区分模板与实际配置的用途；路径和环境变量兼容项作为注释示例保留。

首次部署在项目根目录CMD执行，实际配置已存在时跳过复制：

```cmd
if not exist "config\settings.toml" copy "config\settings.example.toml" "config\settings.toml"
```

复制后只在本机填写密码及调整本机值；后续更新项目逐项补充，不覆盖整个settings.toml。文件缺失时，代码兼容默认值仍为sqlite/files、Playwright/Chromium，MySQL缺省库名和账号仍分别为taobao_automation_verify、taobao_app；实际连接以配置中显式填写的值为准。新环境按本模板部署，不依赖这些兼容缺省值。

模板逐项标明用途、缺省行为、可选值和单位：页面及模块超时为毫秒，连接超时和随机间隔为秒，端口为整数，文件路径相对项目根目录。新增、删除或调整配置项时，同步维护代码读取规则、模板和使用说明，注明是否需要重启。

## 读取规则

1. 配置文件存在password项时优先使用它；显式空字符串表示尚未填写，连接会停止，不会改用旧环境变量。
2. 没有password项时，兼容password_env指定的单一环境变量，默认ECOMMERCE_MYSQL_PASSWORD。
3. 原migration_user、migration_password_env只为旧配置/旧任务快照兼容而忽略，不再产生另一套连接凭据。
4. user支持root，普通连接与升级连接使用同一user和密码。修改账号、地址或密码后重启当前终端/Web服务，以更新连接池。

密码没有默认值，也不在代码或文档中填写。配置格式错误、文件不可读、账号变化等异常均返回不包含文件内容的说明；SQL参数继续隐藏，配置对象展示不包含密码。

## 后台任务与历史申请

加载本地配置后，数据库配置对象只保留凭据文件引用，建立连接时才读取密码。collection_jobs.payload.config和ECOMMERCE_JOB_BOOTSTRAP仅保存host、port、database、user、password_env、connect_timeout及credentials_file，password值不写入任务数据库、派发参数或子进程引导JSON。

credentials_file是内部快照字段，表示配置文件绝对路径；不应手工加入TOML。后台执行先用当前本地配置连接，再核对旧申请的主机、端口和库名；只有同一数据库目标才能绑定当前凭据，工作空间和执行资源仍按原规则检查。旧申请中的程序账号不强制继续使用，因此切换管理员或更新凭据后仍能读取原有业务申请；其他数据库申请不能被串用。

原有两个专用账号和Windows环境变量没有删除。它们不再作为第二套升级凭据；已存在的采集资料、申请记录、表结构和冻结迁移不因这次配置改造而重写。

## 使用与迁移

在项目目录中新开终端执行：

```cmd
.venv\Scripts\python.exe main.py database check
.venv\Scripts\python.exe main.py tasks
```

结构升级需要先正常结束采集任务，然后执行：

```cmd
.venv\Scripts\python.exe main.py database upgrade
```

修改任一配置后，先正常结束当前采集，再重新运行CLI任务或重启Web服务，以加载新配置并更新连接池；已提交采集申请仍保留固定业务参数。随后执行上面的database check，只读核对连接、版本、表结构和备注；该命令不建表、不升级、不采集。

换电脑时，新机器仍需安装MySQL、创建或准备管理员账号并恢复业务库与原件；复制项目文件不会自动创建数据库账号。可以在新机器使用新管理员密码，更新本地settings.toml即可；后台进程引用以新启动配置为准。

## 独立验证配置

独立合成验证只允许以_verify结尾的数据库。正式模板不再兼作验证配置，先在项目根目录CMD创建一个workspace内的本地副本；已有副本时保留：

```cmd
if not exist "workspace" mkdir "workspace"
if not exist "workspace\settings.verify.toml" copy "config\settings.example.toml" "workspace\settings.verify.toml"
```

仅在该本地副本中把[mysql].database改为taobao_automation_verify，并填写本机管理员密码，其他相对路径仍按项目根目录解析。workspace已被Git忽略，实际密码不能回填到示例模板。验证库尚未建表或未升级时，先正常结束采集，再显式执行以下upgrade；已有当前结构时跳过升级，先check后verify：

```cmd
python main.py --config "workspace/settings.verify.toml" database upgrade
python main.py --config "workspace/settings.verify.toml" database check
python main.py --config "workspace/settings.verify.toml" database verify
```

check只读；verify在独立验证库使用合成数据并回滚，不向正式库写入假数据。空密码会拒绝连接；正式库配置执行verify会被_verify库名保护拒绝。本轮仅整理配置和说明，未创建含密码的验证副本，也未执行合成写入或升级。

## 单账号改造验证记录

以下为2026-10-29上一轮单账号改造的验证快照，不替代本轮配置整理核对。

本机实际连接已确认CURRENT_USER()为root@localhost。离线及验证库回归覆盖文件密码优先、两种连接用途统一账号、显式空值拒绝、环境变量兼容、旧任务绑定当前凭据、错误目标拒绝、子进程引用及明文快照拦截。

验证结果：单元套件558项通过，验证器调整后17项凭据/基础单元用例再次通过；数据库34项通过，包含真实子进程引用、旧申请绑定当前账号和不匹配目标拒绝；管理员实际连接和验证库16项基础检查通过，合成记录回滚；验证库结构升级入口通过且版本保持0010_product_favorites。

Web在原地址http://127.0.0.1:8000完成重启，任务、日志、采集资料及对应API共6个查询入口返回200。重启前后15张核心采集资料表的计数和规范内容哈希一致，活动采集0、待补写流水0。本地配置仍被Git忽略；此次未启动真实淘宝采集，原账号及授权保留。

本机证据目录：workspace/output/reports/mysql_single_account_20261029，包含admin_checks.json、post_checks.json、web_restart.json及测试日志，均不含管理员密码值。关联JSON定义统一见[数据库字段取值说明](../数据库字段取值说明.md)，常用数据库命令见[命令参考](命令参考.md)。
