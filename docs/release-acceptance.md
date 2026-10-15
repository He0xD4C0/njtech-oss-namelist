# 发布与上线验收

## 输入门槛

主站公开GitHub仓库、main分支保护、GHCR权限；独立生产/预发布域名、DNS、TLS和已授权服务器接入；每环境三个OAuth应用、SMTP、Turnstile、公开GitHub许可证策略仓库、公开政策文档仓库、Webhook；站外备份S3、Fernet/备份恢复密钥与告警收件人。头像S3可不配置，但该状态必须禁用头像上传。

`SOURCE_REPOSITORY_URL` 必须指向实际公开的对应源码仓库，部署SHA的源码也必须可取得；初始化deploycheck只验证配置，不能替代人工与网络检查。生产脚本进一步请求公开仓库及该SHA的tree页面；运营者仍应人工确认镜像包含的修改与对应源码一致。服务器需能以只读GHCR身份拉取发布镜像，其凭据只留服务器，不进入应用数据库。

GitHub `staging` 与 `production` environments 分别配置 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`。known_hosts由受信渠道获得，不在自动化里使用首次 ssh-keyscan 代替主机认证。production environment 必须配置 required reviewers 和部署分支规则；这些仓库设置不能仅靠 workflow文件保证。

## 自动与人工边界

`Checks`：真实PG17上的应用测试、运维边界测试、浏览器测试；Linuxamd64真实ScanCode测试。`Release and staging`：只有main检查通过才构建五个镜像，记录一批源SHA与不可变摘要、上传发布包并部署固定预发布；迁移、容器健康、公网HTTPS可达是自动门槛。

**自动HTTP健康通过不等于完整验收。** 在预发布人工验证：

- 邮箱注册、真实站内邮件验证、设置密码、Turnstile拒绝/通过、频率限制与真实客户端IP。
- GitHub/Gitee/GitLab.com实际OAuth身份及直接归属；重复绑定/越权被拒；令牌不明文存储；提交至机审公开；私有仓库排除。
- 许可证策略作者年份/排版容差、实质条款差异、混合命中、策略同步失败保留旧版、过期任务不复活移除内容。
- 真实Webhook签名、每日/手动兜底；政策语言和注册同意版本；修改公告即时生效与编辑者名单。
- 手机/桌面页面、分页和URL筛选；公告跨设备只送达一次、政策逐页不可关闭且修改后重确认；archived不失格且入榜；上海跨月封榜/掉星。
- S3未配置/故障/恢复、用户头像优先；源删除/转私有/转移与临时网络错误的区别。
- 注销时 S3 故障、broker 投递失败及并发晚到头像上传：对象键进入持久删除队列，恢复服务后最终清理；审核发件箱在 broker 丢失及扫描进程中断后恢复，不重复公开或复活撤下记录。
- 真实备份S3、连续WAL；从备份隔离恢复应用表/页校验和/配置解密；不覆盖两套既有卷、不启动恢复业务任务。

完成并留存非敏感证据（时间、环境、源SHA、五个摘要、真实流程结果及失败修复）。在当前部署的发布目录，执行服务端验收记录：

```sh
bash deploy/release/accept-staging.sh EVIDENCE_REFERENCE 'VERIFIED TLS MAIL OAUTH WEBHOOK'
```

此命令不替用户验证前三个外部服务；它要求人工确认已完成这些项目，再执行一次真实备份与隔离恢复。只有它们成功才写 `/srv/namelist/staging/accepted/<ci-run-id>.json`，关联**同一批源SHA与所有摘要**。受控服务器用户管理验收凭证，不允许普通Web进程写入。

## 生产提升

在 `Promote verified release` workflow 手动选择成功的main发布run ID、验收证据引用，并填写 `PROMOTE VERIFIED DIGEST`。受保护production environment审批后才运行。服务器再次检查上述同一批摘要验收凭证；缺失或不匹配立即拒绝，**没有新的构建步骤**。

初次生产仍要独立初始化自己的服务配置和首个超管验证；预发布验证不授权拷贝它的账号、OAuth token、应用密钥或数据库。生产健康验证与真实域名验收单独记录；不得把首次容器启动称作站点已具备注册条件。

数据库恢复永不自动覆盖原实例。旧镜像是否可与迁移后的数据库兼容，必须单独验证；GitHub环境审核也不能授权任意破坏性数据库回退。
