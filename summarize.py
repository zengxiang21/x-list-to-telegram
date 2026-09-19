#!/usr/bin/env python3
"""X 列表每日摘要：每天 08:50（北京时间）把「前一天 13:00 → 当天 08:50」
窗口内列表的所有推文汇总成日报发到 Telegram。

- 直接分页拉取 X 列表时间线（足够覆盖整个窗口），不依赖实时推送的存档
- 正文：热门推文 Top5、话题标签 Top10、活跃作者 Top N
- 附件：全量推文 txt（按时间排序）
- summary-state.json 记录上次生成日期，防止本地/GitHub 双端重复发送
"""

import html
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("daily")

AUTH_TOKEN = os.environ["AUTH_TOKEN"]
CT0 = os.environ["CT0"]
LIST_ID = os.environ["LIST_ID"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
PROXY = os.environ.get("PROXY") or None
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
TZ = ZoneInfo("Asia/Shanghai")

STATE_FILE = os.environ.get("SUMMARY_STATE_FILE", "summary-state.json")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "12"))
TODAY = datetime.now(TZ).date()

WINDOW_END = datetime.combine(TODAY, datetime.min.time(), tzinfo=TZ) + timedelta(hours=8, minutes=50)
WINDOW_START = WINDOW_END - timedelta(hours=19, minutes=50)   # 前一天 13:00

BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
          "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
GQL_URL = "https://x.com/i/api/graphql/HjsWc-nwwHKYwHenbHm-tw/ListLatestTweetsTimeline"
FEATURES = json.load(open(os.path.join(os.path.dirname(__file__), "features.json"))) \
    if os.path.exists(os.path.join(os.path.dirname(__file__), "features.json")) else None


# ---------- Telegram ----------

def tg(method: str, **kwargs):
    if DRY_RUN:
        log.info("[DRY_RUN] %s %s", method,
                 json.dumps(kwargs, ensure_ascii=False)[:300] if kwargs else "")
        return None
    last_err = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(proxy=PROXY, timeout=60) as c:
                if "files" in kwargs:
                    resp = c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                  data=kwargs["data"], files=kwargs["files"])
                else:
                    resp = c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                  json=kwargs)
            d = resp.json()
            if not d.get("ok"):
                raise RuntimeError(f"{method}: {d}")
            return d
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            last_err = e
            log.warning("Telegram %s 失败（第 %s 次）: %s", method, attempt, e)
            time.sleep(3 * attempt)
    raise last_err


# ---------- X 拉取 ----------

HEADERS = {
    "authorization": BEARER,
    "cookie": f"auth_token={AUTH_TOKEN}; ct0={CT0}",
    "x-csrf-token": CT0,
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "zh-cn",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}

FEATURES = FEATURES or {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_home_pinned_timelines_enabled": True,
    "facts_contest_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "rweb_video_screen_enabled": False,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_credit_payment_enabled": False,
    "profile_label_improvements_pcf_edit_enabled": True,
    "responsive_web_article_hook_consumption_enabled": False,
    "responsive_web_article_create_enabled": True,
}


def parse_created(s: str) -> datetime:
    return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")


def extract_tweet(result: dict) -> dict | None:
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", result)
    if result.get("__typename") != "Tweet":
        return None
    legacy = result.get("legacy") or {}
    if not legacy.get("id_str"):
        return None
    rt = legacy.get("retweeted_status_result", {}).get("result")
    if rt:
        orig = extract_tweet(rt)
        if orig:
            rt_by = (result.get("core", {}).get("user_results", {})
                     .get("result", {}).get("legacy", {}).get("screen_name"))
            return {**orig, "retweeted_by": rt_by}
        return None
    user = (result.get("core", {}).get("user_results", {})
            .get("result", {}).get("core", {})
            or result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {}))
    media = (legacy.get("extended_entities", {}) or legacy.get("entities", {})).get("media", [])
    return {
        "id": legacy["id_str"],
        "text": legacy.get("full_text") or "",
        "name": user.get("name", ""),
        "handle": user.get("screen_name", ""),
        "likes": legacy.get("favorite_count") or 0,
        "rts": legacy.get("retweet_count") or 0,
        "created": legacy.get("created_at", ""),
        "photos": [m["media_url_https"] for m in media if m.get("type") == "photo"],
        "has_video": any(m.get("type") in ("video", "animated_gif") for m in media),
        "hashtags": [h.get("text", "") for h in (legacy.get("entities", {}).get("hashtags") or [])],
    }


def fetch_window_tweets() -> list[dict]:
    """分页拉取，直到推文时间早于窗口起点或达到页数上限。"""
    out, cursor = {}, None
    seen = set()
    headers = {**HEADERS, "content-type": "application/json"}
    with httpx.Client(proxy=PROXY, timeout=30) as c:
        for page in range(1, MAX_PAGES + 1):
            variables = {"listId": LIST_ID, "count": 100}
            if cursor:
                variables["cursor"] = cursor
            r = c.get(GQL_URL, params={
                "variables": json.dumps(variables), "features": json.dumps(FEATURES)},
                headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"X 接口返回 {r.status_code}: {r.text[:200]}")
            instructions = (r.json()["data"]["list"]
                            ["tweets_timeline"]["timeline"]["instructions"])
            oldest = None
            for ins in instructions:
                for e in ins.get("entries") or []:
                    content = e.get("content") or {}
                    if content.get("cursorType") == "Bottom" and content.get("value"):
                        cursor = content["value"]
                        continue
                    ic = content.get("itemContent") or {}
                    result = (ic.get("tweet_results") or {}).get("result")
                    if not result:
                        continue
                    t = extract_tweet(result)
                    if not t or t["id"] in seen:
                        continue
                    seen.add(t["id"])
                    if t["created"]:
                        dt = parse_created(t["created"])
                        oldest = dt if oldest is None else min(oldest, dt)
                        if WINDOW_START <= dt <= WINDOW_END:
                            out[t["id"]] = t
            log.info("第 %s 页：窗口内累计 %s 条（本页最旧 %s）",
                     page, len(out), oldest.strftime("%m-%d %H:%M") if oldest else "-")
            if not cursor or (oldest and oldest < WINDOW_START):
                break
    return sorted(out.values(), key=lambda t: t["created"])


# ---------- 日报生成 ----------

import re as _re


def clean_text(s: str) -> str:
    """去掉 t.co 短链噪音，压平换行。"""
    s = _re.sub(r"https://t\.co/\w+", "", s or "")
    return _re.sub(r"\s+", " ", s).strip()


RANK_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def build_digest(tweets: list[dict]) -> str:
    n_authors = len({t["handle"] for t in tweets})
    head = (f"📊 X 列表日报\n"
            f"🗓 {WINDOW_START.strftime('%m月%d日')} 13:00 → {WINDOW_END.strftime('%m月%d日')} 08:50\n"
            f"💬 {len(tweets)} 条推文 · {n_authors} 位博主")

    if not tweets:
        return head + "\n\n窗口内列表没有新推文。"

    parts = [head, "━━━━━━━━━━"]

    top = [t for t in sorted(tweets, key=lambda t: -t["likes"]) if t["likes"] > 0][:5]
    if top:
        parts.append("🔥 热门推文\n")
        for i, t in enumerate(top):
            rt = f"（@{t['retweeted_by']} 转推）" if t.get("retweeted_by") else ""
            body = clean_text(t["text"])
            if len(body) > 110:
                body = body[:110] + "……"
            parts.append(f"{RANK_EMOJI[i]} {t['name']}（👍 {t['likes']}）{rt}")
            parts.append(body)
            parts.append(f"🔗 x.com/{t['handle']}/status/{t['id']}\n")

    tags = Counter(h for t in tweets for h in t["hashtags"] if h)
    if tags:
        parts.append("💬 大家在聊")
        parts.append("、".join(f"#{k}×{v}" for k, v in tags.most_common(8)) + "\n")

    by_author = defaultdict(list)
    for t in tweets:
        by_author[t["handle"]].append(t)
    ranked = sorted(by_author.items(), key=lambda kv: -len(kv[1]))
    names = "、".join(f"{ts[0]['name']} {len(ts)}条" for _, ts in ranked[:12])
    parts.append("👥 活跃作者")
    parts.append(names)
    if len(ranked) > 12:
        rest = sum(len(ts) for _, ts in ranked[12:])
        parts.append(f"……另有 {len(ranked) - 12} 位作者 {rest} 条")

    parts.append("━━━━━━━━━━")
    parts.append(f"📎 全部 {len(tweets)} 条推文原文见附件")
    return "\n".join(parts)


def build_attachment(tweets: list[dict]) -> bytes:
    lines = [f"X 列表日报全文 {WINDOW_START:%Y-%m-%d %H:%M} → {WINDOW_END:%Y-%m-%d %H:%M}"
             f"（北京时间，共 {len(tweets)} 条）", "=" * 40]
    for t in tweets:
        dt = parse_created(t["created"]).astimezone(TZ).strftime("%m-%d %H:%M") if t["created"] else "?"
        rt = f" [@{t['retweeted_by']} 转推]" if t.get("retweeted_by") else ""
        lines.append(f"\n[{dt}] {t['name']} (@{t['handle']}){rt}  👍{t['likes']} 🔁{t['rts']}")
        body = clean_text(t["text"])
        lines.append(body if body else "（无文字）")
        lines.append(f"链接: https://x.com/{t['handle']}/status/{t['id']}")
    return "\n".join(lines).encode()


# ---------- 状态 ----------

def already_sent() -> bool:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("last") == TODAY.isoformat()
    except FileNotFoundError:
        return False


def mark_sent():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last": TODAY.isoformat()}, f)


# ---------- 主流程 ----------

def main():
    if already_sent():
        log.info("今天（%s）的日报已发送过，跳过", TODAY)
        return
    tweets = fetch_window_tweets()
    log.info("窗口内共 %s 条推文", len(tweets))

    digest = build_digest(tweets)
    for chunk_start in range(0, len(digest), 3900):
        tg("sendMessage", chat_id=CHAT_ID, text=digest[chunk_start:chunk_start + 3900],
           disable_web_page_preview=True)

    if tweets and not DRY_RUN:
        fname = f"x-list-daily-{TODAY}.txt"
        tg("sendDocument", data={"chat_id": CHAT_ID,
                                 "caption": f"📎 全量推文 {len(tweets)} 条"},
           files={"document": (fname, build_attachment(tweets),
                               "text/plain; charset=utf-8")})
    mark_sent()
    log.info("日报发送完成%s", "（DRY_RUN）" if DRY_RUN else "")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("运行失败")
        if not DRY_RUN:
            try:
                tg("sendMessage", chat_id=CHAT_ID,
                   text=f"⚠️ 日报生成失败：{html.escape(str(e))[:400]}")
            except Exception:
                pass
        sys.exit(1)
