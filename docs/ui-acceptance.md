# 全站主题与初始化：本地验收记录

## 固定用户名、多邮箱与唯一超管（2026-10-16）

登录支持用户名或任一已验证绑定邮箱；用户名及唯一超管 `admin` 身份固定。邮箱管理支持验证后添加、切换默认和解绑，普通账户保留最后一个邮箱，公开主页只展示用户选择公开的默认邮箱。普通注册、第三方首次注册和初始化邮箱均先验证，再创建账户；无邮箱 `admin` 初始化与登录可用。

`core.0006_account_identity` 保留已有账户 ID、密码与业务关联，冲突预检整体回滚；SQLite 与 PostgreSQL 均测试旧数据归一化、重复地址合并、冲突停止，以及数据库唯一性和不可修改约束。PostgreSQL 强制两个确认同时插入同一邮箱，结果仅一个成功；并发切换默认、解绑和确认保持默认地址与兼容字段一致。

本地 PostgreSQL 应用回归 286 项通过（两项真实 ScanCode 专项未重复运行），包含密码修改与默认邮箱并发保护。浏览器回归 31 项通过；中英文、390/768/1440 布局和关键按钮可见性通过。运维脚本 43 项通过；Django check、迁移一致性、Ruff 与 diff 检查通过。

独立 `njtech-identity` DEBUG=0 栈使用 Gunicorn、WhiteNoise、PostgreSQL 17、Redis 8、Celery、真实本地 STARTTLS Mailpit。浏览器完成无邮箱 admin 初始化、真实公开 Git 政策同步、普通注册、跨设备邮件确认、多邮箱添加与切换/解绑、用户名及所有绑定邮箱登录。每个确认前后读取真实数据库，证实 GET 不绑定、验证前不创建账户和邮箱。容器验收两项通过，结果与截图在忽略目录 `var/identity/artifacts/`。

本地预览 <http://localhost:18005/>，收信箱 <http://localhost:18030/>。隔离测试账户 `admin` 与 `identity-user`，密码均为 `Local-preview-only-acceptance-527!`，仅本地使用。Cloudflare 服务端使用公开测试密钥和测试令牌，严格校验的规范域名为测试服务返回的 `example.com`；邮件链接在人工验收时将域名改为 `localhost:18005` 即可。应用未加入测试绕过。正式 OAuth 与公网 SMTP 投递本轮未验证。构建因本地 HTTP 软件源 502，使用仓库 Dockerfile 的本地 HTTPS 软件源副本完成，依赖版本与应用入口相同。

本轮在用户明确授权后将完整工作区一并提交到 main 并用 `dev-*` 标签触发 CI，不创建远程分支、不部署服务器；远端结果以实际 CI 运行记录为准。下文为先前批次记录，旧测试数量和行为不代表本轮。

## 同步恢复与轮询竞态修复（2026-10-16，前一批）

政策与许可证同步在已有 SyncRun 上增加执行令牌、租约到期时间，迁移为 `core.0004_syncrun_execution_lease`；不新增表。租约有效期 120 秒、每 30 秒续期，发布内容与终态前检查执行权和来源版本。worker 丢失后的重投等待租约到期再接管同一记录；执行权等待独立于最多四次业务重试。正常重复投递、旧执行者和迟到消息不能重复发布。定时同步及 Webhook 在入队前创建记录；未改变其他任务的队列等待时间。

初始化和运行时配置提供“重新尝试”，校验权限、最新任务、来源和租约后恢复原记录；重复请求只重新排队一次，提交失败可继续重试。只读状态区分执行中、等待执行权失效和可以恢复，不包含执行令牌。历史 running 记录缺少有效租约时可恢复。

向导导航、提交、输入和隐藏页面会使旧轮询失效。AbortController 只取消轮询 GET，操作序号、页面节点和 URL 校验共同拦截迟到响应及重定向，不取消或重发 POST。真实浏览器测试刻意忽略取消信号、延迟状态和片段响应，验证前进、返回相同 URL、提交、输入、隐藏页面和过期跳转不会覆盖新页面。

故障验收使用独立 `njtech-sync-recovery` 本地栈：真实 PostgreSQL、Redis、Celery prefork worker，观察实际 30 秒续期，SIGKILL 两类任务的执行进程后等待实际 120 秒租约失效，自动重投成功；停止整个 worker 后从 GUI 恢复两类原记录，补投旧消息不增加执行次数。外部 Git 获取和 ScanCode 编译使用明确的测试替身，此项只证明任务生命周期与恢复，不代表真实 Git/许可证识别验收。

| 验证 | 最终结果 |
|---|---|
| 应用与浏览器全套 | 234 passed，8 skipped；跳过项为两项 ScanCode、四项 PostgreSQL 锁测试、两项显式启用的容器测试。 |
| PostgreSQL 应用回归 | 215 passed，2 skipped，25 deselected；两项原有行锁测试、两项新增领取/恢复竞争测试均通过。 |
| 新增恢复与轮询浏览器场景 | 9 passed；另包含在全套结果中。 |
| 最终 DEBUG=0 镜像故障闭环 | 1 passed，307.08 秒；双任务自动接管、GUI 恢复、迟到消息去重通过。 |
| 运维与静态检查 | unittest 42 项通过；Ruff、git diff --check、Django check、makemigrations --check 通过。 |

一次 PostgreSQL 回归中，现有 OAuth 限流测试的第 11 次请求返回 302 而非预期 403；单项复跑及全量复跑均通过，未修改该功能或测试。首次失败和复跑日志分别保留在 `postgres-first-retry.log`、`rate-limit-recheck.log` 和 `postgres-tests.log`，未据此断言失败原因。

日志和结果位于忽略目录 `test-results/sync-recovery/`；`container-recovery-result.json` 记录同一任务 ID、执行次数与外部替身边界，`sync-recovery-390.png`、`sync-recovery-1440.png`、`container-recovery-documents.png`、`container-recovery-licenses.png` 为恢复界面截图。隔离故障栈在验收后清理，不保留后台任务。

本地人工预览仍为 <http://localhost:18002/setup/>，引导密钥仍是仅本地使用的 `local-development-bootstrap`。已应用增量迁移，保留原有账号及配置；更新前快照在忽略目录 `var/sync-recovery/preview-before-lease.dump`。beat 保持停止，没有恢复定时监测。服务器数据、远端分支和部署均未修改。

后续更新必须先停止旧同步消费者，再迁移并启动新版 worker/scanner，详见 [发布说明](release-acceptance.md)。下文保留前一批记录，旧测试数量和“无新增迁移”描述不适用于本轮租约修复。

## 聚焦式初始化与统一邮件（2026-10-16，前一批）

六个阶段内每次只显示一个任务；已完成任务折叠为可修改摘要。SMTP 配置与独立测试在管理员创建之前，测试收件邮箱默认跟随发件邮箱；失败保留配置并给出可处理的错误。SSL/TLS、STARTTLS 使用可视选择，切换不覆盖自定义端口。仓库 `.git`、许可证目录、R2 Endpoint/Bucket/Region、平台回调及运维说明同时用于初始化和运行时配置。

增强表单立即显示忙碌状态，防止重复点击；响应丢失时先读取状态，不自动重发。等待同步与邮箱确认每五秒只读查询，隐藏页面暂停。无 JavaScript 的普通表单仍可完成配置。同步沿用 SyncRun，提交、执行、重试和最终状态使用同一记录，来源变化使旧请求失效；未新增数据库表或迁移。

验证、密码重置、账号通知、SMTP 测试与运维通知共用 `templates/emails/message.html`，保留纯文本。HTML 使用内联样式、转义动态内容、统一按钮和备用链接；确认与密码重置文案已去掉默认的中英文混排和重复链接。

本轮验证：

| 验证 | 结果 |
|---|---|
| 全套本地应用与浏览器测试 | 204 passed，5 skipped；跳过两项真实 ScanCode、两项仅 PostgreSQL 的行锁测试及需显式启用的容器测试。 |
| PostgreSQL 应用回归 | 192 passed，2 skipped，15 deselected；两项行锁通过，ScanCode 跳过，浏览器测试单独执行。 |
| 运维脚本单元测试 | unittest 42 项通过。 |
| 浏览器体验 | 390 / 768 / 1440，浅色 / 深色 / 系统；字段、错误、忙碌反馈、断线恢复、可选项跳过、无 JS、跨设备确认与完整激活通过。截图在 `test-results/setup-experience/`。 |
| DEBUG=0 独立容器 | Gunicorn / WhiteNoise / PostgreSQL / Redis / Celery，真实本地 STARTTLS Mailpit、跨浏览器确认、公开 Git 政策同步、激活和静态资源通过；结果在 `container-result.json`。 |
| 静态检查 | Ruff、git diff --check、Django check、无新增迁移通过；沿用锁定依赖。 |

一次容器复验中，公开 Git 获取第一次失败，Celery 保留同一请求记录自动重试后成功，总计约 145 秒。容器验收脚本对这项外部同步最多等待四分钟，页面操作和 SMTP 的超时没有放宽。最终完整容器闭环 1 passed，耗时 29.70 秒；SMTP 测试点击到下一任务约 0.305 秒，仅代表本地 Mailpit。本轮没有重复运行与本次 UI 改动无关的加密备份/恢复容器专项，其历史结果见下文。

当前新预览为 <http://localhost:18002/setup/>，测试收件箱为 <http://localhost:18029/>，属于独立 `njtech-setup-experience` 项目。引导密钥是 **仅本地使用** 的 `local-development-bootstrap`。可填写 SMTP `mailpit`、端口 `1025`、连接后升级加密（STARTTLS）、发件邮箱 `preview@example.invalid`，账号和密码留空。容器验收创建的本地测试账号和配置在测试后恢复到该项目最初的空白快照，便于重新走向导；旧预览和服务器数据库未改动。预览保留 worker 处理手动同步，beat 已停止。

邮件预览为 `test-results/setup-experience/email-preview.html`，另有 `email-preview-390.png` / `email-preview-1440.png`；页面截图包含 `container-smtp-desktop.png`、`focused-smtp-light-390.png`、`focused-storage-dark-390.png` 和 `container-review-*.png`。邮件中的账号与链接属于本地测试数据；恢复空白快照后确认链接失效是预期行为。

公网邮箱实际到达及邮件客户端显示本轮未验证；真实 Turnstile、OAuth、头像 S3 与外部备份仍需部署方凭据。服务器 `10.126.126.4` 的地址修正列入 [预发布待办](release-acceptance.md#当前预发布地址变更待办2026-10-15)，本轮没有 commit、push、部署、服务器清库或恢复定时监测。下文是前一批界面重构的历史记录，其测试数量与旧预览地址不代表本轮结果。

停止新预览并保留数据：

```sh
docker compose -p njtech-setup-experience -f compose.yaml -f deploy/compose.local.yaml -f var/ui-preview/compose.json -f var/setup-experience/compose.yaml down
```

## 前一批本地验收记录

2026-10-15。以下为提交前的本地验收记录，不代表后续远端 CI 或服务器部署验收。原有运营者信息与政策变量改动保留，并纳入同一政策配置分组。

## 实施结果

- Django Templates 保持不变，沿用 Tabler 1.4.0、Unfold 0.110.0；新增锁定依赖 django-widget-tweaks 1.5.1。公开页使用顶部导航，账号工作区和后台使用侧栏，运行时配置保留原 URL 与超管权限。
- 三种主题共用 Unfold 的 `adminTheme` 存储，首屏应用、系统变化和跨页面恢复均已验证。公告、Markdown、编辑器和验证码随主题切换；手机与平板导航支持键盘展开。
- 未激活时以 `SiteState.activated_at` 为唯一门槛，普通 GET/POST、后台、账号和未知路径均返回 302 + no-store，原业务不执行。只放行初始化、静态、语言切换和初始账号的有效邮箱确认。缺失密钥显示 503；激活后初始化为禁止缓存的 404。
- 六步向导按操作保存配置组，支持返回、刷新、重新授权恢复、邮件重发与可选项跳过。密钥不回显、留空保留；激活重新校验邮箱、SMTP、Turnstile 和政策条件。没有新增数据库迁移或测试专用入口绕过。
- 同步请求、提交失败和实际任务结果分别保存在已有 SyncRun。外部备份配置状态由维护容器上报，缓存三分钟；未知状态明确显示，状态上报失败不阻止独立备份。

## 已执行验证

| 验证 | 结果 |
|---|---|
| 应用、运维和浏览器完整测试（不含真实 ScanCode 专项） | 225 passed，4 skipped，40 subtests passed。其中 SQLite 跳过的行锁路径另用 PostgreSQL 验证。 |
| PostgreSQL 17 应用测试 | 177 passed，2 skipped，11 deselected；真实行锁测试通过，跳过项为未安装的 ScanCode 模块。 |
| 最后修正的确认状态与深色链接 | 13 项初始化/浏览器测试通过；另有 1 项公开页面浏览器测试通过，并检查实际渲染链接对比度至少 4.5。 |
| 浏览器覆盖 | 390、768、1440；浅色、深色、系统；导航、表单错误、语言切换、目录分页、个人中心、平台解绑、公告确认、原生后台和 Markdown 编辑器。 |
| DEBUG=0 初始化 | 真实 Gunicorn/WhiteNoise、PostgreSQL、Redis、Celery；Mailpit STARTTLS 投递、跨设备 allauth 确认、真实 Git 政策同步、跳过可选项并激活到登录页通过。政策 SHA：`b930a007702474529700cd5a3b3300ed7d60c214`。 |
| DEBUG=0 容器冒烟，两种备份场景 | 初始化入口拦截、静态 MIME/gzip/HEAD/304/404、桌面/手机、编辑器、Mailpit、日志脱敏通过；未配置时不执行备份，完整配置时全量/差异加密备份、故障保留 WAL、隔离恢复通过。恢复校验 65 个迁移及加密配置，临时项目已清理。 |
| 构建和静态检查 | uv 锁校验、npm 静态构建、Ruff、Django check、无新增迁移、Compose 解析、git diff --check 通过。 |

日志和截图位于工作区忽略目录 `test-results/ui/`，不进入应用镜像。真实 ScanCode 专项本次未重复运行；之前的独立记录仍见 implementation-status.md。

## 外部服务边界

自动化注册、密码登录和向导回归使用明确的邮件/验证码/Git 测试替身。容器中的初始邮箱使用真实本地 STARTTLS Mailpit，政策同步实际读取公开 Git 仓库；这不证明公网 SMTP 投递。

Cloudflare 真实前端脚本与公开测试密钥能够渲染三种主题并生成测试令牌。服务端实测返回 `success=true, hostname=example.com`，与本地 `localhost` 不符，被现有严格域名检查拒绝；没有放宽生产校验。公开测试密钥见 [Cloudflare 官方测试说明](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)。真实站点 Turnstile、OAuth、头像 S3、异地备份 S3 仍需部署方凭据验证。容器备份测试使用隔离 POSIX 仓库。

## 人工预览

- 已激活主站：<http://localhost:18000/>；Mailpit：<http://localhost:18025/>。仓库列表为明确标注的本地演示数据，不代表真实归属核验或机审放行。
- 独立的首次初始化：<http://localhost:18001/setup/>；Mailpit：<http://localhost:18026/>。引导密钥是本地覆盖文件中的 `local-development-bootstrap`。SMTP 可填 `mailpit`、端口 `1025`、`starttls`、发件人 `preview@example.invalid`，用户名和密码留空；本地证书信任仅配置在验收容器中。
- 后台人工预览使用隔离演示账号的已有会话，运行 `.venv/bin/python var/ui-preview/open_preview.py` 打开独立浏览器，关闭窗口结束。会话文件仅在忽略目录 `var/ui-preview/`，不改变应用认证规则。
- 主要截图：`container-home-light-1440.png`、`container-home-light-390.png`、`container-home-dark-1440.png`、`container-setup-mobile.png`、`container-editor-dark-390.png`；均位于 `test-results/ui/`。

两套预览分别属于 `njtech-ui-preview` 和 `njtech-ui-setup`，仅绑定回环端口。预览数据库中仅启用周期性备份状态报告，其余周期任务暂停，避免演示仓库持续触发真实同步；界面上的手动同步仍可使用。人工验收期间保留；测试数据库容器与 `njtech-ops-smoke` 项目已清理。停止预览但保留数据：

```sh
docker compose -p njtech-ui-preview -f compose.yaml -f deploy/compose.local.yaml -f var/ui-preview/compose.json down
docker compose -p njtech-ui-setup -f compose.yaml -f deploy/compose.local.yaml -f var/ui-preview/compose.json -f var/ui-preview/setup.yaml down
```

上述本地覆盖文件与证书均在忽略目录，不用于正式部署。初始化完成后的 `/setup/` 返回 404 是预期行为；另一套预览用于独立检验首次初始化。

## 2026-10-16：数据库备份与 Logo

初始化第五阶段和运行时配置新增同一套 Logo 上传/应用及数据库备份操作。Logo 的文件校验、透明度、失效缓存、权限和页面即时更新已验证。390、768、1440 宽度 × 浅色、深色、跟随系统的初始化/后台检查通过，截图位于 `test-results/backup-gui/logo-*.png` 和 `logo-admin-*.png`。

本轮全套测试 273 passed / 9 skipped；PostgreSQL 246 passed / 2 skipped / 34 deselected；运维测试 42 passed，Ruff、JS 语法、迁移一致性检查通过。DEBUG=0 的真实容器验收覆盖 Logo GUI、TLS S3 测试/启用/手动备份、全量/差异/WAL、Web 和 Redis 停止后的独立调度与隔离恢复。杀死维护执行进程后 GUI 可识别中断并重试；丢失生效配置时真实 WAL 保留。

完整本地证据见 `test-results/backup-gui/local-acceptance.md`。S3 为开启证书验证的本地 MinIO，Logo 为合成测试图片，不代表用户正式 Logo 或真实存储/邮件验收。已冻结的交付源码摘要以 release manifest 为准；本节是验证后补记。
