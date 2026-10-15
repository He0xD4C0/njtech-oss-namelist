# 应用自带静态资源：本地实现与验证状态（2026-10-15）

仓库只交付应用及其数据/任务/备份服务，不包含反向代理或公网入口配置。CI-only 边界不变：只由 `dev-*` 标签触发完整检查、四镜像构建及交付；`release-*`、分支推送和 PR 不触发工作流。外部 CD、真实站点验收和生产放行均不由本仓库实现。

本轮改动仍为本地未提交状态，没有 commit、push、打标签、发布 GHCR 或修改 GitHub 设置。已有 Git 历史及此前远端记录不受影响。

## 当前交付

- `Dev CI` 只有 Checks、images、bundle 三阶段；移除生产工作流、服务器连接脚本和人工审批/验收凭证机制。
- 镜像使用 `ci-<run-id>-<attempt>` 标签；`ci-bundle` 保留 90 天，清单严格要求 `web`、`scanner`、`maintenance`、`postgres` 四个摘要，不兼容旧五镜像清单。包内不再交付代理文件。
- WhiteNoise 6.12.0 已锁定，使用 `CompressedStaticFilesStorage`，构建期收集并压缩静态资源，不改变原 URL 或引入指纹名。运行期由只读 Web 镜像提供，头像 S3 逻辑不变。
- 保留唯一完整应用栈 `compose.yaml`、本地开发覆盖、备份恢复以及只读安全校验。Web 固定监听容器 8000，宿主仅发布一个回环 HTTP 端口；示例预发布 18000、生产 28000、本地 18000。数据库及 Redis 不发布端口。Mailpit 仅用于本地验证。
- `TRUST_PROXY_HEADERS` 默认关闭，开启后只信任转发协议及单一有效客户端 IP，不信任 XFF 链。Gunicorn 显式禁用自己的转发头自动识别，防止绕过应用开关；访问日志仍不含 URL、query 或 Referer。外部入口的配置及安全限制由操作者负责。
- 保留运维测试的环境隔离修复：Compose 插值不继承宿主业务配置或 `COMPOSE_*` 控制变量，仍保留 Docker 工具环境，且不修改父进程。
- 本轮没有改动业务模型或新增数据库迁移。手动运行说明见 [CI 交付与手动运行](release-acceptance.md)。

## 本轮实际验证

| 验证 | 实际结果及边界 |
|---|---|
| 运维与 CI 交付 | `DJANGO_DEBUG=1`、`0` 下分别 **34 项通过**。包括代理资源缺席、四摘要/旧格式拒绝、构建身份、固定打包白名单、手动包 Compose 加载、回环 HTTP 端口、数据及 Worker 端口禁用、DEBUG 和跨环境配置拒绝。 |
| PostgreSQL 17 应用测试 | **154 passed**，两项可选 ScanCode 模块跳过，6 项浏览器另跑；明确设置 `DATABASE_SQLITE=0`，真实行锁测试未跳过。含 11 项请求头及 Gunicorn 回归；平台传输使用测试替身。本地 collectstatic 后重跑，无静态目录缺失警告。 |
| Playwright | **6 passed**，真实本地 HTTP/CSRF 与桌面/手机浏览器；外部服务使用测试替身。有一条既有测试连接导致的测试库删除警告，退出码为 0；未放宽测试或改动业务代码，隔离 PostgreSQL 容器在验证后清理。桌面与手机截图已查看，布局及样式正常。 |
| 真实 Linux ScanCode | **40 passed，最终重建后冷缓存重跑 111.75 秒**。Linux amd64、Python 3.12、非 root、只读根文件系统及独立冷缓存；真实多进程/线程索引和模拟平台收录链。不是实时 OAuth 验收。 |
| 生产模式静态与浏览器 | 真实 Gunicorn、`DEBUG=0`、只读容器，直接访问 Web 的 18000 端口。首页、CSS、JS、字体、默认头像、后台资源均通过 MIME 检查；gzip、HEAD、ETag/Last-Modified 304、缺失文件/源码及上传目录 404、默认缓存通过。桌面/手机、头像、后台编辑器和实际服务端 Markdown 预览可用，浏览器没有捕获到失败请求或页面错误。 |
| 容器与备份恢复 | 四个运行镜像成功构建；SMTP 向 Mailpit 投递、访问日志脱敏通过。pgBackRest 加密全量/差异与 WAL 检查完成，停止原 Web/PG/Redis 后隔离恢复返回 `status=ok, migrations=65, encrypted_settings=1, kept=false`。临时容器、恢复卷和网络已清理。 |
| 静态检查 | Ruff、Django 系统检查、迁移一致性、uv 锁、npm ci/build、应用及本地覆盖 Compose 配置、差异检查通过。actionlint 1.7.7 检查两个工作流通过；这不是远端 CI 运行结果。 |

CI 交付测试使用明确标记的模拟 GHCR 摘要，不代表已发布镜像。扫描证明来自真实 ScanCode 镜像，不能把可选模块跳过称为通过。容器备份测试使用隔离本地 posix 仓库，不替代真实异地 S3。生产模式浏览器使用隔离库中的测试会话，不证明真实登录、邮箱或 OAuth 接入。最终镜像验证后仅更新了验收说明及测试格式，不把本地 Docker 标识冒充 GHCR 发布摘要。

## 外部边界

本轮未触发远端 CI 或 GHCR 发布；新的标签触发和发布权限仍需后续获授权的真实 dev 标签验证。真实三平台 OAuth、公网 SMTP/Turnstile、Webhook、AlmaLinux 服务器、正式 DNS/TLS 及异地 S3 恢复也不能由本地测试替代。

CI 成功只说明检查与产物交付成功，不说明预发布/生产已部署或真实业务验收通过。上线及回退由操作者或外部系统独立负责，不自动覆盖任何数据库。
