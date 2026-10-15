# 应用与运维运行时契约

此文件只描述交付边界，不把配置存在视为外部服务验证成功。

CI 仅由 `dev-*` 标签触发，负责测试与构建交付；不连接服务器、不处理预发布/生产放行。`release-*`、普通分支推送和 PR 不触发 CI。手动运行统一使用 `compose.yaml`，完整产物接口见 [CI 交付与上线检查](release-acceptance.md)。

## 镜像与进程

| Docker target | 用途 | 运行时 |
|---|---|---|
| `web` | Web、普通 Worker、单实例 Beat、一次性初始化 | Python 3.12，冻结普通依赖，无 Node，无 ScanCode |
| `scanner` | `scan` 队列 | 普通依赖 + uv `scanner` group，原生扫描库 |
| `postgres` | PostgreSQL 17、持续 WAL 归档 | PostgreSQL + pgBackRest，应用无访问 Docker socket |
| `maintenance` | `maintenance` 队列、独立备份计时器、离线恢复 | 同一普通 Python 环境 + PG 17 二进制 + pgBackRest |

`npm ci` 和根目录 `scripts/vendor-assets.mjs` 是唯一前端资产来源。`collectstatic` 在镜像构建期完成，`CompressedStaticFilesStorage` 生成压缩副本但不改文件名。WhiteNoise middleware 位于 SecurityMiddleware 之后，只服务 `/static/` 对应的收集目录，保持默认非版本化缓存策略；不服务上传目录或源码。头像 S3 后端不受影响。运行时只读根文件系统，不能靠启动时覆盖静态资源来更新版本。

一次性 `initialize` 服务调用 `manage.py check --deploy --fail-level ERROR`、`migrate --noinput`、`setup_periodic_tasks`。所有业务进程依赖它成功完成，避免多进程并发迁移；生产源码URL等错误会阻止初始化。本地DEBUG开发配置不冒充生产部署。初始化不创建免邮箱验证的超管；站点首次激活由应用的一次性入口完成。

## 环境变量和 Secrets

应用配置：`DJANGO_DEBUG`、`DJANGO_ALLOWED_HOSTS`、`CSRF_TRUSTED_ORIGINS`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_HOST`、`POSTGRES_PORT`、`REDIS_URL`、`SITE_BASE_URL`、`SOURCE_REPOSITORY_URL`、`WORK_ROOT`。生产要求真实公开源码URL，不能保留OWNER占位符。

服务器密钥：`DJANGO_SECRET_KEY`、`APP_ENCRYPTION_KEY`、`BOOTSTRAP_SECRET`、`POSTGRES_PASSWORD`。`scripts/entrypoint.sh` 支持对应 `_FILE` 路径，把其内容仅注入子进程；同时给出明文变量与文件会拒绝启动，不输出内容。

`TRUST_PROXY_HEADERS` 默认 `0`：不信任转发协议及地址头。仅在受控外部入口覆盖 `X-Forwarded-Proto`、`X-Real-IP` 且限制应用直连时设为 `1`。启用后使用转发协议和单个有效 IP，缺失 IP 使用连接地址，非法 IP 或链返回 400；`X-Forwarded-For` 始终不作为身份来源。`APP_PORT` 只控制回环宿主端口，Web 容器固定 8000；手动预发布 18000、生产 28000，本地开发 18000。数据库与 Redis 不发布宿主端口。

Gunicorn 访问日志只记录连接 IP、方法、状态和耗时，不记录 URL、query 或 Referer；不得恢复 request/referer 格式，避免凭据泄漏。域名、证书、HTTPS、入口超时和上传限制由外部基础设施负责，仓库只交付应用 HTTP 服务。

备份凭据仅挂载给 DB 和维护容器：`BACKUP_S3_KEY_FILE`、`BACKUP_S3_SECRET_FILE`、`BACKUP_CIPHER_FILE`。它们不存应用数据库、不进入 Web/扫描容器、不与头像 S3 共用。私有 pgBackRest 配置启动时生成在 tmpfs，权限 0600，不打印秘密。

## 队列、周期与目录

- `celery`：普通 API / Git 同步、邮件等任务。
- `scan`：`repositories.tasks.audit_submission`、`core.tasks.sync_license_policy`；一般 Worker 不安装扫描库。
- `maintenance`：`core.tasks.restore_drill`，并发固定 1；脚本另有文件锁。
- 每套环境恰好一个数据库调度 Beat。`setup_periodic_tasks` 管理每日来源同步、每小时 Git、每六小时平台 API、每五分钟 `repositories.tasks.recover_audits`、每日 Star 快照/封榜与每日恢复到期检查。
- 审核投递使用数据库 `AuditDispatch` 发件箱：策略激活与相应申请 generation 的发件箱记录在同一事务提交；提交后投递失败不丢失任务。每五分钟恢复未投递或三小时未完成的工作；迟确认、generation 校验及唯一审核结果使重复投递不会复活撤下条目。此机制不等同于依赖 Redis 持久化即可保证投递。
- 头像删除使用 `PendingAvatarDeletion`：用户删除事务捕获并持久化锁定后的最新对象键，上传与注销竞态产生的晚到对象也进入清理队列；只保存对象键，不保留被删除用户资料。普通队列执行删除，每小时 `core.tasks.recover_avatar_deletions` 重投尚未完成的条目，S3 或 broker 故障后仍能继续清理。
- 扫描 Worker 软/硬时限为 6600/7200 秒；恢复 Worker 为 18030/18300 秒，为应用的 18000 秒子进程超时留出清理窗口。扫描超时后的遗留工作由上述发件箱恢复，不把临时失败判成开源失格。
- 管理配置恢复频率为 off / weekly / monthly / quarterly；应用按最近成功演练的日历周期判定是否到期，monthly/quarterly分别为一/三个月，不把自然月固定为30天。
- 维护容器的独立计时器按上海时间每天 03:15 备份：星期日全量，其他日差异；缺首个全量或全量过期七天时改作全量。它不启动第二个 Celery Beat，DB/Redis 不可用时仍可运行 pgBackRest。

`WORK_ROOT=/app/var/work` 在 Web、普通/扫描 Worker 之间共享。动态 ScanCode 索引是服务器生成的可信缓存，不能从用户仓库导入 pickle。同一策略 SHA 仍须验证检测器版本及索引校验和；丢失或损坏时重新构建，路径包含同步 generation，不复用损坏目录。数据库恢复不要求保留 WORK 缓存，恢复后正常来源同步负责重建。维护工作目录 `/var/lib/restore/work` 独立；源数据库卷在维护容器中只读。

## 恢复脚本接口

应用执行 `/app/deploy/backup/restore-drill.sh`，主进程捕获输出、超时 18000 秒并以 Redis 锁避免重复。脚本默认总超时 14400 秒，自己以文件锁保证一个恢复进程。没有 Docker socket；PG 在维护容器内自己的临时 socket 上启动，禁用 TCP监听/归档/主库流复制。

每次恢复只允许新建 `/var/lib/restore/drill-*`，默认验证后清理。磁盘预检默认最大数据库 200 GiB，恢复后仍须预留 50 GiB；确需调整时通过维护容器 `RESTORE_MAX_BYTES`、`RESTORE_RESERVE_BYTES` 明确配置。`--keep` 仅保留已验证且已停止的数据；没有覆盖生产或预发布卷的参数。

验证包括 PG 元数据、恢复一致性、必要应用表与迁移、所有现有加密运行时配置解密、PG 页校验和。至少一个加密配置必须存在，才能声称恢复密钥有效；初始空站点不假装完成此项证明。恢复直接用 psycopg 只读 SQL，不初始化 Django/业务任务/邮件。

脚本成功返回简短 JSON，不返回解密值。失败只返回错误类别，任务结果中不保存潜在带凭据的 SQL或命令输出。应用任务失败通知走已有 `operations_emails` 和加密 SMTP 配置；数据库/整机故障时应有站外监控，数据库内的 SMTP 设置不能保证在数据库失联时发信。
