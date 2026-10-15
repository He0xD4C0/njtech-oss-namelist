# 全站主题与初始化：本地验收记录

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
