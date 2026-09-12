# csu-dk

一个妙妙道具。用邮箱验证码登录，托管学号密码，在打卡时段内随机挑一个时刻自动打卡并留下记录。

技术栈：Python 3.12+、FastAPI、SQLite、Jinja2、htmx。

## 功能

- 邮箱验证码登录，支持邮箱白名单、发送限流和会话数量限制
- 多学号托管，凭据验证通过后方可保存
- 自动读取学校打卡时段，并在安全时间窗内随机调度
- 复用当日业务登录态，跨日自动重新认证
- 账号级串行执行、CAS 登录并发控制和调度器数据库租约
- 自动校验坐标和楼栋信息
- 图形验证码识别，失败时停止重试并保留诊断材料
- AES-256-GCM 加密存储密码与学校登录凭据
- 服务端渲染管理界面，无前端构建步骤

## 快速开始

```bash
uv sync
cp .env.example .env
uv run python -m app
```

默认监听 `http://127.0.0.1:8443`。腾讯云 SES 配置完整时发送登录邮件；配置不完整时，验证码仅输出到服务日志，供本地调试使用。

## 配置

完整配置及说明见 [`.env.example`](.env.example)。主要选项如下：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `CSU_DK_HOST` | `127.0.0.1` | 监听地址 |
| `CSU_DK_PORT` | `8443` | 监听端口 |
| `CSU_DK_ALLOWED_EMAILS` | 空 | 登录邮箱白名单；逗号分隔，空值表示不限制 |
| `CSU_DK_MAX_ACCOUNTS` | `5` | 每个用户可托管的学号上限 |
| `CSU_DK_MAX_SESSIONS` | `10` | 每个用户保留的有效会话上限 |
| `CSU_DK_WINDOW_MARGIN` | `60` | 从学校打卡时段尾部扣除的安全余量，单位为分钟 |
| `CSU_DK_CAS_CONCURRENCY` | `1` | CAS 密码登录全局并发数 |
| `CSU_DK_TRUST_PROXY` | `0` | 是否信任反向代理转发头 |
| `CSU_DK_COOKIE_SECURE` | `0` | 是否强制会话 Cookie 使用 `Secure` |

腾讯云 SES 需要配置：

```dotenv
TENCENT_SES_SECRET_ID=
TENCENT_SES_SECRET_KEY=
TENCENT_SES_REGION=ap-hongkong
TENCENT_SES_TEMPLATE_ID=
MAIL_FROM=服务名称 <no-reply@example.com>
```

邮件模板需包含 `{{code}}` 和 `{{minutes}}` 两个变量。建议使用仅授予 SES 权限的腾讯云子账号密钥。

## 调度机制

新增或更新账号时，服务登录学校系统并读取允许的打卡时段。配置的 `CSU_DK_WINDOW_MARGIN` 会从时段末尾扣除；例如学校返回 `20:00-23:30`，默认实际调度窗口为 `20:00-22:30`。

系统在有效窗口内随机选择执行时间，并遵循以下规则：

- 当日已成功打卡时，下一次任务排至次日
- 暂不可打卡时，当晚最多自动尝试三次
- 服务恢复后，仅在 `CSU_DK_CATCHUP_MINUTES` 允许的范围内补跑
- 同一账号串行执行，CAS 密码登录默认全局串行
- 数据库租约保证多进程环境中仅一个进程执行自动调度

调度、账号锁和接口限流仍以单进程为主要运行模型。不应使用 `uvicorn --workers N` 启动多进程服务，也不要把服务拆成 web 与 worker 两个进程 —— 账号锁、CAS 并发上限、IP 冻结后的暂停状态都只在进程内共享，拆开会让这些保护失效。如需横向扩展，应将锁和限流迁移至共享存储。

## API

管理页面使用 `/ui/*` 路由。JSON API 如下：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/auth/request-code` | 发送登录验证码 |
| `POST` | `/api/auth/verify` | 验证登录码并创建会话 |
| `POST` | `/api/auth/logout` | 注销当前会话 |
| `GET` | `/api/auth/me` | 查询当前用户 |
| `GET` | `/api/accounts` | 查询托管账号 |
| `POST` | `/api/accounts` | 新增或更新托管账号 |
| `PATCH` | `/api/accounts/{id}` | 更新账号配置 |
| `DELETE` | `/api/accounts/{id}` | 删除账号及其记录 |
| `POST` | `/api/accounts/{id}/run` | 立即执行打卡 |
| `POST` | `/api/accounts/{id}/relogin` | 重建学校登录态 |
| `GET` | `/api/accounts/{id}/records` | 查询执行记录 |

## 数据与安全

- SQLite 数据库、WAL、SHM 和主密钥文件权限为 `0600`，数据目录权限为 `0700`
- 学号密码及学校 token、Cookie 等登录凭据使用 AES-256-GCM 加密存储
- 登录验证码使用主密钥执行 HMAC-SHA256 摘要，不保存明文
- 会话令牌仅以 SHA-256 摘要形式存储，浏览器 Cookie 使用 `HttpOnly` 和 `SameSite=Lax`
- 主密钥位于 `data/master.key`；丢失后无法恢复已存凭据
- 图形验证码诊断材料保存在 `data/captcha-debug/`，目录和文件使用受限权限

备份时必须同时保存数据库和主密钥：

```bash
tar czf csu-dk-backup-$(date +%F).tar.gz -C <项目目录> data
```

应用自身不提供 TLS。远程或公网部署必须通过 Caddy、Nginx 等反向代理启用 HTTPS，并配置：

```dotenv
CSU_DK_TRUST_PROXY=1
CSU_DK_COOKIE_SECURE=1
CSU_DK_ALLOWED_EMAILS=admin@example.com
```

同时应在反向代理层限制请求体大小，并覆盖客户端提供的转发头。

## 测试

```bash
uv run pytest
uv run ruff check .
uv pip check
```

常规测试禁止访问学校系统。端到端测试需要已启动的服务和真实账号，可能产生实际打卡：

```bash
CSU_USERNAME=学号 CSU_PASSWORD=密码 uv run python test/e2e.py
```

## 项目结构

```text
app/
├── main.py          FastAPI 应用与 JSON API
├── ui.py            页面与 htmx 路由
├── accounts.py      托管账号业务逻辑
├── auth.py          验证码、白名单、会话与限流
├── checkin.py       登录态维护与打卡引擎
├── scheduler.py     调度、重试、维护任务与租约
├── db.py            SQLite 存储
├── config.py        配置与主密钥管理
├── crypto.py        凭据加密
├── csu/             CAS、智慧学工和验证码识别
├── templates/       Jinja2 模板
└── static/          样式、定位脚本与 htmx
test/                单元测试与接口测试
```

## 致谢

- CAS 登录流程与密码加密实现参考 [@Dislink](https://github.com/Dislink) 提供的技术资料
- 管理界面使用 [htmx 2.0.4](https://htmx.org/)（MIT License）
- 其他依赖见 `pyproject.toml`
