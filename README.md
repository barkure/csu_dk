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

## 项目结构

```text
.
├── app/
│   ├── main.py / ui.py        API 与网页入口
│   ├── accounts.py / auth.py  账号管理与邮箱登录
│   ├── checkin.py             学校登录与打卡
│   ├── scheduler.py           自动调度与重试
│   ├── stats.py               公开统计
│   ├── buildings.py           楼栋定位与坐标缓存
│   ├── db.py / schema.sql     数据访问与表结构
│   ├── config.py / crypto.py  配置与凭据加密
│   ├── csu/                   学校接口客户端
│   ├── templates/             页面与邮件模板
│   └── static/                样式与脚本
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
