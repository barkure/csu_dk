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
│   ├── main.py / ui.py        API、网页路由与应用入口
│   ├── accounts.py / auth.py  托管账号与邮箱登录
│   ├── checkin.py             学校登录与打卡流程
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

[MIT License](LICENSE)
