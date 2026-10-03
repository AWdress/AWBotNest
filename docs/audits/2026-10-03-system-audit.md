# 系统检查报告

检查日期：2026-10-03  
检查版本：v2.0.0.7，分支 `v2`，提交 `8ae8751f12b7e20c871eea686c2d1d8a23f4d072`

本次只检查，未修改业务源码、发布版本或推送。复现使用隔离模拟和测试数据，未连接生产服务器，未读取真实密钥进行验证。

## 后续修复：v2.0.0.8

管理员确认修复后，以下 13 类问题均已处理并补充失败路径回归。本文保留原检查结论，
用于说明修复背景，不代表新版本仍有这些已知缺陷。

- 配置导入加入完整字段校验与恢复事务；后台初始化和 HTTP 监听均成功后才确认导入。
- 插件自动更新绑定确认过的仓库和路径；上传、卸载与自动更新共用安装锁。旧插件可在商店确认来源。
- 配置弹窗统一隔离过期响应；下拉框保存原始类型；敏感列表递归隐藏、按路径读取并防止删行/重排串值。
- 管理员凭据变更同步资源 Cookie；终端、文件和界面日志统一脱敏。
- 首次网络连接失败加入恢复任务及插件重绑；取消免费浏览器席位和浏览器启动失败均清理资源；明确直连参数保留。
- OCR 补齐常用接口，纯滑块不加载文字模型，排队取消和超时覆盖整个等待过程；相同模型的字符范围在客户端之间隔离。
- Docker 无内存上限时仍读取容器自身用量。

最终验证：后端 CI 回归 82 项（其中可选真实 OCR 1 项默认跳过，另显式运行通过），
本地后端回归 129 项、前端 Node 28 项、iPhone 17 回归 20 项通过；前端构建和 OpenAPI
类型同步通过。真实 OCR 子进程识别及空闲退出也验证通过。未进行生产服务器升级或长期负载实测。

## 结论

确认 13 类问题。最优先的是配置导入校验、插件更新来源约束和配置弹窗请求隔离。浏览器取消/启动失败、账号首次连接失败、OCR 兼容与敏感配置也存在现有回归未覆盖的缺陷。

P1 表示应优先处理的数据、安全或启动问题；P2 表示有明确触发条件的功能、资源或安全策略缺陷。不是对所有部署都已发生故障的判断。

## 优先修复

### 1. P1：无效配置能导入成功，重启后启动失败

- 位置：[导入校验](F:/github项目/AWBotNest_v2/awbotnest/backup.py:109)、[应用配置](F:/github项目/AWBotNest_v2/awbotnest/backup.py:239)、[启动顺序](F:/github项目/AWBotNest_v2/awbotnest/main.py:36)。
- 当前只检查 JSON 对象、备份格式和顶层插件配置类型，没有完整检查系统字段。
- 隔离复现：`api_id="not-an-integer"`、`bots=1`、`user_sessions=1` 都通过备份校验，随后 `load_settings()` 抛出 ValueError/TypeError。
- 应用导入会先覆盖正式配置，再删除待恢复文件；之后启动失败不会自动恢复旧配置。旧配置备份仍在，但需要手动恢复。
- 建议：预览和正式导入采用相同的完整字段校验；新配置加载失败时自动回滚。

### 2. P1：自动更新没有绑定插件原始仓库

- 位置：[自动发现](F:/github项目/AWBotNest_v2/awbotnest/market.py:271)、[本地版本匹配](F:/github项目/AWBotNest_v2/awbotnest/market.py:318)、[列表去重](F:/github项目/AWBotNest_v2/awbotnest/market.py:487)、[自动安装](F:/github项目/AWBotNest_v2/awbotnest/market.py:205)。
- 自动发现的公开仓库直接加入配置；相同 ID 的插件按仓库顺序去重。版本匹配依据本地 ID 和单文件/目录形态，不核对原安装仓库。
- 模拟复现：本地第三方 `custom_plugin=1.0`，候选仓库中同 ID、同形态的 `9.0` 排在原仓库之前，系统将其认定为更新并自动安装、重新启用。
- 官方仓库有优先保护；风险主要涉及第三方、手工上传和导入的插件，尤其是原仓库未配置或自动发现发生同名冲突时。没有证据表明当前已经发生恶意替换。
- 建议：记录并绑定安装来源；新增发现仓库与同 ID 来源切换须由管理员确认，不能自动成为已安装插件的更新源。

### 3. P1：切换配置弹窗后，旧响应可能把账号范围写入另一个插件

- 位置：[账号范围保存](F:/github项目/AWBotNest_v2/frontend/src/views/Plugins.vue:537)、[Webhook 加载](F:/github项目/AWBotNest_v2/frontend/src/views/Plugins.vue:488)。
- 保存和部分辅助请求缺少插件 ID/弹窗请求序号检查，响应直接写回共用状态。
- 复现：A 保存 `['alice']` 未完成，关闭 A 后打开 B，B 正确加载 `['bob']`；A 返回后 B 变成 `['alice']`。再为 B 勾选 `charlie`，实际提交 B 的范围是 `['alice', 'charlie']`。
- Webhook 同样能复现：B 已加载 `/b`，旧 A 响应返回后界面变成 `/a`，复制后可能接错插件。
- 建议：所有响应、错误回滚和 finally 状态恢复都绑定请求发起时的插件和弹窗；过期响应不得写回。

## 其他已确认问题

### 4. P2：OCR 接管不完全兼容原库，且取消/纯滑块路径有额外开销

- 位置：[兼容对象](F:/github项目/AWBotNest_v2/awbotnest/services/ocr.py:65)、[参数及模式选择](F:/github项目/AWBotNest_v2/awbotnest/services/ocr.py:129)、[全局替换](F:/github项目/AWBotNest_v2/awbotnest/services/ocr.py:144)、[排队与异步调用](F:/github项目/AWBotNest_v2/awbotnest/services/ocr.py:223)。
- 系统替换了 `import ddddocr`，但 `classification(..., png_fix=True)`、`probability`、`set_ranges()`、`slide_comparison()`、自定义模型等原库接口不完整。复现分别得到 TypeError/AttributeError。基础文字识别插件不因此全部失效；使用这些接口的插件会受影响。
- `DdddOcr(ocr=False, det=False)` 被路由到默认文字模型；原库的纯滑块模式不需要加载文字 ONNX 模型。
- 排队中的异步 OCR 任务取消后，底层线程仍可拿锁并执行。模拟中已取消任务仍被提交；排队等待也不计入识别超时。
- 建议：明确支持的兼容契约，补齐原有常用接口；纯滑块独立路由；排队任务加入取消检查及端到端超时。
- 这些结果不能推导出生产服务器精确内存下降量；真实 Linux 原生模型的内存仍需升级前后实测。

### 5. P2：取消竞态可永久占用 CloakBrowser 免费席位

- 位置：[异步席位获取](F:/github项目/AWBotNest_v2/awbotnest/cloak_proxy.py:280)。
- 初次直接等待 task，取消会连带取消 task；随后再 shield 已无法取得线程返回的 lease。
- 控制复现：线程刚取得席位后延迟返回，取消调用者，再放行线程。最终 `active=True、waiting=0`，实际没有浏览器启动，之后免费会话持续排队。
- 建议：从第一次等待开始保护工作 task；取消时等线程返回并释放已经取得的席位。现有测试只覆盖仍在排队时取消。

### 6. P2：Chromium 启动失败会残留 Playwright 驱动

- 位置：[同步启动](F:/github项目/AWBotNest_v2/awbotnest/services/browser.py:54)、[异步启动](F:/github项目/AWBotNest_v2/awbotnest/services/browser.py:110)。
- 驱动启动和浏览器 launch 在清理区之外；launch 抛错或被取消时不会执行 driver.stop。
- 同步/异步模拟均得到 `stop_calls=0`。本地真实 Playwright 使用不存在的浏览器 executable，抛错后仍有 Node 驱动子进程；手动 stop 后消失。
- 建议：将初始化也纳入 finally，浏览器未创建时同样清理驱动。成功路径已有清理，不是所有浏览器调用都泄漏。

### 7. P2：启动时断网的账号不会在网络恢复后自动上线

- 位置：[首次连接](F:/github项目/AWBotNest_v2/awbotnest/telegram.py:181)、[失败清理](F:/github项目/AWBotNest_v2/awbotnest/telegram.py:197)、[启动入口](F:/github项目/AWBotNest_v2/awbotnest/main.py:76)。
- 首次连接 60 秒超时后客户端被断开，未加入账号集合，也没有恢复任务。Bot 启动失败有相同缺口。
- 模拟首次 connect 超时，再恢复网络后，连接尝试仍为 1 次、users 为空、没有后台恢复任务。
- 当前无限自动重连只保护已成功连接并保留的客户端。没有证据支持“用户离线必然导致 Bot 通知失败”；通知已有独立 Bot API 路径。
- 建议：对网络类首次连接失败启动可取消的重试；恢复后补齐插件绑定。凭据或 session 失效应区别处理，不能无限盲重试。

### 8. P2：CloakBrowser 会忽略明确直连参数

- 位置：[代理解析和传递](F:/github项目/AWBotNest_v2/awbotnest/services/browser.py:85)、[代理继承](F:/github项目/AWBotNest_v2/awbotnest/cloak_proxy.py:261)。
- `ctx.browser.run(..., proxy=False)` 被解析成空值并省略 launch 参数；Cloak 包装又将其当作未指定而继承系统代理。
- 模拟系统代理为 `http://system-proxy.test:8080`，请求明确直连，实际 launch 仍收到系统代理。Chromium 路径没有这个重新继承行为。
- 建议：区分“未指定”和“明确直连”，后者显式传递直连标记。

### 9. P2：数字/布尔下拉选项保存后改变类型，重开会回到第一项

- 位置：[select 保存](F:/github项目/AWBotNest_v2/frontend/src/components/FieldInput.vue:258)、[挂载校验](F:/github项目/AWBotNest_v2/frontend/src/components/FieldInput.vue:61)。
- DOM change 保存字符串，挂载时却严格比较选项原始类型。
- 复现选项 `[1, 2]`，选第二项得到 `"2"`，重开后触发 `update=1`。布尔选项同样得到字符串，可能导致插件判断或后端校验错误。
- 建议：根据选项索引/原始值保存，不直接使用 DOM 字符串。

### 10. P2：修改管理员密码后，Vue 插件资源暂时无法加载

- 位置：[修改凭据](F:/github项目/AWBotNest_v2/awbotnest/api/system.py:179)、[前端令牌更新](F:/github项目/AWBotNest_v2/frontend/src/api/index.js:86)、[插件资源授权](F:/github项目/AWBotNest_v2/awbotnest/api/plugins.py:343)。
- 密码修改旋转管理员令牌，前端更新 Bearer，但资源授权 Cookie 没更新。
- ASGI 模拟：登录 200 → 修改密码 200 → 新 Bearer 读设置 200 → 旧资源 Cookie 加载 remoteEntry.js 403。刷新页面重新补种 Cookie 或重新登录可恢复。
- 建议：凭据/令牌变更时同步刷新资源 Cookie。

### 11. P2：敏感日志只有管理界面脱敏，文件和终端仍可能明文

- 位置：[结构化脱敏](F:/github项目/AWBotNest_v2/awbotnest/logs.py:112)、[文件 handler](F:/github项目/AWBotNest_v2/awbotnest/logs.py:174)。
- 脱敏只改变 MemoryLogHandler 的局部 message，不改变文件及终端的 formatter 输出。
- 同一模拟日志在结构化记录中为 `token=*** password=***`，文件 formatter 中仍为模拟 token/password 原值。
- 触发前提是插件或错误信息输出了敏感值；不是说系统默认把所有密钥写入日志。
- 建议：在各输出通道统一脱敏，包含异常栈。测试记录和异常，避免只验证 WebUI。

### 12. P2：列表内敏感配置未递归掩码，按需读取接口也不支持嵌套路径

- 位置：[配置掩码](F:/github项目/AWBotNest_v2/awbotnest/api/plugins.py:311)、[字段读取](F:/github项目/AWBotNest_v2/awbotnest/api/plugins.py:330)。
- Schema 支持列表内子字段，但 API 只检查顶层字段。
- 以 B站/贴吧插件的 `accounts.fields.cookie` 声明复现：GET 默认返回列表内模拟 Cookie 明文，reveal `accounts.0.cookie` 返回 400。
- 接口仍要求管理员鉴权；这是按需暴露策略和接口契约不完整，不是未授权用户可以读 Cookie。
- 建议：递归处理 Schema 与值；按路径 reveal，并在保存时递归保留未修改的掩码值。NodeSeek 自定义 Vue 已主动读取真实列表，本次未证实其账号列表丢失，不计入问题。

### 13. P2：不限内存的容器，状态页统计可能显示整机内存

- 位置：[cgroup 读取](F:/github项目/AWBotNest_v2/awbotnest/resources.py:39)、[统计回退](F:/github项目/AWBotNest_v2/awbotnest/resources.py:59)。
- `memory.max=max` 时丢弃整个 cgroup 结果，回退到整机 virtual_memory。容器限额等于/大于主机总量时也未采用容器用量。
- 模拟容器实际使用 256 MiB、无上限，主机使用 8 GiB/总量 16 GiB，snapshot 返回 8192 MiB，而不是容器用量。
- 建议：容器用量与总容量分别取值，并在界面明确统计口径。
- 此问题不能解释此前 docker stats 与 Python RSS 确认的约 1.17 GiB 实际占用，那是另外的真实内存问题。

## 验证与限制

- `quality_tests`：43 项通过。
- 本地 `tests`：129 项通过；该目录不属于当前仓库 CI 的后端测试入口。
- 前端 Node 回归：12 项通过。
- iPhone 17 Playwright 回归：17 项通过。
- 前端生产构建成功；OpenAPI 类型生成后没有差异。
- 前端机械审计检测未报告问题，但它不能覆盖异步竞态、类型和后端协议缺陷，也不等同于完整视觉/无障碍审查。
- OCR 现有回归主要使用模拟 executor/模拟 ddddocr，不能证明所有真实原库接口都兼容。
- 以上功能缺陷分别有代码路径与隔离复现；未进行生产服务器长时间负载、真实 Telegram 登录、外部 AI 调用、Linux 原生 OCR 内存测量或真实 iPhone 系统键盘测试。

建议顺序：先处理第 1–3 项，再处理浏览器/账号恢复和 OCR，最后补齐配置安全、类型及统计口径。每项修复应加入对应失败路径回归，不能只依赖当前全绿结果。
