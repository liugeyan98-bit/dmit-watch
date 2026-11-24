#!/usr/bin/env python3
"""DMIT 补货监控（云端版，纯标准库）。

数据源：
  1. t.me/s/dmitnews   —— DMIT 官方「活动推送」频道（补货/上新公告，实测今天仍有更新）
  2. t.me/s/DMIT_INC   —— DMIT 官方英文频道
  3. vpsyh.net 的 RSS   —— 会播报 VPS 补货的资讯站（漏网补一道）

逻辑：抓最新帖子 → 与 state.json 里的已见 message id 对比 → 有新内容就发邮件。
不需要任何第三方依赖，GitHub Actions 上直接跑。
"""

import gzip
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)

TG_CHANNELS = [
    ("dmitnews", "DMIT活动推送"),
    ("DMIT_INC", "DMIT官方频道"),
]

FEEDS = [
    ("https://www.vpsyh.net/feed", "VPS补货播报"),
]

# 命中这些词的帖子，邮件标题会标成「补货」等级
RESTOCK_WORDS = ["补货", "補貨", "上新", "重新上线", "重新上線", "扩容", "擴容",
                 "restock", "back in stock", "available now", "到货", "到貨"]

STATE_FILE = Path(__file__).resolve().parent / "state.json"
MAX_KEEP = 400  # 每个源最多记多少个已见 id


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&#39;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def parse_telegram(channel: str) -> list[dict]:
    """解析 t.me/s/<channel> 的静态预览页。"""
    html = fetch(f"https://t.me/s/{channel}")
    # 按 data-post 切块
    marks = list(re.finditer(r'data-post="([A-Za-z0-9_]+)/(\d+)"', html))
    items = []
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        block = html[start:end]
        mid = int(m.group(2))
        text_m = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S
        )
        text = strip_tags(text_m.group(1)) if text_m else ""
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        when = time_m.group(1) if time_m else ""
        if not text:
            continue
        items.append(
            {
                "source": channel,
                "id": mid,
                "when": when,
                "text": text,
                "url": f"https://t.me/{channel}/{mid}",
            }
        )
    # 去重 + 按 id 升序
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x["id"]):
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    return uniq


def parse_feed(url: str, label: str) -> list[dict]:
    """极简 RSS/Atom 解析：取 <item>/<entry> 的 title/link/pubDate。"""
    xml = fetch(url)
    items = []
    blocks = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml, re.S | re.I)
    for b in blocks[:40]:
        t_m = re.search(r"<(?:title|media:title)[^>]*>(.*?)</(?:title|media:title)>", b, re.S | re.I)
        l_m = re.search(r"<link[^>]*>(.*?)</link>|<link[^>]*href=\"([^\"]+)\"", b, re.S | re.I)
        d_m = re.search(r"<(?:pubDate|published|updated)[^>]*>(.*?)</(?:pubDate|published|updated)>", b, re.S | re.I)
        g_m = re.search(r"<(?:guid|id)[^>]*>(.*?)</(?:guid|id)>", b, re.S | re.I)
        title = strip_tags(t_m.group(1)) if t_m else ""
        link = ""
        if l_m:
            link = strip_tags(l_m.group(1) or l_m.group(2) or "")
        if not title:
            continue
        key = (g_m.group(1).strip() if g_m else "") or link or title
        items.append(
            {
                "source": label,
                "id": key[:200],
                "when": strip_tags(d_m.group(1)) if d_m else "",
                "text": title,
                "url": link,
            }
        )
    return items


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"seen": {}, "initialized": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def is_restock(text: str) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in RESTOCK_WORDS)


def mail_config() -> tuple[str, str, str]:
    """支持两种配置方式：

    1) 一个 secret `TONGZHI`，值写成三段：邮箱|授权码|收件地址
       （分隔符支持 | 、逗号、分号、换行、空格）
    2) 三个 secret：MAIL_USER / MAIL_PASS / MAIL_TO
    """
    raw = os.environ.get("TONGZHI", "").strip()
    if raw:
        parts = [p for p in re.split(r"[|,;\n\r]+", raw) if p.strip()]
        if len(parts) == 1:
            parts = [p for p in raw.split() if p.strip()]
        parts = [p.strip() for p in parts]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], parts[1], parts[0]
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_PASS", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    return user, pwd, to


def send_mail(subject: str, body: str) -> bool:
    user, pwd, to = mail_config()
    if not (user and pwd and to):
        print("[warn] 未配置 TONGZHI（或 MAIL_USER/MAIL_PASS），跳过发信")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30, context=ctx) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"[mail] sent to {to}")
    return True


def main() -> int:
    # 保证在 GBK 控制台和 Linux runner 上都能安全输出
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    dry = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

    # 手动触发时的自检：只发一封测试邮件，不碰基线
    if os.environ.get("TEST_MAIL", "").lower() in ("1", "true", "yes"):
        try:
            sent = send_mail(
                "[DMIT Watch] 测试邮件 · 链路正常",
                "收到这封邮件说明 tongzhi 配置正确，邮件通道可用。\n\n"
                "之后 DMIT 官方频道（t.me/dmitnews / t.me/DMIT_INC）一有补货或上新公告，"
                "就会自动发到你的邮箱。\n",
            )
            if not sent:
                print("[error] 测试邮件未发送：缺少凭据")
                return 1
            print("[test] 测试邮件已发送")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[error] 测试邮件发送失败: {e!r}")
            return 1

    state = load_state()
    seen = state.setdefault("seen", {})
    first_run = not state.get("initialized")

    collected: list[dict] = []
    errors: list[str] = []

    for channel, label in TG_CHANNELS:
        try:
            items = parse_telegram(channel)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel}: {e!r}")
            continue
        already = set(seen.get(channel, []))
        fresh = [i for i in items if i["id"] not in already]
        collected.extend(fresh)
        seen[channel] = sorted({i["id"] for i in items} | already)[-MAX_KEEP:]

    for url, label in FEEDS:
        try:
            items = parse_feed(url, label)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e!r}")
            continue
        already = set(seen.get(label, []))
        fresh = [i for i in items if i["id"] not in already and "dmit" in i["text"].lower()]
        collected.extend(fresh)
        seen[label] = sorted({i["id"] for i in items} | already)[-MAX_KEEP:]

    for e in errors:
        print(f"[error] {e}")

    state["initialized"] = True
    save_state(state)

    if first_run:
        print(f"[init] 首次运行，建立基线 {len(collected)} 条，不发信")
        return 0

    if not collected:
        print("[ok] 没有新内容")
        return 0

    restock = [i for i in collected if is_restock(i["text"])]
    tg_items = [i for i in collected if i["source"] in dict(TG_CHANNELS)]
    # 中文频道优先（dmitnews 排在最后，取 [-1] 时被选中）
    tg_items.sort(key=lambda i: 1 if i["source"] == "dmitnews" else 0)
    cands = restock or tg_items or collected
    head = (cands[-1]["text"][:40] if cands else "有更新").replace("\n", " ")
    level = "补货" if restock else "新公告"
    subject = f"[DMIT {level}] {head}"

    lines = []
    for i in collected[-12:]:
        tag = "★补货" if is_restock(i["text"]) else "  公告"
        lines.append(f"{tag} | {i['source']} | {i['when']}\n{i['text'][:600]}\n{i['url']}\n")
    if errors:
        lines.append("---\n抓取失败：\n" + "\n".join(errors))
    body = "\n".join(lines)

    print(subject)
    print(body[:1500])

    if dry:
        print("[dry-run] 不发信")
        return 0

    try:
        send_mail(subject, body)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 发信失败: {e!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
