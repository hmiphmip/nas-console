# NAS 出片控制台

面向 **n8n 自动化视频生产流水线** 的单页控制台。零前端依赖（原生 HTML/CSS/JS），后端仅用 Python 标准库实现，实时展示流水线状态、阶段进度、成片审片、发布准备与系统配置。

> 定位：自托管工作台的"看板 + 操作台"。它读取真实执行产物，不伪造登录或发布状态。

## 功能

- **流水线总览**：吸顶进度条、8 个阶段（市场研究 → 选题 → 方案规划 → 软件开发 → 独立验收 → 屏幕录制 → 视频合成 → 归档复盘）实时状态。
- **一键启动**：总览「流水线状态」卡片空闲时可点击任意处启动全流程，运行中自动禁用；也可在执行控制区启动 / 刷新。
- **运行数据**：14 天每日执行、成功耗时分布、阶段健康度、失败与重试、执行记录。
- **成片审片台**：在线播放归档 / 出片目录中的成片，列出体积与时长。
- **发布准备**：按平台生成发布包（抖音 9:16 母版、小红书 3:4 安全版），加入发布队列。**不代登录、不代发布、不自动发送。**
- **系统配置**：立题方向、模型网关与 API 密钥、按阶段指定模型、配音音色。

## 目录结构

```
console/
├── console_server.py   # 后端：标准库 HTTP 服务 + 业务 API（端口默认 20130）
├── index.html          # 页面结构（总览 / 数据 / 成片 / 操作台）
├── app.js              # 前端逻辑（零依赖，原生 DOM）
├── style.css           # 视觉样式（亮/暗双主题）
└── tests/              # 访问控制回归测试（标准库 unittest）
```

## 环境要求

- Python 3.8+
- 一个运行中的 [n8n](https://n8n.io/) 实例，以及与之配套的工作流目录（本文以 `workflows/` 结构约定为准）
- 可选：`ffmpeg`（发布包转码与抽帧）、`docker`（读取 n8n 容器状态）

## 快速开始

```bash
# 1) 安装依赖：无（纯标准库）
# 2) 配置环境变量（见下表），按你的实际部署填写
export CONSOLE_BASE=/opt/n8n/console
export WF_ROOT=/opt/n8n/workflows
export N8N_URL=http://localhost:5678
export EXECUTOR_HOST=127.0.0.1
export EXECUTOR_PORT=18764

# 3) 启动
python3 console_server.py
# 打开 http://localhost:20130
```

需要从局域网访问时，必须同时给出监听地址与令牌，否则服务会拒绝启动：

```bash
export CONSOLE_BIND=0.0.0.0
export CONSOLE_TOKEN=$(openssl rand -hex 32)
python3 console_server.py
# 首次用 http://<内网IP>:20130/?token=<CONSOLE_TOKEN> 打开，之后浏览器自动携带 Cookie
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONSOLE_BASE` | 脚本所在目录 | 控制台工作目录（存放 `state.json` / `config.json` / `packages/` 等） |
| `WF_ROOT` | `CONSOLE_BASE/../workflows` | n8n 工作流产物根目录 |
| `CONSOLE_PORT` | `20130` | 监听端口 |
| `CONSOLE_BIND` | `127.0.0.1` | 监听地址。**默认仅本机可访问**；设为 `0.0.0.0` 等非回环地址时必须同时设置 `CONSOLE_TOKEN`，否则服务拒绝启动 |
| `CONSOLE_TOKEN` | 空 | 访问令牌。设置后所有请求都需携带（`Authorization: Bearer`、`X-Console-Token` 或 `?token=`） |
| `WF_ID` | `nasFullPipelineManual` | n8n 中主工作流的 ID |
| `N8N_URL` | `http://localhost:5678` | n8n 控制台地址（仅用于展示/跳转） |
| `EXECUTOR_HOST` | `127.0.0.1` | 执行器主机 |
| `EXECUTOR_PORT` | `18764` | 执行器端口 |
| `EXECUTOR_URL` | 由上面两项拼出 | 执行器展示地址 |
| `MODEL_GATEWAY_URL` | `http://localhost:20128` | 模型网关管理界面地址 |
| `BACKUP_DIR` | `WF_ROOT/../_patch` | 工作流节点备份目录 |

## 测试

访问控制有回归测试，仅依赖 Python 标准库，不需要 docker / n8n / ffmpeg：

```bash
python3 -m unittest discover -s tests -v
```

覆盖：回环无令牌放行、启用令牌后缺失/错误令牌返回 401、三种令牌携带方式均通过、非回环地址无令牌时拒绝启动。

## 安全说明

- 仓库内**不含任何内网地址、主机名、密码或密钥**；所有地址通过环境变量注入。
- **默认只监听 `127.0.0.1`**。需要局域网访问时显式设置 `CONSOLE_BIND=0.0.0.0`，此时必须同时设置 `CONSOLE_TOKEN`，否则进程直接退出（退出码 2），避免管理接口在无认证状态下暴露。
- 令牌可用 `Authorization: Bearer`、`X-Console-Token`、`?token=` 或 Cookie 四种方式携带；用 `?token=` 首次打开页面后，服务端下发 `HttpOnly; SameSite=Strict` Cookie，供 `<script>` / `<link>` / `<video>` 等无法自定义请求头的子资源复用。
- 模型密钥由执行器从磁盘文件（`secrets/model-key`）读取，控制台只在本地进程内转发，不落库、不回显明文（前端仅显示掩码）。
- 运行时数据（`state.json` / `config.json` / `accounts.json` / `queue.json` / `packages/`）已列入 `.gitignore`，不会上传。
- 本工具**不代登录、不代发布**；账号状态如实显示"未登录"，不存在模拟发布。

## 说明

本项目是特定 n8n 视频生产流水线的配套控制台，接口与目录结构依赖约定好的工作流产物布局。如需用于其他流水线，请按 `STAGE_DIRS` 与各 API 的实际字段调整。
