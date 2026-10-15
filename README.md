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

## 验证

```sh
DJANGO_DEBUG=1 DJANGO_SETTINGS_MODULE=config.test_settings uv run --frozen pytest -m 'not scancode and not e2e'
uv run --frozen python -m unittest discover -s deploy/tests -v
docker compose --env-file .env.example config --quiet
docker compose -f compose.yaml -f deploy/compose.local.yaml config --quiet
```

容器级验证先构建 `web`、`postgres`、`maintenance` 为 `njtech-ops-<target>:local`，安装 Playwright Chromium 后运行 `.venv/bin/python deploy/tests/smoke_stack.py`。脚本独占 `njtech-ops-smoke` 项目，使用 `DEBUG=0` 的真实 Gunicorn 和只读镜像，验证静态资源的 MIME、gzip、HEAD、条件请求及 404，检查桌面/手机与管理后台编辑器，再验证 Mailpit、日志脱敏、加密全量/差异备份及停库后的独立恢复；结束时仅删除该项目的测试卷。

真实扫描验证参见 [licensecheck/README.md](licensecheck/README.md)。浏览器测试先安装 Chromium：`uv run --frozen playwright install chromium`。CI 分别要求普通测试、真实 ScanCode 测试与浏览器测试；本地结果不等于远端 CI、真实平台 OAuth 或部署验收。

## Dev CI 与手动运行示例

只有推送 `dev-*` 标签才触发 `Dev CI`：完整检查通过后构建 `web`、`scanner`、`maintenance`、`postgres` 四个镜像，发布到 GHCR，镜像标签为 `ci-<run-id>-<attempt>`。共享 Checks 只供该工作流调用；普通分支推送、PR、`release-*` 标签都不触发 CI。

成功运行提供保留 90 天的 `ci-bundle` artifact，内含 `ci-bundle.tar.gz`。解压后包含 `manifest.json`（dev 标签、源码 SHA、CI run/attempt、四个镜像摘要）、`images.env` 和手动运行配置。旧五镜像清单不可混用。相同提交可以另打 release 标签，但外部 CD 的监听、验收、审批和部署不由本仓库实现。重跑时选择 **Re-run all jobs**，避免混合不同 attempt 的镜像记录。

本仓库没有服务器部署工作流、反向代理或公网入口示例，也不需要部署用 GitHub Secrets。`compose.yaml` 是唯一完整的手动应用栈示例，Web 仅通过 `127.0.0.1:${APP_PORT:-8000}` 提供 HTTP；域名、证书、HTTPS、入口超时及上传限制由外部基础设施负责。开发覆盖配置不用于公网。CI 成功不等于实际部署、真实 OAuth/邮件或异地备份验收。

- [运维手册](docs/operations.md)：AlmaLinux、两套环境、HTTP 接入边界、Secrets、备份和独立恢复。
- [CI 交付与上线检查](docs/release-acceptance.md)：产物接口、显式手动 Compose 命令及外部集成检查。
- [运行时契约](docs/runtime-contract.md)：任务队列、初始化命令、密钥接口及镜像职责。
- [实施证据与未验证项](docs/implementation-status.md)：明确区分已执行与待提供凭据的验证。

CI 交付不可变镜像摘要，不自动启动任何服务器服务或覆盖数据库；上线与回退由操作者或外部 CD 独立负责。
