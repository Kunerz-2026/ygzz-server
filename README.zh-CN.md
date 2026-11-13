# YGZZ Server

**《妖怪正传》（*Yokai: Spirits Hunt*）的非官方粉丝服务端实现。**

> 本项目与游戏的发行商、版权方**无隶属、代理或合作关系**。仓库只包含**服务端源码**，
> 不包含、也不分发游戏客户端、客户端文件、素材、数据表、美术、音频或其他版权内容。

## 状态

开发中。服务端实现了游戏网络协议和服务端系统的一大部分；兼容性取决于客户端版本和
你自备的数据表。无任何担保，使用风险自负。

## 仓库里有什么

包含：

- `run_server.py` — 启动入口
- `work/` — 服务端实现与运维工具
- `tests/` — 一小部分可独立运行的测试
- `config.ini.example` — 配置模板
- 文档、许可证与 GitHub 元数据

不包含：

- 游戏客户端、客户端补丁或客户端文件
- 游戏数据表、Lua 脚本、美术、音频或其他素材
- 账号数据、存档、日志、token 或签名材料

你需要自备兼容的数据文件与客户端环境；本项目不提供获取途径。

## 要求

- Python 3.10 或更高版本（仅标准库，无第三方依赖）
- 你合法拥有并可使用的兼容客户端与数据表
- Windows / Linux / macOS（服务端是纯 Python）

## 快速开始

```bash
git clone https://github.com/Kunerz-2026/ygzz-server.git
cd ygzz-server
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: . .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini     # Windows: copy config.ini.example config.ini
# 把自备数据表放到 data/tables/，或设置 YGZZ_TABLE_DIR
python run_server.py
```

默认端口：HTTP `18080`，游戏 `27001`/`27002`。可在 `config.ini` 修改。只开放需要的端口，
不要把后台管理端点暴露到公网。

首次启动会生成 `work/admin_token.txt`，请把它当作密码保管。

## 数据表

仓库**不附带数据表**。服务端从以下位置读取 JSON 表：

1. 环境变量 `YGZZ_TABLE_DIR`（如果设置）；否则
2. 仓库根目录下的 `data/tables/`。

可选的第二套表目录用 `YGZZ_TABLE_DIR_ALT` 指定；未设置时，依赖它的功能会优雅降级。

## 配置

`config.ini.example` 说明了可用选项：HTTP/游戏端口、邀请码、游客登录、对外地址、
举报联系方式。复制为 `config.ini` 后本地修改；`config.ini` 已被 git 忽略。

常用环境变量：

- `YGZZ_TABLE_DIR` — 数据表目录
- `YGZZ_TABLE_DIR_ALT` — 可选的第二套数据表目录
- `YGZZ_ADVERTISE_HOST` — 下发给客户端的地址
- `YGZZ_BIND_HOST` — 监听地址
- `YGZZ_AUTH`、`YGZZ_MULTI_ACCOUNT`、`YGZZ_PUBLIC` — 安全相关开关，见 `run_server.py`

## 测试

仓库包含一小部分可独立运行的测试，不需要游戏数据或设备：

```bash
python tests/run_tests.py
```

完整回归套件依赖特定环境与游戏数据，因此不在本仓库中。

## 安全

- 仅建议在私有/局域网环境使用。不要在缺少防火墙、鉴权和 HTTPS 的情况下暴露到公网。
- 保管好 `work/admin_token.txt`、`config.ini`、账号数据和日志。
- 漏洞请通过 GitHub 私密漏洞报告提交，见 `SECURITY.md`。

## 贡献

见 `CONTRIBUTING.md`。请勿提交游戏素材、客户端文件、数据表、账号数据、日志或密钥。

## 许可证

Copyright (c) 2026 kunerz。

AGPL-3.0-or-later，见 `LICENSE`。许可证只覆盖本仓库的源码，不授予游戏、素材或商标的任何权利。

## 免责声明

*Yokai: Spirits Hunt* 与 *妖怪正传* 是各自权利人的商标，此处仅用于说明兼容性。
如权利人认为本仓库侵权，请通过 GitHub 联系维护者，我们会及时处理。
