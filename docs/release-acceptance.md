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

预发布允许备份 S3 尚未配置，但须按运维手册预先建立受保护的空 `SECRET_DIR/backup` 子目录。此时即使 `BACKUP_ENABLED=1` 或 GUI 保存了演练频率，也没有备份、恢复演练或 WAL 归档保护；不能将应用启动成功视为备份验收。服务器管理模式的生产配置不允许此例外；应用管理模式允许在向导中跳过备份，界面明确显示未保护状态。补齐备份后须重新创建 DB/维护服务并完成首次全量备份验证。

仅限隔离内网的 HTTP 预发布可显式设置 `SITE_ENVIRONMENT=staging`、`STAGING_HTTP=1`，并使 `SITE_BASE_URL`、允许主机和 CSRF 来源与实际 HTTP 地址一致。此时会话与 CSRF Cookie 不要求 HTTPS，DEBUG 仍须为 0；生产环境拒绝此开关。应用端口仍绑定回环地址，IP 入口由外部代理提供并限制内网来源；可信请求头必须由该代理覆盖。SMTP、验证码及 OAuth 对来源地址的要求须另行实测，不能以页面可访问代替集成验收。

### 当前预发布地址变更待办（2026-10-15）

目标入口已改为 `http://10.126.126.4`，SSH 用户为 `oss-namelist`。此前只读检查发现 Nginx、应用环境与 Django Site 记录仍指向旧地址 `10.10.14.219`；本轮本地 UI 修改没有更新服务器。下一次获准预发布更新时，应一起核对：

- Nginx 的入口主机及转发头；`DJANGO_ALLOWED_HOSTS` 包含 `10.126.126.4`。
- `CSRF_TRUSTED_ORIGINS=http://10.126.126.4`、`SITE_BASE_URL=http://10.126.126.4`，继续明确使用 staging 的 HTTP 开关，保持 `DEBUG=0`。
- Django Site 的 domain 为 `10.126.126.4`，name 保留 `NJTech OSS`；执行项目初始化命令后检查记录，不以环境文件已修改作为成功依据。
- GUI 中显示的三个平台回调地址、实际验证邮件和密码重置邮件中的链接均使用新地址；平台侧注册的回调地址也需对应更新。
- 从新入口走完表单与确认链接，验证旧地址没有残留。保留服务器现有数据库与 SMTP 配置；地址更新不需要重新初始化或清库。

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

### 同步执行租约迁移

包含 `core.0004_syncrun_execution_lease` 的版本为 SyncRun 增加两个可空字段。更新前停止旧 worker、scanner 和 beat，等待原执行进程结束，再由 initialize 执行迁移并启动同版本消费者；不要混跑仍不校验租约的旧消费者。历史 running 记录没有有效租约时可通过初始化/运行时配置的“重新尝试”恢复，不需清库或手工改成成功。

租约为 120 秒、每 30 秒续期。进程异常后的重投可等待当前租约到期；整个 worker 失联时，Redis 现有六小时 visibility timeout 仍可能延迟原消息重投，可在执行权失效后手动恢复。同一条任务记录会继续更新；迟到的原消息不重复发布。此说明是后续获准更新时的操作要求，本轮没有执行服务器部署。

## 外部集成与运行检查

### 许可证审批与故障记录迁移

包含 `core.0008_syncrun_detail_incident_notificationdelivery` 和 `repositories.0005_auditdispatch_attempts_auditdispatch_execution_token_and_more` 的版本新增故障/通知、原文快照、审批任务及机审执行状态。先停止旧 Web、Worker、Scanner、Beat 和维护消费者并保留可恢复备份，再执行迁移；不得混跑旧消费者。迁移保留既有证据与公开状态，并将历史已完成派发标记为完成。初始化安装审批补偿、通知补偿与每日 09:00 汇总任务，检查三项均存在且只有一个 Beat。

策略写入令牌由超管单独配置；没有令牌时原文可查看，审批明确失败且不写仓库。首笔真实审批须回读远端提交的正文和来源清单、实际激活策略与最终复审结果；测试替身通过或镜像健康不能替代这三项。恢复诊断升级不代表原恢复失败已修复。

操作者或外部 CD 自行完成并保留证据，本仓库不生成验收凭证：

- 邮箱注册、真实邮件确认、设置密码、Turnstile 拒绝/通过、限流及真实客户端 IP。
- 三平台实际 OAuth 身份与直接归属、重复绑定/越权拒绝、公开仓库筛选和提交至机审公开；令牌不明文持久化。
- 许可证文本差异、名单冲突、策略同步失败保留有效版本，过期任务不复活移除内容。
- Webhook 签名、每日及手动同步，政策语言/同意版本、公告编辑者及重新确认。
- 桌面/手机、筛选分页、公告弹窗、archived 资格及上海时区跨月榜单。
- S3 未配置/故障/恢复、头像优先级、注销后的持久删除重试与审核发件箱恢复。
- 独立真实备份 S3、连续 WAL；隔离恢复的表/迁移、页校验和及配置解密，不覆盖既有卷、不启动恢复业务任务。

首次运行仍需独立完成应用服务配置、超管邮箱验证和站点激活；容器健康不能代替注册条件就绪。镜像回退和数据库恢复是独立操作，不能假定迁移后的数据库与旧镜像兼容。

### 应用备份配置与内网生产交付

本次独立生产目标为 `http://10.126.126.4:28000`，Compose 只发布回环上游 `127.0.0.1:28001`，反向代理由操作者手动配置，本轮不配置 Nginx；使用 `namelist-production`、`SITE_ENVIRONMENT=production`、`PRIVATE_HTTP=1`、`BACKUP_CONFIG_MODE=application`、DEBUG=0。保留预发布，不复制预发布数据或密钥。初始化、SMTP、管理员及真实S3由用户填写。

迁移包含 `core.0005_backup_configuration`；四个镜像须从同一源码快照构建。工作区直接构建的交付记录源码快照SHA256和镜像ID，不能冒称CI产物，不需要提交或推送。应用管理模式首次安装允许空的备份配置；本地TLS S3验证不能替代真实用户存储验收。部署证据保存在本地 `test-results/backup-gui/` 及服务器独立生产release目录。

2026-10-16 已通过 Compose 启动独立生产栈，`TRUST_PROXY_HEADERS=1`。上游初始化页面、六种入口拦截、八项静态资源、CSRF、状态授权、代理头解析及邮件/平台回调 origin 检查通过。账号数为零，未激活，真实备份配置为空。预发布容器 ID 和现有 Nginx 配置摘要未改变。对外 `28000` 入口尚需操作者完成反代，未将上游验证冒称对外验收。源码/镜像清单保存在 `/srv/namelist/production/current/manifest.json`，安装镜像校验在 `installed-images.json`；引导密钥由操作者在服务器执行 `sudo cat /srv/namelist/production/secrets/bootstrap_secret` 读取。

### 2026-10-16 生产单入口更新（替代上述双端口约定）

已从本地快照 `20261016-single-entry-101533` 构建并通过 Compose 更新 web、worker、scanner、maintenance、beat。生产唯一宿主入口为 `10.126.126.4:28000`，直接映射容器 8000；28001 已取消。没有新增或修改本机 Nginx，预发布继续使用原入口。

公开站点与 Django Site 已统一为 `https://njtech.oss.best`，仅允许该 Host，CSRF 来源匹配，PRIVATE_HTTP/STAGING_HTTP 均为 0，Secure Cookie 恢复。代理头仅信任实际连接来源 `10.0.0.0/8`；此范围包含 `10.126.0.0/16`，不额外信任 Docker 桥接网段。

验证：相关测试 132 passed / 1 skipped；完整非 E2E、非 ScanCode 回归 262 passed / 7 skipped / 34 deselected；静态检查通过。DEBUG=0 独立容器验证可信/非可信连接、伪造请求头、初始化跳转、8 项静态资源、CSRF、Secure Cookie 和邮件/三平台回调 URL。生产从另一台机器实测连接来源 `10.126.126.3`，10 个 GET/POST 普通入口均 302 + no-store，初始化 200，静态 200，非法代理头与 Host 为 400，未授权状态请求及非法 CSRF 为 403。三个消费者 pong，Web/DB/Redis 健康。部署检查无 ERROR，仍有 HSTS 与 HTTP→HTTPS 重定向的两项提示，由外部 HTTPS 入口另行验收。

数据库、Redis 容器未重建，账号、运行时配置、激活状态与迁移记录摘要一致，密钥文件一致；预发布容器 ID 与本机 Nginx 配置摘要一致。无迁移、清库、自动激活、commit 或 push。PostgreSQL 镜像未改；三个应用镜像的本地/服务器 ID 一致。源码与镜像清单在 `var/releases/20261016-single-entry-101533/manifest.json` 和服务器 `current/manifest.json`，属于本地工作区构建，非 CI 产物。

验收记录位于 `test-results/proxy-entry/`；外部 Nginx 片段见 `docs/runtime-contract.md`。外部 DNS、TLS 证书及浏览器完整链路尚未验收，待运营者配置另一台机器的反代后完成。HTTP 上游不是另一个用户入口，初始化应通过 HTTPS 域名进行。

回退资料保留在 `/srv/namelist/production/rollbacks/20261016-single-entry-101533/`；如需回退，执行 `sudo python3 /srv/namelist/production/rollbacks/20261016-single-entry-101533/rollback-production.py 20261016-single-entry-101533`。它恢复旧镜像、环境、Site 和 current 指向，不删除卷；旧版本会恢复 127.0.0.1:28001 的旧入口。
