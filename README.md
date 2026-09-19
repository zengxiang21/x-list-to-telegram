# X 列表 → Telegram 群推送

免费、零服务器方案：GitHub Actions 定时读取你的 X（Twitter）列表，把新推文推送到 Telegram 群/频道。

```
GitHub Actions (每10分钟) ──> monitor.py ──> X GraphQL 接口读列表（用你的 cookie）
                                  │
                                  └──> Telegram Bot API ──> 你的 TG 群/频道
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `monitor.py` | 主脚本：拉取列表推文 → 去重 → 推送 TG |
| `.github/workflows/push.yml` | GitHub Actions 定时任务，并自动回写去重状态 `state.json` |
| `requirements.txt` | 依赖（httpx） |
| `state.json` | 记录最后已推送的推文 ID（脚本自动生成和更新） |

## 配置（Secrets）

在仓库 Settings → Secrets and variables → Actions 中添加：

| Name | 值 |
|---|---|
| `AUTH_TOKEN` | X 的 auth_token cookie |
| `CT0` | X 的 ct0 cookie |
| `LIST_ID` | 列表数字 ID |
| `BOT_TOKEN` | Telegram bot token |
| `CHAT_ID` | 频道/群的负数 ID |

### 如何获取这些值

- **BOT_TOKEN**：Telegram 找 @BotFather 发 `/newbot`
- **CHAT_ID**：把 bot 拉进群并随便发一条消息后，浏览器打开
  `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`，在返回 JSON 里找 `"chat":{"id":-100xxxx}`
- **AUTH_TOKEN / CT0**：电脑浏览器登录 x.com → F12 → Application → Cookies → `https://x.com`，
  复制 `auth_token` 和 `ct0` 的值
- **LIST_ID**：列表页面网址 `https://x.com/i/lists/<数字>`

### 本地调试（可选）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export AUTH_TOKEN=... CT0=... LIST_ID=... BOT_TOKEN=... CHAT_ID=...
export PROXY=http://127.0.0.1:7897   # 本地网络访问不了 x.com/api.telegram.org 时需要
DRY_RUN=1 .venv/bin/python monitor.py
```

`DRY_RUN=1` 只打印不发送。`FIRST_RUN_PUSH=1` 可让首次运行就推送最近几条。

## 行为说明

- **首次运行**只记录当前位置、不推送（避免一次刷几十条历史推文）
- **去重**靠 `state.json`（自动提交回仓库），运行中断也不会重发
- **单次最多推 10 条**（`MAX_PER_RUN` 可调），积压时分几次追平
- 图片以原生图片发进频道；视频/GIF 附原推链接；转推会标注谁转的
- 出错时 bot 会往频道发一条告警，避免静默失效

## 可调整项（环境变量，加到 workflow 的 env 里即可）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FETCH_COUNT` | 60 | 每次拉取的推文数 |
| `MAX_PER_RUN` | 10 | 单次最多推送条数 |
| `FIRST_RUN_PUSH` | 0 | 首次运行是否推送最近推文 |
| `DRY_RUN` | 0 | 只打印不发送 |

想提高频率：编辑 `push.yml` 的 `cron`（可叠加多条错开的 cron），免费额度内可到约 5 分钟级。

## 已知限制与风险

- **延迟约 5~15 分钟**：GitHub 免费版定时任务的物理上限。要 1 分钟级可迁 Oracle Cloud 永久免费 VPS（脚本直接循环跑即可）
- **cookie 过期**：X 可能不定期使 cookie 失效，表现为 Actions 报 401/403——重新复制 cookie 更新 Secrets 即可
- **X 改版**：脚本直连 X 网页版同款 GraphQL 接口（`ListLatestTweetsTimeline`），X 改版可能需要更新接口地址
- **账号风险**：用主号 cookie 低频只读风险较低但非零；建议注册小号专用
- 仓库每 10 分钟自动回写 state，不会触发 GitHub「60 天不活跃暂停定时任务」

## 故障排查

| 现象 | 处理 |
|---|---|
| Actions 报 401/403 | cookie 过期，重新复制 `auth_token`/`ct0` 并更新 Secret |
| 频道收不到消息 | 确认 bot 已拉进频道且有发消息权限；确认 `CHAT_ID` 是 getUpdates 里那个负数 |
| 一直"没有新推文" | 确认 `LIST_ID` 正确；列表需公开或属于你自己的账号 |
| 突然全挂 | X 改版，需要更新脚本里的 GQL_URL / FEATURES |
