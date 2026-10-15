# CI 交付与手动运行

## CI 边界与产物

`Dev CI` 只监听推送的 `dev-*` 标签。首先以完整历史拉取远端 `main`，验证标签提交是 `main` 的当前提交或祖先；拉取失败或未合入 `main` 时停止，不执行后续检查和构建。远端只维护 `main` 分支，应先推送 main 提交，再推送对应 dev 标签。共享 Checks 执行 PostgreSQL 应用测试、运维与浏览器测试、Linux amd64 真实 ScanCode、容器及备份恢复冒烟；全部成功后才构建发布四个 GHCR 镜像。普通分支推送、PR、`release-*` 都不触发工作流。仓库没有 CD、GitHub 部署 environment、服务器 Secrets 或额外审批入口。

镜像标签为 `ci-<run-id>-<attempt>`；只用不可变 `@sha256:` 摘要选择运行版本，不提供 `latest`。每个镜像记录与最终清单都绑定同一标签、源码提交、run ID 和 attempt，不能混用其他运行的记录。失败后重试选择 **Re-run all jobs**，保证当前 attempt 的四个记录齐全。

Actions artifact `ci-bundle` 保留 90 天，下载后解压其中的 `ci-bundle.tar.gz`：

- `manifest.json`：`source_tag`（`dev-*` 标签字符串）、`source_sha`（40 位提交 SHA）、`ci_run_id`（正十进制字符串）、`ci_run_attempt`（正整数），以及 `images` 中的 `web`、`scanner`、`maintenance`、`postgres` 四个 GHCR 摘要。
- `images.env`：`APP_IMAGE`、`SCANNER_IMAGE`、`MAINTENANCE_IMAGE`、`POSTGRES_BACKUP_IMAGE`，与清单逐项一致；旧五镜像清单不受支持，不可与新示例混用。
- 唯一完整应用栈 `compose.yaml`、`.env.example`、应用/数据库/备份配套配置、只读安全校验工具和说明。固定文件白名单排除代理配置、真实凭据、本地缓存和额外脚本。

同一提交可先打 dev 标签再打 release 标签，两个后缀无需一致。外部 CD 如何定位该提交、验证预发布、处理过期产物和生产放行不在本仓库实现。CI 成功及清单存在都不代表站点已上线或完成真实外部集成验收。

## 手动 Compose 示例

在独立工作目录解压 CI 包；安装 Docker Compose 2.24.4 或更新版本及 Python 3。部署主机需能拉取 GHCR，私有拉取凭据只由主机保管。先从 `.env.example` 复制自己的 `runtime.env`，填写域名、公开源码 URL、独立备份 S3、Secrets 目录等非秘密配置。密钥文件按 [运维手册](operations.md) 独立准备，禁止使用开发覆盖配置或开发凭据。

例子使用 `staging`、`APP_PORT=18000`；生产需同步改为 `namelist-production`、`SITE_ENVIRONMENT=production`、`APP_PORT=28000`、独立 Secrets 目录及 `/namelist/production` 备份前缀。Web 只绑定宿主 `127.0.0.1`，PostgreSQL 与 Redis 不发布端口。`images.env` 无需编辑，作为第二个环境文件覆盖模板中的镜像占位符。

预发布允许备份 S3 尚未配置，但须按运维手册预先建立受保护的空 `SECRET_DIR/backup` 子目录。此时即使 `BACKUP_ENABLED=1` 或 GUI 保存了演练频率，也没有备份、恢复演练或 WAL 归档保护；不能将应用启动成功视为备份验收。生产配置不允许此例外。补齐备份后须重新创建 DB/维护服务并完成首次全量备份验证。

仅限隔离内网的 HTTP 预发布可显式设置 `SITE_ENVIRONMENT=staging`、`STAGING_HTTP=1`，并使 `SITE_BASE_URL`、允许主机和 CSRF 来源与实际 HTTP 地址一致。此时会话与 CSRF Cookie 不要求 HTTPS，DEBUG 仍须为 0；生产环境拒绝此开关。应用端口仍绑定回环地址，IP 入口由外部代理提供并限制内网来源；可信请求头必须由该代理覆盖。SMTP、验证码及 OAuth 对来源地址的要求须另行实测，不能以页面可访问代替集成验收。

**宿主同名环境变量优先于环境文件。** 使用无开发变量、无 `COMPOSE_*` 自定义项的专用终端；不要从已 export 开发配置的 shell 启动。不要公开打印带敏感配置的完整 Compose 输出。

```sh
docker compose --env-file runtime.env --env-file images.env -f compose.yaml config --format json | python3 deploy/validate-compose.py staging
docker compose --env-file runtime.env --env-file images.env -f compose.yaml pull
# 更新既有实例时，先停止旧消费者，避免它们在迁移期间继续使用旧 schema。
docker compose --env-file runtime.env --env-file images.env -f compose.yaml stop worker scanner beat maintenance
docker compose --env-file runtime.env --env-file images.env -f compose.yaml up --no-build --detach --wait --wait-timeout 600
docker compose --env-file runtime.env --env-file images.env -f compose.yaml run --rm --no-deps web python manage.py check --deploy --fail-level ERROR
```

任一命令失败即停止，不继续执行后续命令。`initialize` 负责初始化/迁移并且必须成功；例子不重建 CI 镜像、不自动备份回滚，也不覆盖数据库。仓库只提供应用 HTTP；域名、证书、HTTPS、入口超时与上传限制由外部基础设施负责。操作者自行验证真实公网 DNS/TLS、页面及静态资源，按 [运行时契约](runtime-contract.md) 判断是否启用可信请求头。`SOURCE_REPOSITORY_URL` 必须指向实际公开源码，清单所列 SHA 的对应源码也必须能取得；配置检查不能代替这一验证。

## 外部集成与运行检查

操作者或外部 CD 自行完成并保留证据，本仓库不生成验收凭证：

- 邮箱注册、真实邮件确认、设置密码、Turnstile 拒绝/通过、限流及真实客户端 IP。
- 三平台实际 OAuth 身份与直接归属、重复绑定/越权拒绝、公开仓库筛选和提交至机审公开；令牌不明文持久化。
- 许可证文本差异、名单冲突、策略同步失败保留有效版本，过期任务不复活移除内容。
- Webhook 签名、每日及手动同步，政策语言/同意版本、公告编辑者及重新确认。
- 桌面/手机、筛选分页、公告弹窗、archived 资格及上海时区跨月榜单。
- S3 未配置/故障/恢复、头像优先级、注销后的持久删除重试与审核发件箱恢复。
- 独立真实备份 S3、连续 WAL；隔离恢复的表/迁移、页校验和及配置解密，不覆盖既有卷、不启动恢复业务任务。

首次运行仍需独立完成应用服务配置、超管邮箱验证和站点激活；容器健康不能代替注册条件就绪。镜像回退和数据库恢复是独立操作，不能假定迁移后的数据库与旧镜像兼容。
