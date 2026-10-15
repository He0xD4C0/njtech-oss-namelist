# 南京工业大学开源软件仓库名录

通过站内邮箱认证及 GitHub、Gitee、GitLab.com OAuth 核验直接归属的公开 Git 仓库目录。机审的放行权只来自独立策略仓库的许可证文本，管理员不能手动放行；`archived` 仅作标签，不剥夺收录与榜单资格。

Python 3.12 / Django 5.2 LTS / PostgreSQL 17 / Redis 8 / Celery / ScanCode。公开界面采用 Django Templates、Tabler；管理后台采用 Django Admin、Unfold。主站源码采用 **AGPL-3.0-only**，全文见 [LICENSE](LICENSE)。第三方依赖保留各自许可证，前端资源从唯一根目录 npm 锁文件构建，没有运行时 Node。

## 本地运行

先安装 uv、Node.js 22、Docker Compose 2.24.4 或更新版本。ScanCode 32.x 的原生库验证应在 Linux amd64 执行，Apple Silicon 本地容器采用 amd64 仿真。

```sh
uv sync --frozen
npm ci --ignore-scripts --no-audit --no-fund
npm run build
docker compose -p namelist-dev -f compose.yaml -f deploy/compose.local.yaml up --build --detach --wait
```

本地 Web 为 `http://localhost:18000`（本地代理同时服务静态资源），静态探针为 `http://localhost:18001/static/`，Mailpit 为 `http://localhost:18025`。本地覆盖配置只包含明确标记的开发凭据；**不得**用于公网部署。

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
docker compose -f deploy/edge/compose.yaml config --quiet
```

容器级验证先构建 `web`、`postgres`、`maintenance`、`static` 为 `njtech-ops-<target>:local`，再运行 `.venv/bin/python deploy/tests/smoke_stack.py`。脚本独占 `njtech-ops-smoke` 项目，实际验证首页、静态、Mailpit、加密全量/差异备份及停库后的独立恢复，结束时仅删除该项目的测试卷。`deploy/tests/edge_smoke.py` 单独验证官方 NginxUI 默认公网站点拒绝访问管理界面；测试端口均仅绑定 loopback。

真实扫描验证参见 [licensecheck/README.md](licensecheck/README.md)。浏览器测试先安装 Chromium：`uv run --frozen playwright install chromium`。CI 分别要求普通测试、真实 ScanCode 测试与浏览器测试；本地结果不等于远端 CI、真实平台 OAuth 或部署验收。

## 部署与恢复

- [运维手册](docs/operations.md)：AlmaLinux、两套环境、NginxUI 隔离、Secrets、备份和独立恢复。
- [上线验收清单](docs/release-acceptance.md)：所需输入、外部集成、预发布验收与生产提升。
- [运行时契约](docs/runtime-contract.md)：任务队列、初始化命令、密钥接口及镜像职责。
- [实施证据与未验证项](docs/implementation-status.md)：明确区分已执行与待提供凭据的验证。

本地代码可供审阅；没有执行 commit、push、GHCR 发布或远端部署。生产发布必须使用已经验收的同一镜像摘要，不重新构建，也不自动覆盖数据库。
