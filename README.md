# Tibo Codex Reset Bot

每 5 分钟在 GitHub 云端检查一次 Tibo（[@thsottiaux](https://x.com/thsottiaux)）的新推文。只有识别到 Codex / ChatGPT Work 的额度重置已经发生或即将发生时，才发送一封邮件；其余时间保持静默。

## 工作方式

1. GitHub Actions 每 5 分钟启动一次，不需要手机、电脑或浏览器保持在线。
2. 从公开 RSS 镜像读取推文，不使用 X API，也不产生 X API 费用。
3. 优先通过 Twiiit 自动选择当前可用的 Nitter 实例；失败后依次尝试多个直接镜像。
4. 同时满足以下两类语义才会触发：
   - 产品：`Codex`、`ChatGPT Work`、`Codexer`
   - 重置：`reset`、`banked reset`、`usage limits ... reset`
5. 识别 `tomorrow`、`lands in the next hour`、`will grant` 等未来表述，在邮件中标注“可能即将发放”。
6. 按推文 ID 记录最近 500 条已处理内容。同一条消息只提醒一次，镜像切换也不会重复发信。
7. 镜像或邮件发送失败时，本次工作流报错且不推进状态，下次运行自动重试。

## GitHub Actions 配置

进入：

`Settings → Secrets and variables → Actions → Secrets`

需要以下 Secrets：

| Secret | 当前用途 |
|---|---|
| `SMTP_HOST` | Gmail 为 `smtp.gmail.com` |
| `SMTP_PORT` | Gmail SSL 为 `465` |
| `SMTP_SSL` | Gmail SSL 为 `true` |
| `SMTP_USER` | 发件 Gmail |
| `SMTP_PASSWORD` | Gmail 的 16 位应用专用密码 |
| `EMAIL_FROM` | 通常与 `SMTP_USER` 相同 |
| `EMAIL_TO` | 接收提醒的邮箱 |

不再需要 `X_BEARER_TOKEN`。

### 可选：自定义镜像

默认会依次尝试：

- `https://twiiit.com/{username}/rss`
- `https://xcancel.com/{username}/rss`
- `https://nitter.net/{username}/rss`
- `https://nitter.poast.org/{username}/rss`

如需覆盖，在 `Settings → Secrets and variables → Actions → Variables` 新建 `RSS_FEED_URLS`，用逗号或换行分隔网址；网址中的 `{username}` 会自动替换为 `thsottiaux`。

## 第一次运行

进入：

`Actions → Check Tibo for Codex resets → Run workflow`

第一次仅处理过去 60 分钟内的推文，避免把很久以前的消息误当成新提醒。之后只处理未见过的推文。

## 测试

项目只使用 Python 标准库：

```bash
python -m unittest -v
```

## 可靠性说明

公共 RSS 镜像免费，但可能临时限流、失效或被 X 的接口变化影响。因此机器人采用自动切换和失败重试来降低漏报概率，但无法达到官方付费 API 的稳定性。

GitHub 计划任务也可能因平台繁忙稍有延迟，因此“5 分钟”是计划检查间隔，不是严格的实时保证。
