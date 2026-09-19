#!/usr/bin/env python3
"""X List -> Telegram 推送脚本。

每次运行：用已登录 X 的 cookie（auth_token + ct0）直接请求 X 的 GraphQL
接口读取指定列表的最新推文，把上次运行之后的新推文推送到 Telegram 群。

所有配置来自环境变量（GitHub Actions 里放 Secrets）：
    AUTH_TOKEN   X 的 auth_token cookie（必需）
    CT0          X 的 ct0 cookie（必需）
    LIST_ID      X 列表 ID，网址 x.com/i/lists/<数字> 里的数字（必需）
    BOT_TOKEN    Telegram bot token（必需）
    CHAT_ID      Telegram 群 chat_id（必需）
    STATE_FILE   去重状态文件路径（默认 state.json）
    FETCH_COUNT  每次拉取的推文数（默认 60）
    MAX_PER_RUN  单次最多推送条数（默认 10，防止刷屏）
    FIRST_RUN_PUSH  首次运行（无状态文件）是否推送最近的推文（默认 0，只记录不推送）
    DRY_RUN      设为 1 时只打印将要发送的内容，不实际请求 Telegram
    PROXY        本地调试用的 HTTP 代理（如 http://127.0.0.1:7897），Actions 上不用设
"""

import html
import json
import logging
import os
import sys
import time

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

AUTH_TOKEN = os.environ["AUTH_TOKEN"]
CT0 = os.environ["CT0"]
LIST_ID = os.environ["LIST_ID"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FETCH_COUNT = int(os.environ.get("FETCH_COUNT", "60"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "10"))
FIRST_RUN_PUSH = os.environ.get("FIRST_RUN_PUSH", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
PROXY = os.environ.get("PROXY") or None

# X 网页版公开 bearer token（所有用户相同，非私密凭证）
BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
          "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
GQL_URL = "https://x.com/i/api/graphql/HjsWc-nwwHKYwHenbHm-tw/ListLatestTweetsTimeline"
FEATURES = {
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

TEXT_LIMIT = 3500       # sendMessage 上限 4096，留出格式余量
CAPTION_LIMIT = 900     # sendPhoto/sendMediaGroup caption 上限 1024，留出格式余量


# ---------- Telegram ----------

def tg(method: str, **params) -> dict | None:
    if DRY_RUN:
        log.info("[DRY_RUN] %s %s", method, json.dumps(params, ensure_ascii=False)[:500])
        return None
    last_err = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(proxy=PROXY, timeout=30) as c:
                resp = c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params)
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram {method} 失败: {data}")
            return data
        except (httpx.HTTPError, RuntimeError) as e:
            last_err = e
            log.warning("Telegram %s 失败（第 %s 次）: %s", method, attempt, e)
            time.sleep(3 * attempt)
    raise last_err


def alert(text: str) -> None:
    """向群里发一条错误告警（尽力而为，不抛异常）。"""
    try:
        tg("sendMessage", chat_id=CHAT_ID, text=text)
    except Exception:
        log.exception("发送告警失败")


# ---------- X 拉取与解析 ----------

def fetch_list_tweets() -> list[dict]:
    """返回列表时间线上的推文列表（已解析为扁平 dict，旧→新不保证，需按 id 排序）。"""
    headers = {
        "authorization": BEARER,
        "cookie": f"auth_token={AUTH_TOKEN}; ct0={CT0}",
        "x-csrf-token": CT0,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    }
    params = {
        "variables": json.dumps({"listId": LIST_ID, "count": FETCH_COUNT}),
        "features": json.dumps(FEATURES),
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(proxy=PROXY, timeout=30, http2=False) as c:
                r = c.get(GQL_URL, params=params, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"X 接口返回 {r.status_code}: {r.text[:200]}")
            instructions = (r.json()["data"]["list"]
                            ["tweets_timeline"]["timeline"]["instructions"])
            return parse_instructions(instructions)
        except Exception as e:
            last_err = e
            log.warning("拉取列表失败（第 %s 次）: %s", attempt, e)
    raise last_err


def extract_tweet(result: dict) -> dict | None:
    """从 tweet_results.result 里剥出原始推文（转推取被转内容）。"""
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", result)
    if result.get("__typename") != "Tweet":
        return None
    legacy = result.get("legacy") or {}
    if not legacy.get("id_str"):
        return None
    rt = legacy.get("retweeted_status_result", {}).get("result")
    rt_tweet = extract_tweet(rt) if rt else None

    if rt_tweet:
        return {**rt_tweet, "retweeted_by": (result.get("core", {}).get("user_results", {})
                                             .get("result", {}).get("legacy", {}).get("screen_name"))}

    user = (result.get("core", {}).get("user_results", {})
            .get("result", {}).get("legacy", {}))
    media = (legacy.get("extended_entities", {}) or legacy.get("entities", {})).get("media", [])
    return {
        "id": legacy["id_str"],
        "text": legacy.get("full_text") or "",
        "name": user.get("name", ""),
        "handle": user.get("screen_name", ""),
        "photos": [m["media_url_https"] for m in media if m.get("type") == "photo"],
        "has_video": any(m.get("type") in ("video", "animated_gif") for m in media),
    }


def parse_instructions(instructions: list) -> list[dict]:
    tweets = []
    for ins in instructions:
        if ins.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
            continue
        entries = ins.get("entries") or ins.get("moduleItems") or []
        items = []
        for e in entries:
            content = e.get("content") or {}
            if content.get("entryType") == "TimelineTimelineItem" or content.get("type") == "TimelineTimelineItem":
                items.append(content.get("itemContent") or {})
            elif content.get("entryType") == "TimelineTimelineModule":
                items += [i.get("item", {}).get("itemContent") for i in content.get("items", [])]
            elif e.get("type") == "TimelineTimelineModule":  # moduleItems path
                items.append(e.get("item", {}).get("itemContent") or {})
        for ic in items:
            if not ic:
                continue
            result = ic.get("tweet_results", {}).get("result")
            if result:
                t = extract_tweet(result)
                if t:
                    tweets.append(t)
    # 同一条推文可能重复出现（转推+原推），按 id 去重
    seen, out = set(), []
    for t in tweets:
        if t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out


# ---------- 消息格式化与发送 ----------

def tweet_url(t: dict) -> str:
    return f"https://x.com/{t['handle']}/status/{t['id']}"


def header_and_body(t: dict, limit: int) -> tuple[str, str]:
    name = html.escape(t["name"] or "")
    handle = html.escape(t["handle"] or "")
    body = html.escape(t["text"]).strip()
    suffix = f'\n\n<a href="{tweet_url(t)}">🔗 原推</a>'
    room = limit - len(f"<b>{name}</b> @{handle}\n") - len(suffix)
    if len(body) > room:
        body = body[: max(room - 2, 0)] + " …"
    rt = ""
    if t.get("retweeted_by"):
        rt = f'<a href="https://x.com/{html.escape(t["retweeted_by"])}">@{html.escape(t["retweeted_by"])}</a> 🔁转推\n'
    head = f'{rt}<b>{name}</b> <a href="https://x.com/{handle}">@{handle}</a>\n'
    return head, body + suffix


def send_tweet(t: dict) -> None:
    head, body = header_and_body(t, CAPTION_LIMIT)
    photos = t["photos"][:10]

    if photos and not t["has_video"]:
        caption = head + body
        if len(photos) == 1:
            tg("sendPhoto", chat_id=CHAT_ID, photo=photos[0],
               caption=caption, parse_mode="HTML")
        else:
            media = [{"type": "photo", "media": url} for url in photos]
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"
            tg("sendMediaGroup", chat_id=CHAT_ID, media=media)
        return

    _, body = header_and_body(t, TEXT_LIMIT)
    text = head + body
    if t["has_video"]:
        text += "\n🎬 含视频/GIF，点原推链接观看"
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML")


# ---------- 状态 ----------

def load_last_id() -> int | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return int(json.load(f)["last_id"])
    except FileNotFoundError:
        return None


def save_last_id(last_id: int) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(last_id)}, f)
    log.info("状态已写入 %s: last_id=%s", STATE_FILE, last_id)


# ---------- 主流程 ----------

def main() -> None:
    tweets = fetch_list_tweets()
    log.info("拉取到 %s 条推文", len(tweets))
    if not tweets:
        return

    last_id = load_last_id()
    if last_id is None:
        newest = max(int(t["id"]) for t in tweets)
        if FIRST_RUN_PUSH:
            batch = sorted(tweets, key=lambda t: int(t["id"]))[-MAX_PER_RUN:]
            log.info("首次运行，推送最近 %s 条", len(batch))
            for t in batch:
                send_tweet(t)
        else:
            log.info("首次运行，只记录位置 last_id=%s，不推送（设 FIRST_RUN_PUSH=1 可推送最近推文）", newest)
        save_last_id(newest)
        return

    fresh = sorted((t for t in tweets if int(t["id"]) > last_id), key=lambda t: int(t["id"]))
    if not fresh:
        log.info("没有新推文")
        return

    log.info("发现 %s 条新推文，本次推送最多 %s 条", len(fresh), MAX_PER_RUN)
    for t in fresh[:MAX_PER_RUN]:
        send_tweet(t)
        save_last_id(int(t["id"]))   # 逐条落盘，中断也不重发
        log.info("已推送 tweet %s by @%s", t["id"], t["handle"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("运行失败")
        if not DRY_RUN:
            alert(f"⚠️ X 列表推送中断：{html.escape(str(e))[:400]}\n已推送的部分不受影响，下一个周期会自动重试。")
        sys.exit(1)
