# dmit-watch

DMIT 限量款补货监控（跑在 GitHub Actions 上，**你电脑关机也能收到**）。

有新公告 → 发一封邮件到你指定的邮箱（比如 Gmail）。

## 它监控什么

| 源 | 说明 |
|---|---|
| `t.me/s/dmitnews` | **DMIT 官方「活动推送」频道** —— 补货、上新、扩容都在这里首发（实测更新到 2026-11-24） |
| `t.me/s/DMIT_INC` | DMIT 官方英文频道 |
| `vpsyh.net` RSS | VPS 资讯站，只用标题里含 `DMIT` 的条目兜底 |

## 为什么不去直接查官网库存

DMIT 官网挂在 Cloudflare 后面（JS 挑战）：

- 裸 HTTP 请求 → `403 Just a moment...`
- 带浏览器 UA 的请求 → 还是 `403`
- 无头 Chrome 渲染 → `403 Attention Required!`

只有真实浏览器、过了挑战才行。放云端（GitHub Actions / 任何监控服务）去裸查一律 403，所以这里改盯**官方公告**——补货时官方会先发帖，比自己去轮询库存更实际。

## 部署（3 步）

### 1. 建仓库并推上去

GitHub 新建一个 **public** 仓库（public 的 Actions 分钟数不花钱），把本目录内容推上去：

```bash
git init
git add .
git commit -m "dmit restock watch"
git branch -M main
git remote add origin https://github.com/<你的用户名>/dmit-watch.git
git push -u origin main
```

### 2. 加 3 个 Secret

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Name | 值 |
|---|---|
| `MAIL_USER` | 你的 QQ 邮箱地址（发信账号） |
| `MAIL_PASS` | QQ 邮箱的 **16 位 SMTP 授权码**（不是 QQ 密码） |
| `MAIL_TO` | 你要收信的地址（Gmail 等） |

> 授权码获取：QQ 邮箱 → 设置 → 账户 → POP3/IMAP/SMTP 服务 → 开启 SMTP → 生成授权码。

### 3. 跑一次建立基线

仓库 → **Actions** → 左侧 `DMIT Restock Watch` → 右侧 **Run workflow**。

第一次运行只建立基线（记下已见帖子），**不会发信**。之后每 10 分钟自动跑，有新公告才发邮件。

## 邮件长什么样

- 标题：`[DMIT 补货] <公告开头>` 或 `[DMIT 新公告] <公告开头>`
- 正文：每条新公告的来源、时间、正文摘要、直达链接

## 本地试跑（不发信）

```bash
DRY_RUN=1 python watch.py
```

无第三方依赖，Python 3.9+ 即可。

## 停止 / 调整

- **停**：仓库 → Actions → `DMIT Restock Watch` → `⋯` → Disable workflow
- **改频率**：`watch.yml` 里的 `cron: "*/10 * * * *"`（GitHub 最短 5 分钟）
- **加源**：`watch.py` 顶部的 `TG_CHANNELS` / `FEEDS`

## 已知限制

- GitHub 免费 runner 的 cron **高峰期会延迟**，实测常见 5–20 分钟；它不是秒级监控。
- 它报的是「**官方发了补货公告**」，不是「官网库存刚变成有货」。抢 WEE 这种几分钟售罄的款，仍然要靠手速；但「整批补货」能撑几小时，这个方案够用。
- 公开仓库里**不要**放任何密钥，密钥一律走 Secrets。
