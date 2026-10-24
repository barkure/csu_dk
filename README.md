# csu-dk

一个妙妙道具。用户通过邮箱验证码登录，添加并验证学号后，系统自动完成每日打卡。

基于 Python 3.14+、FastAPI、SQLite、Jinja2 和 htmx。

## 启动

```bash
uv sync
cp .env.example .env
uv run python -m app
```

配置见 [`.env.example`](.env.example)。

## 学校短信认证

已保存的账号在学校要求短信多因子认证时，会显示“待短信认证”并暂停自动登录尝试。
在账号“管理”中点击“继续学校认证”，依次准备认证、请求学校发送短信、输入绑定手机号收到的验证码。
认证成功后保存加密登录态并保留原有自动打卡开关；本流程只验证登录，不提交打卡。

认证会话使用原来的 CAS 会话及校园网代理出口，仅在当前服务进程内保存，约 10 分钟过期；
服务重启或修改账号凭据后需重新准备认证。请使用单个 Web worker 完成此流程。
短信请求至少间隔 2 分钟，每个会话最多提交 3 次验证码；验证码不会保存或写入日志。
“信任此打卡服务的登录设备”默认关闭，是否勾选由账号本人决定，有效期由学校控制。
本流程目前支持已保存账号的短信认证；其他二次认证方式仍需在学校官方页面处理。

## 项目结构

```text
.
├── app/
│   ├── main.py / ui.py        API、网页路由与应用入口
│   ├── accounts.py / auth.py  托管账号与邮箱登录
│   ├── checkin.py             学校登录与打卡流程
│   ├── mfa.py                 学校短信认证会话与验证
│   ├── scheduler.py           自动打卡、重试与调度租约
│   ├── buildings.py           宿舍定位与坐标缓存
│   ├── db.py / schema.sql     SQLite 数据访问与结构
│   ├── config.py / crypto.py  配置与凭据加密
│   ├── csu/                   学校接口客户端
│   ├── templates/             页面模板
│   └── static/                前端样式与脚本
├── test/                      测试
└── tools/                     工具
```

## 测试

```bash
uv run pytest
uv run ruff check .
```

## 致谢

- CAS 登录流程参考自 [@Dislink](https://github.com/Dislink)
- 管理界面使用 [htmx 4.0.0](https://htmx.org/)（MIT License）

## 许可证

[MIT](LICENSE)
