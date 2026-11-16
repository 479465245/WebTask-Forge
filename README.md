# WebTask Forge（网页自动化工坊）

基于 FastAPI、Playwright 和 PostgreSQL 的自托管网页自动化工作台。Linux 上运行单个持久 Chromium 浏览器进程，各任务实例使用独立标签页并共享该浏览器上下文中的 Cookie/localStorage。每个任务只允许一个尚未结束的实例；不同任务可并行。运行详情页通过 WebSocket 显示实时画面，默认只读，解锁后可操作。

## 能力

- 任务定义、CRON 定时、手动启动、启动数据和配置导入/导出。
- 流程节点：人工操作、人工数据输入、条件判断、延时等待、URL 跳转和结束节点。
- 输入映射支持固定值、实例全局数据、上一执行节点输出和启动数据；支持点路径嵌套对象。节点输出可映射回实例全局数据。
- 条件脚本按顺序在隔离 Python 子进程执行，只接受严格布尔值；单次运行有最大节点执行次数。
- 节点逐次记录输入、输出、日志、状态、错误和耗时。任务实例绑定启动时的编排版本。
- 按运行实例监听并保存页面 HTTP/HTTPS 请求、响应和请求失败；运行详情页增量查看记录。
- 人工解锁操作时可开启录制；每次点击、滚动、按键和页面跳转保存操作详情及操作后截图，锁定时命名保存，并将录制时段内的脱敏网络请求一并归档。
- 首页可查看录制列表及操作截图、网络请求详情；未命名草稿在连接中断后仍可继续保存或删除。
- 人工操作/输入暂停流程并保留标签页。成功实例自动关闭标签页；出错或取消后保留供检查。
- 服务重启时，正在执行及等待人工的实例标记为“服务重启”。

## 本地启动

需要 Python 3.10+、PostgreSQL 14+。Windows PowerShell：

    $env:DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5433/automation'
    .\scripts\init.ps1
    .\scripts\restart.ps1

Debian/Ubuntu Linux：

    chmod +x scripts/*.sh
    export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5433/automation'
    ./scripts/init.sh
    ./scripts/restart.sh

初始化脚本创建项目虚拟环境、安装 Python 依赖和 Playwright Chromium。Linux 脚本还会通过 apt 安装 Playwright 所需 Chromium 系统依赖及 Tesseract OCR 英文、简体中文语言包；安装系统依赖时会请求 sudo 权限。Windows 需单独安装 Tesseract OCR 引擎及语言包。

Debian/Ubuntu 安装 OCR 引擎和英文、简体中文语言包：`sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim`。Windows 请安装 [Tesseract for Windows (UB Mannheim)](https://github.com/UB-Mannheim/tesseract/wiki)，在安装器中选择 English 和 Chinese (Simplified) 语言数据，并将安装目录加入 `PATH`。若不希望修改 `PATH`，可设置 `TESSERACT_CMD` 为 `tesseract` 可执行文件的完整路径；例如 PowerShell 当前会话可运行 `$env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'`。Linux 也可用此变量指定非标准安装路径。只识别英文时可省略中文语言包。

安装后运行 `tesseract --version` 和 `tesseract --list-langs`，确认引擎可启动且列表包含 `eng`、`chi_sim`。Python 依赖仍通过 `pip install -r requirements.txt` 安装；其中 `pytesseract` 只是 Python 调用封装，不会安装 Tesseract 可执行文件或语言数据。

应用通过 `DATABASE_URL` 环境变量读取数据库连接地址；本地 PostgreSQL 18 Docker 容器使用本机 5433 端口。应用默认监听 127.0.0.1:8765，也可设置 BROWSER_DEMO_HOST、BROWSER_DEMO_PORT、BROWSER_DATA_DIR、BROWSER_HEADLESS 调整运行配置。restart.ps1 / restart.sh 在没有已运行 systemd 服务时会在后台启动本地应用；stop.ps1 / stop.sh 停止应用。日志写入项目目录中的 app.stdout.log、app.stderr.log（Windows）或 app.log（Linux）。Linux 若以 systemd 运行，会优先通过 systemd 重启或停止服务。

首次启动会在 `DATABASE_URL` 指定的 PostgreSQL 数据库中创建表；未设置该环境变量时应用会提示配置错误，且不会回退到 SQLite。局域网部署设置 BROWSER_DEMO_HOST=0.0.0.0，并配置强随机 BROWSER_DEMO_USERNAME 和 BROWSER_DEMO_PASSWORD。应用使用 HTTP Basic Auth；HTTPS 由可信反向代理提供并转发 WebSocket Upgrade。防火墙应限制访问来源。

## Linux systemd 部署

将项目放至 /opt/automation-workbench，创建专用用户和可写数据目录，然后创建 venv 并安装 requirements.txt。安装 Playwright Chromium 系统依赖和浏览器。将 deploy/web-browser-demo.service 安装为 systemd unit，并按实际目录调整 EnvironmentFile、WorkingDirectory、ExecStart 和 StateDirectory。

服务环境文件至少设置 DATABASE_URL、BROWSER_DEMO_HOST、BROWSER_DEMO_PORT、BROWSER_DEMO_USERNAME、BROWSER_DEMO_PASSWORD、BROWSER_DATA_DIR 和 BROWSER_HEADLESS=true。DATABASE_URL 应包含 PostgreSQL 连接地址及凭据。之后执行 systemctl daemon-reload、systemctl enable --now automation-workbench。

## 输入映射

输入映射是数组，每项包含 target 和 source。输入来源 `source.kind` 可为 `fixed`、`global`、`previous`、`start`，分别表示固定值、实例全局数据、上一步节点结果和启动数据。示例：
[{"target":"project.id","source":{"kind":"global","path":"project.id"}},{"target":"url","source":{"kind":"fixed","value":"https://example.com"}}]

输出映射也是数组，每项包含 source 和 target，将所选来源写入实例全局数据。输出来源 `source.kind` 可为 `output`（当前节点输出）、`global`（实例全局数据）、`previous`（上一步节点结果）或 `fixed`（固定值）。路径来源使用 `source.path`，固定值使用 `source.value`。例如将当前节点输出中的 `id` 写入 `record_id`：
[{"source":{"kind":"output","path":"result"},"target":"is_login"}]

为兼容旧任务，输出映射仍接受字符串 source，例如 `{"source":"id","target":"record_id"}`，其含义等同于当前节点输出的 `id` 字段。字段路径支持对象点路径及数组索引，例如 `items.0.id`。

## API

HTTP API 使用 Basic Auth。主要路径：

- GET/POST /api/tasks，PUT/DELETE /api/tasks/{id}：任务定义。
- POST /api/tasks/{id}/runs：创建实例，可提交 {"data":{...}} 启动数据。
- GET /api/runs，GET /api/runs/{id}，DELETE /api/runs/{id}：运行及节点记录；删除运行记录会停止活动实例、清理节点和网络数据及截图，并关闭对应浏览器标签页；共享浏览器上下文中的 Cookie/localStorage 会保留。详情接口可用 include_nodes=false 仅读取运行信息。
- GET /api/runs/{id}/nodes：节点执行记录，支持 page 和 page_size，默认每页 10 条，按开始时间倒序。
- GET /api/recordings：录制列表；GET /api/recordings/{id}：录制操作、截图和网络事件；POST /api/recordings/{id}/save：命名保存草稿；DELETE /api/recordings/{id}：删除录制及截图。
- GET /api/runs/{id}/network：网络事件默认支持 page 和 page_size，默认每页 10 条，按开始时间倒序；传入 after_id 和 limit 时保留增量读取模式。
- POST /api/runs/{id}/human-operation，POST /api/runs/{id}/human-input：推进人工节点。
- POST /api/runs/{id}/cancel，POST /api/runs/{id}/rerun：取消和重新发起。
- GET /api/tasks/{id}/export，POST /api/tasks/import：任务配置导入/导出。

## 边界

- Cookie/localStorage 在共享浏览器上下文中共享；sessionStorage 按标签页隔离。
- 不设全局并行上限；同一任务有等待人工的实例时，再次触发直接跳过，不进入队列。
- HTTP Basic 为单一共享账号，不含多用户/角色/操作人审计。
- 浏览器会话由单个服务进程管理，不支持多 worker 协调。节点外部副作用无法自动回滚。
- CRON 使用服务端本地时区，停机期间不补触发。后续 schema 变更应安排生产迁移。
- 实时画面断线不终止任务；浏览器异常退出后的自动恢复和故障归因需在 Linux 部署环境验证。


关键节点执行后会自动保存一张 JPEG 画面，运行详情可查看。默认保存在 BROWSER_DATA_DIR 所在目录的 artifacts 子目录；可用 ARTIFACTS_DIR 单独指定。请将截图目录纳入受限权限的持久化存储和备份策略。

## 许可证

本项目源码采用 GNU Affero General Public License v3.0（AGPL-3.0-only），完整条款见 [LICENSE](LICENSE)。第三方依赖遵循各自的许可证。

## 请作者喝杯咖啡

如果这个项目对你有帮助，欢迎用支付宝扫码请作者喝杯咖啡。感谢支持！

<p align="center">
  <img src="docs/images/alipay-donation.jpg" alt="支付宝赞赏二维码，请作者喝杯咖啡" width="320">
</p>
