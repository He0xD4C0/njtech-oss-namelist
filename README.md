# 南京工业大学开源软件仓库名录

通过站内邮箱认证及 GitHub、Gitee、GitLab.com OAuth 核验直接归属的公开 Git 仓库目录。机审的放行权只来自独立策略仓库的许可证文本，管理员不能手动放行；`archived` 仅作标签，不剥夺收录与榜单资格。

Python 3.12 / Django 5.2 LTS / PostgreSQL 17 / Redis 8 / Celery / ScanCode。公开界面采用 Django Templates、Tabler；管理后台采用 Django Admin、Unfold。主站源码采用 **AGPL-3.0-only**，全文见 [LICENSE](LICENSE)。第三方依赖保留各自许可证，前端资源从唯一根目录 npm 锁文件构建，没有运行时 Node。

## 本地运行

先安装 uv、Node.js 22、Docker Compose 2.24.4 或更新版本。ScanCode 32.x 的原生库验证应在 Linux amd64 执行，Apple Silicon 本地容器采用 amd64 仿真。

以下命令用于源码目录；CI 交付包只包含手动运行文件，应使用 [CI 交付与手动运行](docs/release-acceptance.md) 中的步骤。

```sh
uv sync --frozen
npm ci --ignore-scripts --no-audit --no-fund
npm run build
docker compose -p namelist-dev -f compose.yaml -f deploy/compose.local.yaml up --build --detach --wait
```

本地 Web 为 `http://localhost:18000`，应用通过 WhiteNoise 同时提供 `/static/` 资源；Mailpit 为 `http://localhost:18025`。两者仅绑定回环地址。本地覆盖配置只包含明确标记的开发凭据；**不得**用于公网部署。

首次初始化使用本地 bootstrap secret `local-development-bootstrap`；注册仍需要初始化、SMTP 验证、Turnstile 与必需政策正文就绪。Mailpit 可验证邮件流程，但不证明真实公网邮件可达；本地没有真实 OAuth 或 CAPTCHA 密钥时相应流程保持禁用，不添加生产绕过。

本地 SMTP 可在初始化界面设为 `mailpit:1025`、无加密、无认证。真实部署使用受信任 SMTP/TLS 服务。查看应用 CLI 的实际参数：

```sh
docker compose -p namelist-dev -f compose.yaml -f deploy/compose.local.yaml run --rm web python manage.py help
```

## 初始化与界面

`/setup/` 按引导授权、邮件与初始管理员、安全与政策、平台与头像存储、运维、检查激活六步配置。步骤通过 `?step=` 切换，每个表单只保存自己的字段组；返回、刷新和重新授权后可继续已保存的进度。加密密钥不回显，留空保留原值。SMTP 字段改变后必须重新验证发送，初始账号只有通过邮箱确认并完成激活后才获得超级管理员权限。跨设备邮箱确认不会授予初始化配置权限。

`SiteState.activated_at` 是唯一开放标志；记录不存在也保持关闭。未激活时普通页面、账号入口、后台、业务接口及未知地址均返回 `302` 和禁止缓存头，跳转初始化；被拦截的 POST 不执行业务操作。只放行 `/setup/`、静态文件、语言切换和初始账号的有效邮箱确认链接。缺少 `BOOTSTRAP_SECRET` 时初始化返回 `503` 配置提示，其他入口继续受限；激活后初始化入口返回 `404`。容器原有 TCP、数据库和缓存健康检查不受影响。

管理平台（`/admin/`、`/manage/settings/`）固定使用简体中文，公开页面与账号工作区保留中英切换。管理首页提供待处理申请、待机审申请、公开仓库和失败任务的列表入口；仓库与机审列表支持搜索和筛选，申请详情可打开最近五次机审证据。管理员仍不能手动放行仓库。

站点配置沿用 `/manage/settings/` 和超管权限，按分组展开，保存后保留分组位置；邮件与头像存储可保存当前输入并测试连接，验证错误在原表单显示。同步提交、提交失败和实际完成分别记录，刷新只读取状态。外部备份配置状态由维护容器上报，通过现有缓存保留三分钟；过期或未收到状态时明确显示未知，不根据 Web 容器猜测。该状态只代表服务器配置完整性，不证明远程存储可达或备份已完成。

后台框架的补充中文翻译位于 `locale/zh_Hans/LC_MESSAGES/django.po`；修改后需使用 GNU gettext 执行 `DJANGO_DEBUG=1 uv run --frozen python manage.py compilemessages --locale zh_Hans --ignore .venv --ignore node_modules`，一并保留生成的 `django.mo`，容器运行时无需安装 gettext。中文显示信息的迁移不改表结构。

公开页、账号工作区和初始化使用本地 Tabler；后台使用 Unfold。浅色、深色及跟随系统共用 Unfold 的 `adminTheme` 本地存储，默认跟随系统。表单使用 `django-widget-tweaks==1.5.1` 的共享字段模板；后台使用 Unfold 字段样式，静态资源仍由 npm 锁文件构建。

## 验证

```sh
DJANGO_DEBUG=1 DJANGO_SETTINGS_MODULE=config.test_settings uv run --frozen pytest -m 'not scancode and not e2e'
uv run --frozen python -m unittest discover -s deploy/tests -v
docker compose --env-file .env.example config --quiet
docker compose -f compose.yaml -f deploy/compose.local.yaml config --quiet
```

容器级验证先构建 `web`、`postgres`、`maintenance` 为 `njtech-ops-<target>:local`，安装 Playwright Chromium 后运行 `.venv/bin/python deploy/tests/smoke_stack.py`。脚本独占 `njtech-ops-smoke` 项目，分别验证未配置备份仍可启动但不执行备份/演练，以及配置完整后的加密全量/差异、存储故障保留 WAL 和停库隔离恢复。两种场景均检查 `DEBUG=0` 的只读 Gunicorn、静态 MIME/gzip/HEAD/条件请求/404、桌面/手机、后台编辑器、Mailpit 和日志脱敏；结束时仅删除该项目的测试卷。

真实扫描验证参见 [licensecheck/README.md](licensecheck/README.md)。浏览器测试先安装 Chromium：`uv run --frozen playwright install chromium`。CI 分别要求普通测试、真实 ScanCode 测试与浏览器测试；本地结果不等于远端 CI、真实平台 OAuth 或部署验收。

## 独立政策仓库

- 许可证策略源：`https://github.com/He0xD4C0/njtech-oss-license-policy.git`，仅读取根目录 `whitelist/`、`blacklist/` 下一级普通文件中的完整许可证正文。
- 政策正文源：`https://github.com/He0xD4C0/njtech-oss-eula.git`，读取 `zh-CN/`、`en-US/` 下的 `privacy.md`、`terms.md`、`inclusion.md`；每种语言必须三份齐全。

在初始化或 `/manage/settings/` 中填写上述两个源地址并同步。政策正文可在普通文本中使用 `{{ operator_name }}` 和 `{{ operator_contact_email }}`，分别显示后台的“运营主体名称”和“公开联系邮箱”。字段可留空，中英页面分别显示“本站运营者 / Site operator”和“联系邮箱未设置 / Contact email not set”；不会从管理员账号或 SMTP 配置推断公开联系方式。替换不作用于代码块或链接目标，也不执行模板表达式。站点设置变更记录在配置审计中；政策页面的 Git SHA 和注册同意版本仅标识仓库正文，不包含这些运行时设置。

## Dev CI 与手动运行示例

只有推送 `dev-*` 标签才触发 `Dev CI`：首先拉取远端 `main` 并验证标签提交属于其历史，不属于 `main` 或拉取失败时停止；完整检查通过后构建 `web`、`scanner`、`maintenance`、`postgres` 四个镜像，发布到 GHCR，镜像标签为 `ci-<run-id>-<attempt>`。远端只维护 `main` 分支。共享 Checks 只供该工作流调用；普通分支推送、PR、`release-*` 标签都不触发 CI。

成功运行提供保留 90 天的 `ci-bundle` artifact，内含 `ci-bundle.tar.gz`。解压后包含 `manifest.json`（dev 标签、源码 SHA、CI run/attempt、四个镜像摘要）、`images.env` 和手动运行配置。旧五镜像清单不可混用。相同提交可以另打 release 标签，但外部 CD 的监听、验收、审批和部署不由本仓库实现。重跑时选择 **Re-run all jobs**，避免混合不同 attempt 的镜像记录。

本仓库没有服务器部署工作流、反向代理或公网入口示例，也不需要部署用 GitHub Secrets。`compose.yaml` 是唯一完整的手动应用栈示例，Web 仅通过 `127.0.0.1:${APP_PORT:-8000}` 提供 HTTP；域名、证书、HTTPS、入口超时及上传限制由外部基础设施负责。开发覆盖配置不用于公网。CI 成功不等于实际部署、真实 OAuth/邮件或异地备份验收。

- [运维手册](docs/operations.md)：AlmaLinux、两套环境、HTTP 接入边界、Secrets、备份和独立恢复。
- [CI 交付与上线检查](docs/release-acceptance.md)：产物接口、显式手动 Compose 命令及外部集成检查。
- [运行时契约](docs/runtime-contract.md)：任务队列、初始化命令、密钥接口及镜像职责。
- [实施证据与未验证项](docs/implementation-status.md)：明确区分已执行与待提供凭据的验证。

CI 交付不可变镜像摘要，不自动启动任何服务器服务或覆盖数据库；上线与回退由操作者或外部 CD 独立负责。
