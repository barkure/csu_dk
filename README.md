# csu-dk

一个妙妙道具：用邮箱验证码登录，托管学号密码，在学校开放的打卡时段内随机选择时间自动打卡并保留记录。

基于 Python 3.12+、FastAPI、SQLite、Jinja2 和 htmx。

## 功能

- 多学号托管，保存前验证凭据
- 自动读取打卡时段、随机调度、失败重试与跨日重新认证
- 坐标和楼栋校验、图形验证码识别及失败诊断
- 邮箱白名单、登录限流、会话限制和账号级串行执行
- AES-256-GCM 加密保存密码、token 和 Cookie

## 快速开始

```bash
uv sync
cp .env.example .env
uv run python -m app
```

服务默认监听 <http://127.0.0.1:8443>。所有配置及说明见 [`.env.example`](.env.example)。未配置腾讯云 SES 时，登录验证码只会写入服务日志，供本地调试。

## 部署

应用以单进程运行为前提，请勿使用多个 Uvicorn worker，也不要拆分 web 与 worker 进程。账号锁、CAS 并发控制和 IP 冻结暂停状态仅在进程内共享，多进程会绕过这些保护。公网部署还应：

- 使用 Caddy、Nginx 等反向代理提供 HTTPS
- 设置 `CSU_DK_TRUST_PROXY=1` 和 `CSU_DK_COOKIE_SECURE=1`
- 通过 `CSU_DK_ALLOWED_EMAILS` 限制可登录邮箱
- 在反向代理层限制请求体大小并覆盖客户端转发头

数据库和主密钥位于 `data/`。主密钥丢失后，已保存的凭据无法恢复；备份时必须完整备份该目录：

```bash
tar czf csu-dk-backup-$(date +%F).tar.gz -C <项目目录> data
```

## 项目结构

```text
app/
├── main.py / ui.py        应用入口、JSON API 与页面路由
├── accounts.py / auth.py  托管账号、验证码认证与会话管理
├── checkin.py             学校登录态与打卡流程
├── scheduler.py           任务调度、重试与租约协调
├── db.py / schema.sql     SQLite 数据访问与结构定义
├── config.py / crypto.py  配置、主密钥与凭据加密
├── csu/                   CAS、智慧学工接口与验证码识别
├── templates/             Jinja2 页面模板
└── static/                样式、定位脚本与 htmx
test/                      单元、接口及端到端测试
```

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/health` | 健康状态与默认配置 |
| `POST` | `/api/auth/request-code` | 获取邮箱验证码 |
| `POST` | `/api/auth/verify` | 验证并登录 |
| `POST` | `/api/auth/logout` | 注销会话 |
| `GET` | `/api/auth/me` | 当前用户 |
| `GET` / `POST` | `/api/accounts` | 查询或保存账号 |
| `PATCH` / `DELETE` | `/api/accounts/{account_id}` | 更新或删除账号 |
| `POST` | `/api/accounts/{account_id}/run` | 立即打卡 |
| `POST` | `/api/accounts/{account_id}/relogin` | 重新认证 |
| `GET` | `/api/accounts/{account_id}/records` | 执行记录 |

## 测试

```bash
uv run pytest
uv run ruff check .
uv pip check
```

常规测试不会访问学校系统。端到端测试需要运行中的服务和真实账号，并可能产生实际打卡：

```bash
CSU_USERNAME=学号 CSU_PASSWORD=密码 uv run python test/e2e.py
```

## 许可与致谢

- 本项目采用 [MIT License](LICENSE) 开源
- CAS 登录流程参考自 [@Dislink](https://github.com/Dislink)
- 管理界面使用 [htmx 4.0.0](https://htmx.org/)（MIT License）
- 其他依赖见 `pyproject.toml`
