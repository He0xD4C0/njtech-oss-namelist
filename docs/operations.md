# 部署、备份与恢复手册

本仓库只提供 CI 产物和 `compose.yaml` 手动运行示例，不执行 CD。手动启动步骤见 [CI 交付与上线检查](release-acceptance.md)；以下密钥和服务器配置由操作者自行管理，不注入 GitHub Actions。

## 服务器与隔离

目标：AlmaLinux 10 / amd64 / 24 核 / 128 GiB / 1000 GiB。Docker 的 CentOS 官方安装文档不等同于 AlmaLinux 官方认证；安装后必须实际验证内核、cgroup v2、Compose 和 SELinux 卷访问。保持 SELinux enforcing，不要为绕过权限问题关闭 SELinux。新服务器接入、软件安装及远程命令必须在授权后执行。

生产项目 `namelist-production` 与固定预发布项目 `namelist-staging` 独立：卷、backend/egress/backup_egress 网络、数据库角色密码、Fernet 根密钥、bootstrap secret、域名、OAuth应用、SMTP、Webhook secret、外部存储前缀均分别配置。默认扫描并发生产 2、预发布 1；两套服务的内存/CPU上限在 Compose 中明确，总和给系统与恢复留余量。不得用预发布容器连接生产数据库。

每个环境在 `/srv/namelist/<environment>/runtime.env` 保存自己的非秘密运行配置；从 `.env.example` 派生，再通过第二个 `--env-file images.env` 选择 CI 交付的整批摘要，不把 :latest 当正式发布。生产和预发布的 `SECRET_DIR` 不能相同。Compose 的命名卷由各自项目名生成，不设置跨环境共用 `container_name`。只读校验工具 `deploy/validate-compose.py` 检查项目名、DEBUG、Secrets 目录和备份前缀，不连接服务器或修改容器。

PostgreSQL 与 Redis 不映射宿主端口。DB只加入内部 backend 和独立备份出口网络，Redis只加入 backend；Web 和应用进程使用 backend 与 egress，按需访问 Git/API/SMTP。Web 容器监听 8000，宿主只能发布一个 `127.0.0.1:${APP_PORT}:8000` TCP 端口，预发布示例为 18000、生产为 28000。安全校验拒绝公网绑定、额外端口、宿主网络与数据端口暴露。Web/扫描/维护容器均不挂载 Docker socket。

## 服务器 Secrets

各环境的 `/srv/namelist/<environment>/secrets` 独立目录内放置以下单行文件；部署检查拒绝跨环境目录或符号链接指向共用密钥目录：

| 文件 | 用途 |
|---|---|
| `django_secret` | Django 签名密钥 |
| `app_encryption_key` | URL-safe Base64 的 32-byte Fernet 根密钥 |
| `bootstrap_secret` | 首次激活一次性环境凭据 |
| `db_password` | 无超级权限的应用角色密码 |
| `db_admin_password` | PostgreSQL 管理角色密码，应用容器不挂载 |
| `backup/backup_s3_key` / `backup/backup_s3_secret` | 独立异地备份存储身份，不是头像身份 |
| `backup/backup_cipher` | 至少 32 字符高熵 pgBackRest 客户端加密口令 |

使用密码管理器生成并站外保管；不要将任何值、完整容器 inspect、带 Secrets 的环境导出或 pgBackRest 配置贴入工单。Fernet、备份口令及 S3 身份也必须站外备存。只有备份文件而没有解密密钥不构成可恢复备份。

Compose 的 `file:` secret 是 bind mount，不是加密保险库，也不保证把源文件改为声明的 uid/mode。目录由受信服务器用户管理并限制 0700；被逐文件挂载的文件应允许容器的 app(uid 10001) 或 postgres(uid 999)读取。可用受保护目录内的只读 0444文件（目录阻止其他宿主用户进入），或受控 ACL；上线检查两个非 root 容器实际能读且不能写。Docker 操作权限本身相当于主机管理权限。

备份子目录 `${SECRET_DIR}/backup` 单独只读挂载到 DB 和维护容器的 `/run/backup-secrets`，共享 SELinux 标签，不暴露其他应用密钥。宿主 Secrets 父目录保持 0700；子目录使用 0755，文件使用 0444，使容器 uid 999 可读取而宿主其他用户仍无法穿过父目录。目录必须预先建立，Compose 不自动创建路径。预发布可保留空子目录，不创建虚假凭据。旧安装应复制原有三个备份文件到子目录、验证新挂载后再由操作者决定清理旧文件，不自动覆盖密钥。

服务端应用修复命令使用镜像中的 CLI，例如通过 `docker compose run --rm --no-deps web python manage.py help` 查看配置修复实际参数。不要重新开启永久公网 bootstrap bypass 来修复 SMTP/Turnstile。

## 外部 HTTP 接入边界

应用自行提供页面及 `/static/`；头像仍由已配置的 S3 提供。仓库不包含反向代理、证书或公网入口部署方案。域名、证书、HTTPS、入口超时与上传限制由外部基础设施负责，操作者须验证实际 DNS/TLS、OAuth callback、Webhook URL 及客户端 IP 限流。

`TRUST_PROXY_HEADERS=0` 为默认值，应用忽略转发头。仅在外部入口覆盖 `X-Forwarded-Proto` 和 `X-Real-IP`，且应用直连受到限制时，才可设为 `1`。启用后接受单一有效 `X-Real-IP`，缺失时使用连接地址，非法值或地址链返回 400；不解析任意 `X-Forwarded-For` 链。此开关不会自动部署入口或建立网络信任边界。外部入口日志同样须避免记录 OAuth query、邮件验证路径或 Referer 中的凭据。

## pgBackRest 备份

`BACKUP_ENABLED=1` 仅表示启用意愿。必需参数或备份文件缺失、为空时，预发布仍可启动，但 WAL 归档关闭，备份计时器不启动，手动备份和恢复演练均不执行；GUI 保存的演练频率不能绕过门槛。此时**没有备份保护**。非法非空配置及读取权限错误仍报脱敏错误；生产部署始终要求配置完整且启用独立 S3 备份。

就绪检查只验证本地配置，不探测网络、不使用头像 S3、不产生“配置缺失”故障告警。配置完整后网络、鉴权、存储故障仍按失败处理并告警，保留待归档 WAL，不自动切换为关闭。直接脚本的未配置/关闭退出码为 2，非法配置为 1；恢复任务提前返回 `not-configured` / `disabled`，不创建恢复锁或目录。

补齐 `runtime.env` 的 S3 参数和上述三个文件后，使用相同摘要重新创建 `db`、`maintenance` 服务，再执行首次全量备份并确认 `stanza-create`、`check`、备份成功。仅修改 GUI 或填好文件而不重新创建服务，不代表 WAL 归档已经开启。

远端 S3 必须是独立站外服务；备份本机同一个磁盘不算异地。`BACKUP_REPO_TYPE=s3`，`BACKUP_REPO_PATH` 分别固定 `/namelist/staging` 与 `/namelist/production`；部署检查拒绝误接另一套环境。endpoint 填 hostname 而非 URL；开启 TLS 校验，没有生产跳过证书验证选项。使用 `aes-256-cbc` 客户端加密、zstd 压缩。

配置就绪后，维护容器每天上海时间 03:15 备份、周日全量，PostgreSQL 持续 WAL归档。首次备份和错过一周全量会自动选 full。首次人工验证：

```sh
docker compose --project-name namelist-staging --env-file /srv/namelist/staging/runtime.env --env-file images.env -f compose.yaml run --rm --no-deps maintenance /app/deploy/backup/backup.sh full
docker compose --project-name namelist-staging --env-file /srv/namelist/staging/runtime.env --env-file images.env -f compose.yaml run --rm --no-deps maintenance sh -c 'python /app/deploy/backup/render-config.py && pgbackrest --stanza=namelist --output=json info'
```

`stanza-create` 与 `check` 必须成功。独立容器的私有配置在tmpfs中，运行 `info` 前先生成它，不要打印内容。

保留策略为 [pgBackRest time retention](https://pgbackrest.org/configuration.html)：至少30天可恢复窗口，所需更早全量可能留得更久；差异与 WAL随它依赖的全量安全过期，不额外截短 WAL。没有会丢弃 WAL 的 archive队列容量上限。监控 WAL增长、存储剩余、S3故障和备份年龄；不能因归档受阻占满磁盘而无告警。

默认 monthly 恢复演练由应用每日检查是否到期；superadmin可改 off/weekly/monthly/quarterly。维护Worker单并发，文件锁再防重复。脚本的总超时、单命令超时、CPU、内存、磁盘大小/空闲预检独立限制。恢复的所有应用表只能只读检查，不执行业务任务。

## 应用不可用时独立恢复

使用维护镜像覆盖入口并不依赖 Redis、Beat、现有应用 DB；仍需要独立S3和根密钥文件。不要挂载生产数据库卷作写目标：

```sh
docker compose --project-name namelist-staging --env-file /srv/namelist/staging/runtime.env --env-file images.env -f compose.yaml run --rm --no-deps --entrypoint /app/deploy/backup/restore-drill.sh maintenance --keep
```

脚本直接读取 `APP_ENCRYPTION_KEY_FILE`，无需 Django进程。`--keep` 返回成功验证且已停止的数据目录；可附 `--backup-set 20261015-031500F` 或带 UTC offset 的 `--target-time` 实施指定恢复。常规演练不传 `--keep`，成功/失败后仅清理本次新建 `drill-*`；停止失败时保留隔离目录以免删除运行中PG。

若隔离 PG 启动失败，维护卷仅保留 `/var/lib/restore/last-startup-error.log` 的最后 64 KiB，权限 0600，供服务器管理员检查；不发送到 Celery 结果、公告或邮件。不要按照损坏检查点日志中的通用提示删除 `backup_label` 来绕过恢复一致性验证。

把恢复数据切换为正式数据库是**另一次显式授权操作**，此脚本没有覆盖现有实例的功能。实施前停流量与业务进程、保存原卷快照、确认PG版本与校验、根密钥和回退路径；不能用 `down -v` 或自动恢复替代迁移/发布回退。

## 故障与更新

任务失败和备份失败尝试向 `operations_emails` 发信，不发送解密值或带密钥错误文本。SMTP/数据库/整机本身故障时，应用内邮件不是可靠独立告警渠道；上线必须接入站外存活与备份年龄监控。

镜像回退与数据库恢复分别处理。手动更新时先拉取同一批摘要、停止旧 Worker/Beat/维护进程，再启动初始化迁移和新服务；迁移失败立即停止，不自动恢复数据库或假定旧 schema 兼容。保留原 CI 包与摘要；本仓库不生成生产验收凭证或替外部 CD 作放行判断。
