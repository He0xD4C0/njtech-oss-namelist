# 首版本地实现与验收状态（2026-10-15）

首版代码及可独立执行的本地验证已完成。**未初始化 Git、commit、push、GHCR 发布或远端部署，生产尚未上线。** 以下最终结果替代文末折叠的中间记录。

## 最终验证

| 验证 | 实际结果及边界 |
|---|---|
| PostgreSQL 17 服务端 | **143 passed**。含真实行锁并发、配额、转移/旧审核竞争、Broker 断连补投、无效许可证、版本、公告、榜单及头像清理回归。平台传输使用测试替身。 |
| Playwright | **6 passed**。桌面/手机、筛选分页、全页公告、后台 Markdown 编辑器、注册/站内邮箱确认/密码登录/退出与解绑；真实本地 HTTP/CSRF，外部服务使用测试替身。 |
| 真实 ScanCode/收录链 | **40 passed，122.25 秒**。Linux amd64、UID 10001、只读根文件系统、全新缓存；真实多进程冷缓存及线程复用，条款/符号差异、版权变化、冲突、损坏、三个模拟平台的提交至公开链。 |
| 运维边界 | **25 passed**。加密保留、WAL、目录/符号链接、隔离 Secrets/网络、摘要及验收凭证、脱敏日志。 |
| 构建/静态检查 | 五个运行镜像及 scanner-test 成功构建；Ruff、迁移一致性、uv 锁、npm ci/build、三种 Compose 校验通过。Actions 经 actionlint 1.7.7 校验，不等于远端 CI 通过。 |
| 实际 Compose | 最新镜像完成迁移，Gunicorn 首页及静态 HTTP 200，运行时 SMTP 向 Mailpit 实际投递，访问日志没有模拟凭据标记。 |
| 加密备份/离线恢复 | pgBackRest 2.59.3 真实全量、差异及 WAL 检查通过；停止原 Web、PG、Redis 后独立恢复，返回 `status=ok, migrations=65, encrypted_settings=1, kept=false`。配置解密、隔离 PG 一致性与页校验通过，临时卷/网络已清理。**本地 posix 仓库，不是异地 S3 验收。** |
| 官方 NginxUI | 已实际检查 loopback 管理 HTTP 200、未知主机 HTTP 关闭/TLS 拒绝，默认公开管理反代未加载。不是正式域名证书验收。 |

macOS Web 测试跳过的两个可选扫描模块不计入扫描证明；扫描证明来自上面的真实 Linux 镜像。

## 交付范围

- Django/Tabler/Unfold、邮箱与三平台 OAuth、独立邮箱/密码、凭据加密、资料/配额/注销、可选头像 S3。
- 仓库身份与收录关系分离、纯机审、动态文本黑白名单、版本防旧任务覆盖、历史与重新认领；archived 不失格。
- Git/Webhook/周期/手动同步、Git 多语言政策正文、数据库即时公告与确认、首页/目录/个人页/上海时区月榜及封榜。
- 初始化关闭、服务器 CLI 修复、独立双环境 Compose、GHCR 工作流、受限 NginxUI、加密备份及隔离恢复入口。
- `AuditDispatch` 与业务同事务持久化，每五分钟补投；`PendingAvatarDeletion` 每小时恢复，注销/上传竞态及 Broker 故障不会丢掉待清理对象。

验收中修复了共享 ScanCode 缓存删除竞态、检测器升级/缓存缺失重编、无效输入沿用旧公开结论、转移锁顺序、重复任务、队列投递丢失、GitLab 标准载荷、后台弹窗、头像竞态、只读 Nginx 和恢复参数。

`Another Author` 占位通知无法被版权解析器完整识别，负向测试保持待处理；正向收录链使用完整解析的 `Bob Smith`，没有放宽条款检查以换取通过。

## 已测试的本地镜像 ID

这些是本机 Docker 标识，**不是已发布 GHCR 摘要或生产放行记录**。后续 CI 从提交构建的新镜像仍须预发布验收。

| 标签 | ID |
|---|---|
| `njtech-ops-web:local` | `sha256:9129bea52d4e03d1b172b0704e3233bd4ea9a2f9f5e09d7f72f5aa721f795e0b` |
| `njtech-ops-scanner:local` | `sha256:233068b25e8e42d753d92be367bbc2ae4afdd39b91b622a44592937255312946` |
| `njtech-ops-postgres:local` | `sha256:cf3a8034f52689bee1583ddc633cccaad909ea230f18d68cfc4eac6a3b44fd78` |
| `njtech-ops-maintenance:local` | `sha256:420f3ea661176e3b6f2563471bfcb98a2db4bef3843f530060a5e1159c50e1dc` |
| `njtech-ops-static:local` | `sha256:51713c75adc17846c2d8c6d7a216988789643d69b9ad67364ced3fa06dd3fd25` |

扫描复验：

```sh
docker build --platform linux/amd64 --target scanner-test -t njtech-ops-scanner-test:local .
docker run --rm --platform linux/amd64 --read-only \
  --tmpfs /tmp:rw,size=2g,mode=1777 \
  --tmpfs /home/app/.cache/scancode:rw,uid=10001,gid=10001,size=2g \
  --tmpfs /app/var/work:rw,uid=10001,gid=10001,size=256m \
  -e DJANGO_SETTINGS_MODULE=config.test_settings njtech-ops-scanner-test:local
```

上述五个本地标签就绪后，`.venv/bin/python deploy/tests/smoke_stack.py` 可复验 Compose、SMTP、日志及离线恢复。脚本只操作自己创建的 `njtech-ops-smoke` 项目，拒绝覆盖已有同名容器，并清理测试卷；不能用于生产恢复。

## 尚未完成的外部验收

真实三平台 OAuth/归属/令牌刷新、公网 SMTP/Turnstile、Webhook 投递、AlmaLinux 服务器、正式 DNS/TLS、远端 CI/GHCR、异地 S3 恢复尚未验收。需要主站 GitHub 仓库、两环境域名与获授权的服务器接入、每环境 OAuth/SMTP/Turnstile/Git/Webhook 配置、独立备份 S3 及站外恢复密钥。头像 S3 可稍后配置。

按 [上线验收清单](release-acceptance.md) 完成预发布真实验收与隔离恢复后，才可明确授权提升同一批镜像摘要到生产。当前交付用于本地人工审阅，不代表生产放行。

<details>
<summary>历史中间验证记录（已被以上最终结果替代，不是当前阻塞）</summary>

## 运维中间记录

## 已实现并已执行的本地验证

- 主应用负责人提供的冻结源码验收记录：真实PostgreSQL上141项通过、2项可选扫描模块跳过，6项浏览器测试通过。运维侧独立执行的检查与容器结果如下；可选扫描跳过不作为检测器验证。
- Dockerfile包含web/scanner/postgres/maintenance/static目标；前端复用根npm锁文件，无重复deploy/frontend依赖树。
- 生产/预发布Compose、开发覆盖配置、独立Mailpit、官方NginxUI受限入口配置均已创建；三种 `docker compose config --quiet` 校验成功。
- 25项 `deploy/tests` 运维测试通过：加密备份配置、WAL/保留策略、目录/符号链接清理边界、摘要及验收凭证校验、非公网数据库、Secrets分发、恢复数据只读挂载、脱敏访问日志和tmpfs YAML拆分回归等。根目录Ruff检查、运维Python格式检查通过。
- shell语法与Python编译检查通过。
- 实际amd64 Postgres+pgBackRest镜像构建成功，运行包 pgBackRest 2.59.3。初次apt下载遇到临时EOF，启用标准Acquire重试后成功。
- 实际Web构建曾在collectstatic暴露allauth缺少requests；主工程已修复socialaccount extra并更新锁文件，后续Web/扫描/维护/静态镜像均已成功构建。应用有继续修正，最终快照待重建补记。
- 官方NginxUI镜像已实际启动：loopback管理HTTP200；未知主机HTTP关闭、TLS拒绝；运行配置不加载默认9000管理反代。测试容器已删除。
- 三份GitHub Actions文件经actionlint 1.7.7验证通过；本地只检查工作流，没有运行GitHub CI/发布。
- 隔离Compose已实际完成迁移、Gunicorn首页HTTP200、独立静态HTTP200与运行时SMTP向Mailpit真实投递。期间修复只读Nginx临时目录、开发Mailpit网络和PG临时初始化服务健康探针问题；每次失败项目的临时卷/网络均清理。
- 本地客户端加密pgBackRest真实全量、差异备份及WAL检查通过；停止原Web、PG及Redis后，以独立维护入口恢复并验证63条迁移、1项加密配置解密、隔离PG一致性和页校验和。返回 `status=ok, kept=false`。这是开发posix备份仓库，不是异地S3验收；最新迁移与最终快照仍需再验。
- 恢复实测修正了同步归档下不合法的spool-path、标准archive-push命令与隔离PG参数；不删除backup_label、不关闭archive-check或页校验和来绕过验证。测试清理显式启用ops profile，防止遗留恢复临时卷。

## 扫描运行时集成：中间快照未通过

中间快照真实 `scanner-test`（Linux amd64 / 非root / ScanCode32.5）执行 `tests/test_licensecheck.py` 和 `tests/test_ingestion.py` 共39项：36通过、3失败。原生缓存重用及真实多进程冷缓存测试已通过；三个本地平台收录链在 `tests/test_ingestion.py:91` 期望published、实际needs_review。此结果不是全部通过，最终构建等待引擎负责人修复快照后重跑。

冻结源码只读挂载后以 `-x` 复现首个收录链失败（50秒）。真实版权解析器对 `2026 Another Author` 仅解析年份，整行因此保留；`Alice Smith` / `Jane Wilson` 则完整解析。该fixture不能证明完整版权通知的作者容差；应使用无歧义姓名验证该链条，不应为不完整解析放宽许可证条款检查。

镜像配置可写的 `SCANCODE_CACHE`、`SCANCODE_LICENSE_INDEX_CACHE`、`SCANCODE_PACKAGE_INDEX_CACHE`，不用root写入只读site-packages。动态授权索引独立，不从外部加载pickle；数据库恢复后由策略同步验证/重建缓存。这些本地模拟平台收录链不是实时OAuth证明。

## 仍待实际证明

当前文件不是上线完成凭证。最终源快照仍等待原生引擎修复及重跑，最终五镜像ID和最新数据库迁移恢复结果完成后补记。运行时契约已同步审核AuditDispatch持久发件箱、每五分钟恢复、两小时扫描上限、头像PendingAvatarDeletion每小时恢复和日历月演练。

未执行GitHub远端CI、GHCR发布、SSH部署、真实域名/TLS、真实SMTP/Turnstile、三个OAuth平台、真实Webhook或异地S3恢复验收。没有任何commit/push/远端部署授权被推断或执行。开发Mailpit或本地加密仓库只验证程序链条，不替代这些外部门槛。

</details>
