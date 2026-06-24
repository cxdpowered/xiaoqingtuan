# 小青团 (xiaoqingtuan)

面向 QQ / 微信聊天场景的个人任务型 LLM Agent 助手：多轮对话、工具调用、长期记忆、
Hybrid RAG、多用户隔离、人工确认、操作审计、可重建索引。

- 结构说明：`AGENT_ARCHITECTURE.md`
- 底层 NoneBot2 / OneBot V11 / WxClaw 是接入层（`src/channels/`），与 Agent Core 解耦。

## 特性

- **统一 function calling**：对话与任务执行是同一个工具循环，意图与工具选择全交给 LLM。
- **Wiki-first 长期记忆**：Markdown Wiki 为唯一权威源，memories / FTS5 / LanceDB 是可重建派生索引；
  Hybrid 检索（关键词 BM25 + bge-small 语义）带来源返回。
- **多用户身份**：person / account 分离，记忆按人分区，支持 QQ↔微信 跨渠道绑定合并。
- **人工确认 + 审计**：高风险操作（如提交预约）二次确认；events / tool_calls 全程入库。
- **可扩展工具**：内置 wiki_search / note_write / web_search / 图书馆 / 预约历史；
  可接入任意外部 **MCP 服务**（见下）。
- **Docker 全栈**：`pmhq + llbot + xiaoqingtuan` 一键拉起，QQ 后端容器化。

> 已端到端跑通：CLI 对话、记忆检索与 Hybrid 召回、last-write-wins 记忆更新、高风险确认落库、
> 多用户隔离与跨渠道绑定、Docker 容器内冒烟。图书馆真实预约（Playwright）仅留接口/mock，
> 完整现状与待办见 `AGENT_ARCHITECTURE.md` §10。

---

## 项目结构

```text
.
├── bot.py                  # NoneBot 入口：注册 OneBot V11 / WxClaw / 插件 / MCP
├── src/
│   ├── agent/              # LangGraph Agent 主流程、确认门、LLM 客户端
│   ├── channels/           # QQ / 微信 / CLI 消息标准化
│   ├── identity/           # person / account 身份映射与跨渠道绑定
│   ├── memory/             # Wiki-first 记忆写入、FTS5、LanceDB、重建索引
│   ├── plugins/            # NoneBot 插件入口
│   ├── storage/            # SQLite 事件、工具调用、身份表
│   └── tools/              # 内置工具与 MCP bridge
├── wiki/                   # 共享长期知识源；wiki/users/ 为运行时个人记忆，默认不入库
├── Dockerfile              # xiaoqingtuan Agent 镜像
├── docker-compose.yml      # pmhq + llbot + xiaoqingtuan 全栈部署
├── .env.example            # 可提交的配置模板
└── AGENT_ARCHITECTURE.md   # 架构与当前交付状态
```

不会提交或打进镜像的本地数据：

- `.env` / `.env.*`：DeepSeek、Tavily、OneBot、WxClaw、MCP 等密钥或 token。
- `data/`：SQLite、LanceDB、HF 模型缓存等派生数据。
- `llbot_config/`：QQ 登录态、WebUI 密码、二维码、日志、LLBot 本地配置。
- `wiki/users/`：按 person 生成的个人画像、偏好和多用户私有记忆。
- `.local/`、`.claude/`、`.venv/`、`__pycache__/` 等本机工具和缓存目录。

## 安装

```powershell
uv sync                      # 核心依赖（langgraph / langchain-deepseek / lancedb 等）
uv sync --extra embeddings   # 可选：本地 bge-small-zh 向量检索（torch CPU，约数百 MB）
uv sync --extra mcp          # 可选：接入外部 MCP 服务（见下）
```

未装 embeddings 时向量层自动降级，检索仅靠 FTS5（BM25），其余功能不受影响。

## 本机快速体验（CLI，无需 QQ / 微信）

```powershell
# .env 里填好 DEEPSEEK_API_KEY 后：
uv run python -m src.memory.reindex   # 首次：从 wiki/ 重建索引（会下载 bge-small）
uv run python -m src.channels.cli     # 终端里直接和小青团对话
```

可试：

- `我一般喜欢坐图书馆哪里？` → LLM 调 `wiki_search`，带来源回答。
- `帮我约 2026-06-24 08:00-12:00 二楼安静区 312 座位，直接提交` → 高风险预约，回复『确认』执行。

CLI 也可模拟某渠道账号，联调多用户记忆隔离 / 跨渠道绑定：

```powershell
uv run python -m src.channels.cli --channel qq --account 123456
uv run python -m src.channels.cli --channel wechat --account wxid_x
```

---

## 接入任意 MCP 服务

小青团是 **MCP 客户端**：把外部 [MCP](https://modelcontextprotocol.io) server 的工具导入注册表后，
LLM 像调用内置工具一样调用它们——Agent 代码零改动，系统提示里的工具清单也自动出现这些远程工具。

**1) 装依赖**：`uv sync --extra mcp`（未装时 MCP 功能静默跳过，不影响内置工具）。

**2) 写配置** `mcp.config.json`（两种等价写法）：

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    },
    "weather": {
      "transport": "http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer xxxxx" },
      "tool_prefix": "wx_",
      "high_risk_tools": []
    }
  }
}
```

或显式数组：`{ "servers": [ { "name": "filesystem", "transport": "stdio", "command": "npx", "args": [...] } ] }`

**3) 指向并启动**：`.env` 设 `XQT_MCP_CONFIG=./mcp.config.json`，正常启动即可。
启动时自动连接、列出工具、并入注册表（日志打印 `已接入 MCP 工具：[...]`），退出时自动关闭连接。

配置字段：

| 字段 | 说明 |
|---|---|
| `name` | server 名（写法 A 用 key）。用于日志与 `source=mcp:<name>` 标记。 |
| `transport` | `stdio`（默认，本地子进程）/ `sse` / `http`（streamable-http）。 |
| `command` + `args` | stdio 启动命令；`url` + `headers`：sse/http 地址与鉴权。 |
| `env` | 仅 stdio：注入子进程的额外环境变量。 |
| `tool_prefix` | 给导入工具名加前缀，避免重名。 |
| `include` / `exclude` | 工具名白/黑名单，按需导入。 |
| `high_risk_tools` | **本地**指定哪些远程工具属高风险、必须二次确认。 |

**安全边界**：远程工具高风险等级由**本地白名单**决定（不信任 server 自报），调用前仍走 `confirm_gate`；
参数由对方 server 校验，结果统一归一化。想把图书馆预约拆成独立 MCP server 见 `AGENT_ARCHITECTURE.md` §7。

---

## 多用户记忆与跨渠道绑定

身份层（`src/identity`）把"人"与"账号"分开：

| 概念 | 说明 |
|---|---|
| person | 稳定的人标识（`p`+10 位 hex）。长期记忆按它分区。 |
| account | `(channel, account_id)`，如 `(qq, 123456)`。首次见到自动建 person。 |
| 绑定 | 聊天内「绑定 + 验证码」把两个账号关联成同一 person（合并记忆）。 |

记忆分区：个人记忆落到 `wiki/users/<person_id>/...`，**非 `users/` 路径是全员共享知识**
（项目文档 `projects/`、通用规则 `rules/`）。检索时某 person 只看见自己的 `users/<pid>/` + 共享路径。

> ⚠️ 共享根目录只放公共知识——**个人画像 / 偏好绝不要写进共享根目录**，否则泄漏给所有用户。
> 个人内容由写入器运行时自动落到 `wiki/users/<pid>/`；预置个性化内容按模板写到该目录再 `reindex`。

跨渠道绑定（聊天里直接发，防冒认）：

```text
绑定QQ 123456        # 微信侧发起 → 机器人回验证码（如 P52K73）
确认绑定 P52K73       # 到 QQ 123456 上发送该验证码完成绑定（10 分钟内有效）
我的身份 / whoami     # 查看当前 person 及已关联账号
```

确认时会校验"确认者正是声称要绑的那个账号"，匹配才合并 person、搬迁个人 wiki 目录并重建索引。

---

# 部署与启动

两种方式二选一，第一步都是配置 `.env`：

- **Docker 全栈（推荐，生产/服务器/WSL）**：一条 `docker compose up -d` 起三容器，QQ 后端容器化。
- **本地直跑（开发调试）**：`uv run python bot.py`，QQ 用宿主机桌面版 LLBot。

## 〇、配置凭据（`.env`）

密钥/账号集中在项目根 `.env`（已 gitignore）。首次：`cp .env.example .env` 后编辑。

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | https://platform.deepseek.com → API keys。对话/工具选择/记忆抽取都靠它。 |
| `DEEPSEEK_MODEL` / `DEEPSEEK_API_BASE` | 默认即可 | 默认 `deepseek-v4-flash`（支持 function calling）/ `https://api.deepseek.com`。 |
| `ONEBOT_ACCESS_TOKEN` | 可选 | QQ 反向 WS 鉴权 token。留空=不鉴权；若填，LLBot WebUI 那条 ws-reverse 要填同值。 |
| `WXCLAW_ACCOUNTS` | 可选 | 微信账号数组，不接微信保持 `[]`（格式见下）。 |
| `TAVILY_API_KEY` | 可选 | https://app.tavily.com（免费 1000/月）。留空时 `web_search` 返回 mock。 |
| `XQT_MCP_CONFIG` | 可选 | MCP server 配置 JSON 路径（见上「接入 MCP 服务」）。需 `--extra mcp`。 |
| `XQT_*` | 可选 | 数据/模型路径、embedding 模型，容器里已由 compose 注入。 |

最小可跑：**只填 `DEEPSEEK_API_KEY`** 即可在 CLI / QQ 对话（微信、联网搜索、MCP 均为可选增强）。

微信接入（可选）：首次用 WxClaw 扫码登录拿 `account_id` 与 `token`，填成一行 JSON 数组：

```dotenv
WXCLAW_ACCOUNTS='[{"account_id":"账号ID","token":"登录后的token","base_url":"https://ilinkai.weixin.qq.com","enabled":true}]'
```

微信走出站 HTTP 长轮询，不需开放端口。

### 安全边界

- 仓库只保留源码、可复现配置模板和共享 wiki 文档；真实密钥只放 `.env` 或服务器环境变量。
- MCP 配置可能包含 `Authorization` header，因此默认忽略 `mcp.config*.json`；需要示例时请另建脱敏模板。
- Docker 构建依赖 `.dockerignore` 过滤上下文，`Dockerfile` 虽然执行 `COPY . .`，也不会复制 `.env`、`data/`、`llbot_config/`、`wiki/users/`。
- 发布云端镜像只发布 `xiaoqingtuan` Agent 镜像；`pmhq` 与 `llbot` 继续从上游镜像拉取，QQ 登录态保存在运行服务器本地 volume/目录。

## 一、Docker 全栈部署（推荐）

三容器在同一 compose 网络 `xqt_net`（拓扑见 `AGENT_ARCHITECTURE.md` §9）。前置：装好 Docker + Compose，填好 `.env`。

```bash
# 1) 设置 LLBot WebUI 登录密码（仅英文+数字），首次必做：
echo 'yourPassword2026' > llbot_config/webui_token.txt

# 2) 构建 agent 镜像
docker compose build                                       # 精简镜像（向量层降级 FTS5，无 torch，快）
docker compose build --build-arg INSTALL_EMBEDDINGS=true   # 或：含本地 bge-small（torch CPU，较大）

# 3) 首次从 wiki/ 重建检索索引
docker compose run --rm xiaoqingtuan python -m src.memory.reindex

# 4) 一键拉起 + 看 QQ 登录二维码
docker compose up -d
docker compose logs -f pmhq llbot
```

### 构建并推送 Agent 镜像到云端

当前已发布到 Docker Hub：

```text
zachiever/xiaoqingtuan:0.1.0
zachiever/xiaoqingtuan:latest
```

先确认 `.dockerignore` 已生效，然后只构建 `xiaoqingtuan` 这一个服务镜像：

```bash
# 本地构建，默认不包含 embeddings / torch
docker compose build xiaoqingtuan

# 按你的镜像仓库打 tag。示例：
# Docker Hub: docker.io/<用户名>/xiaoqingtuan:0.1.0
# GitHub Container Registry: ghcr.io/<用户名或组织>/xiaoqingtuan:0.1.0
# 阿里云 ACR: registry.cn-hangzhou.aliyuncs.com/<命名空间>/xiaoqingtuan:0.1.0
docker tag xiaoqingtuan:dev <registry>/<namespace>/xiaoqingtuan:0.1.0
docker tag xiaoqingtuan:dev <registry>/<namespace>/xiaoqingtuan:latest

# 先 docker login 到对应 registry，再推送
docker push <registry>/<namespace>/xiaoqingtuan:0.1.0
docker push <registry>/<namespace>/xiaoqingtuan:latest
```

本仓库提供了独立的生产部署文件 `docker-compose.prod.yml`，服务器上无需构建源码：

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

服务器上仍需单独准备 `.env`、`llbot_config/webui_token.txt` 和持久化 volume；这些文件不应上传到镜像仓库或 Git 仓库。

### 扫码登录 QQ

第 4 步日志里会打印二维码，三种方式任选：

1. **二维码图片**：打开 `./llbot_config/temp/login-qrcode.png`，手机 QQ 扫。
2. **二维码链接**：日志里 `https://api.2dcode.biz/...` 那条，浏览器打开扫。
3. **WebUI**：`http://localhost:3080`（密码 = 第 1 步写入的值）。

登录成功后 pmhq 日志出现 `selfNick <昵称>`，登录态持久化在 `qq_volume`，之后重启免重扫。

### 配置 OneBot v11 反向 WS（一次性）

`http://localhost:3080` → 协议 → **OneBot 11** → 新增「反向 WebSocket (ws-reverse)」：

```text
URL:            ws://xiaoqingtuan:8080/onebot/v11/ws
Token:          留空（若 .env 设了 ONEBOT_ACCESS_TOKEN 则填同值）
messageFormat:  array
```

保存后 agent 日志出现 `OneBot V11 | Bot <QQ号> connected` 即接入成功，给该 QQ 发消息即可对话。
配好后写入 `llbot_config/`，以后 `up -d` 自动重连。

### 运维与排错

```bash
docker compose ps                          # pmhq / llbot healthy，xiaoqingtuan up
docker compose logs -f xiaoqingtuan        # 跟 agent 日志（收发/工具/确认）
docker compose run --rm --no-deps xiaoqingtuan python -m src.channels.cli  # 不依赖 QQ 的本机冒烟
docker compose up -d --build xiaoqingtuan  # 改代码：重建并滚动更新
docker compose down                        # 停（保留卷=保留登录态与数据）；-v 连卷删（需重扫+reindex）
```

> 改 `.env` 后 `docker compose up -d`（或 `restart xiaoqingtuan`）生效；改 `wiki/` 后跑一次 reindex。

常见问题：

- **Windows 开 `localhost:3080` 显示 502**：Docker Desktop 的 IPv6 代理 quirk，改在 **WSL 内**访问即正常。
- **3080 被占**（已跑桌面版 LLBot）：`LLBOT_WEBUI_PORT=3081 docker compose up -d` 换端口共存。
- **拉镜像慢**：把 compose 里镜像名换镜像源前缀，如 `docker.1ms.run/linyuchen/pmhq:latest`。
- **agent 没有 `Bot ... connected`**：确认 ws-reverse URL 是 `ws://xiaoqingtuan:8080/onebot/v11/ws`
  （容器内用服务名，不是 `127.0.0.1`），且 Token 与 `.env` 一致。
- **同一 QQ 不能两处登录**：容器化 llbot 与宿主机桌面版二选一。

## 二、推服务器

与本地同一份 `docker-compose.yml` + `.env`：拷仓库（含 `wiki/`）到服务器，填 `.env` 与
`llbot_config/webui_token.txt`，`docker compose build && docker compose up -d`，照上面扫码。
建议 ≥4GB 内存（启用 embeddings / 将来 Playwright 再加）。

## 三、本地直跑（不用 Docker，开发用）

```powershell
uv sync                                  # 可选 --extra embeddings / --extra mcp
# .env 填好 DEEPSEEK_API_KEY
uv run python -m src.memory.reindex      # 首次重建索引
uv run python bot.py                     # 启动 NoneBot，监听 :8080
```

桌面版 LLBot（`.local/llbot-desktop-win-x64-v7.12.15/llbot.exe`）扫码登录后，把 OneBot 11 反向
WebSocket 设为 `ws://127.0.0.1:8080/onebot/v11/ws`（设了 token 就在 `.env` 配 `ONEBOT_ACCESS_TOKEN`）。

运行时控制消息：`关闭插件` / `开启插件`。
