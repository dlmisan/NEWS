import asyncio
import datetime
import email.utils
import json
import os
import re
import time
import edge_tts
import feedparser
import requests

# ================= 新闻源配置（精选自带丰富正文摘要的权威直连源） =================
NEWS_SOURCES = [
    # ---------- 【猎奇 / 奇闻趣事】 ----------
    {
        "name": "合众国际社 UPI (奇闻)",
        "url": "https://rss.upi.com/news/odd_news.rss",
        # 优势：每条 RSS 均自带 200 词以上的完整英文故事细节
    },
    {
        "name": "赫芬顿邮报 HuffPost (全球奇闻)",
        "url": "https://www.huffpost.com/section/weird-news/feed",
        # 优势：报道极其详尽，包含大量当事人采访引言与起因细节
    },
    {
        "name": "英国卫报 The Guardian (奇异趣闻)",
        "url": "https://www.theguardian.com/world/weird/rss",
    },
    # ---------- 【娱乐 / 影视 / 流行文化】 ----------
    {
        "name": "Variety (综艺杂志)",
        "url": "https://variety.com/feed/",
    },
    {
        "name": "Deadline (好莱坞前线)",
        "url": "https://deadline.com/feed/",
    },
    {
        "name": "BBC 娱乐与艺术",
        "url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    },
    {
        "name": "好莱坞报道 Hollywood Reporter",
        "url": "https://www.hollywoodreporter.com/feed/",
    },
    {
        "name": "Billboard (音乐公告牌)",
        "url": "https://www.billboard.com/feed/",
    },
    {
        "name": "联合早报 (娱乐与文化)",
        "url": "https://www.zaobao.com.sg/rss/lifestyle/entertainment",
    },
    # ---------- 【体育 / 竞技焦点】 ----------
    {
        "name": "BBC Sport (国际体育)",
        "url": "https://feeds.bbci.co.uk/sport/rss.xml",
    },
    {
        "name": "英国卫报 The Guardian (体育专栏)",
        "url": "https://www.theguardian.com/sport/rss",
        # 优势：包含大篇幅的比赛复盘、比分和球星赛后发言
    },
    {
        "name": "天空体育 Sky Sports",
        "url": "https://www.skysports.com/rss/12040",
    },
    {
        "name": "联合早报 (体坛风云)",
        "url": "https://www.zaobao.com.sg/rss/sports",
    },
]

VOICE = "zh-CN-YunyangNeural"
HISTORY_FILE = "ent_history_ids.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_history(history_set):
    recent_history = list(history_set)[-300:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))


# 对标国际新闻：抓取池扩大至 60 条，确保素材信息总量充足
def fetch_candidate_news(candidate_limit=60):
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个文体奇闻源收集候选资讯...")

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, "id", entry.link)
                if entry_id not in history:
                    valid_entries.append((entry, entry_id, name))
            source_buckets[name] = valid_entries
            print(f"  - [{name}] 抓取到 {len(valid_entries)} 条候选线索")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过: {e}")
            source_buckets[name] = []

    candidates = []
    new_ids = set()
    max_loops = max([len(v) for v in source_buckets.values()], default=0)

    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in new_ids:
                    title = entry.title
                    summary = entry.summary if hasattr(entry, "summary") else ""
                    clean_summary = re.sub(r"<[^>]+>", "", summary).strip()
                    # 仅保留包含实质内容的有效条目
                    if len(clean_summary) > 20:
                        candidates.append(
                            f"【来源：{media_name}】 标题: {title}\n摘要详情: {clean_summary}\n"
                        )
                        new_ids.add(entry_id)
                    if len(candidates) >= candidate_limit:
                        break
        if len(candidates) >= candidate_limit:
            break

    history.update(new_ids)
    save_history(history)

    print(
        f"总计收集到 {len(candidates)} 条高质量候选素材，准备交由大模型提炼..."
    )
    return "\n".join(candidates)


def rewrite_with_gemini(raw_news, api_key, period_name, max_retries=3):
    print(f"正在生成【{period_name}】长篇口播稿（目标 4000~4500 字）...")

    prompt = f"""
你是一位极具专业素养、叙事生动且语言功底深厚的新闻广播播音员。
以下是从全球主流媒体收集的【奇闻趣事、影视娱乐与体育焦点】真实新闻素材：

【核心任务与要求】：
1. 筛选并整合成 20 至 25 条独立事件：
   - 均衡覆盖三大板块：
     * 环球奇闻与新鲜事（7-8条）：包含离奇遭遇、冷门纪录、动植物趣事、奇特自然现象。
     * 影视娱乐与流行文化（6-8条）：新片定档、票房成绩、音乐动态、影展及明星名流资讯。
     * 体坛风云与赛事焦点（6-8条）：足球联赛、篮球焦点大战、大满贯赛事、选手赛场表现。
   - 若多家媒体报道同一事件，必须融合成一条完整的报道，严禁拆分。

2. 篇幅与字数硬性指标（核心要求）：
   - 全篇总字数必须严格控制在 4000 至 4500 汉字之间，确保结构完整，切勿中途截断！
   - 坚决杜绝一句话带过的简讯！请充分利用素材中的事实细节展开播报：交代事件的起因原委、具体时间地点、关键数字（比分、票房、金额、尺寸等）、当事人回应或现场情况。
   - 每一条新闻独立成段，单条篇幅展开至 180 到 220 字左右，确保内容充实饱满。

3. 严格保留新闻来源与客观性：
   - 每条新闻开篇必须明确交代消息来源（例如：“据合众国际社报道……”、“英国卫报消息称……”、“据好莱坞报道披露……”）。
   - 仅客观复述素材提供的事实，严禁主观臆想或凭空捏造事实。

4. 彻底删除转场套话：
   - 开篇直接播报简短日期与节目名（例如：“听众朋友们好，欢迎收听全球奇闻、娱乐与体育速递。”）。
   - 严禁使用任何生硬广播转场词（绝对不要出现“下一条消息”、“下面关注”、“与此同时”等过渡词），每条新闻直接以来源和事实展开。
   - 结尾简短致谢收尾（例如：“以上是本次奇闻文体速递，感谢收听，下期再见。”）。

5. 纯文本输出：
   - 严禁出现 Markdown 标记（如 **、#）、阿拉伯数字序号标头、括号、网址或特殊符号。

原始新闻素材如下：
{raw_news}
"""
    # 请求结构与国际新闻脚本完全对齐
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for attempt in range(max_retries):
        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=360,
            )
            data = response.json()

            if "candidates" in data and data["candidates"]:
                text_result = data["candidates"][0]["content"]["parts"][0][
                    "text"
                ]
                print(
                    f"✅ 口播稿生成成功！实际总字数：{len(text_result)} 字。"
                )
                return text_result

            if "error" in data:
                error_msg = data["error"].get("message", "未知 API 错误")
                print(
                    f"⚠️ 第 {attempt + 1} 次请求失败: API 报错 - {error_msg}"
                )
            else:
                print(
                    f"⚠️ 第 {attempt + 1} 次请求失败: 返回格式异常 - {data}"
                )

        except Exception as e:
            print(f"⚠️ 第 {attempt + 1} 次请求发生网络异常: {e}")

        if attempt < max_retries - 1:
            print(f"⏳ 等待 30 秒后自动进行第 {attempt + 2} 次重试...\n")
            time.sleep(30)

    raise RuntimeError(
        f"Gemini API 严重拥堵或异常，{max_retries} 次重试后依然失败。"
    )


async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)


def update_podcast_feed(audio_url, audio_size, episode_title, repo):
    print("正在增量合并至公共订阅源 public/feed.xml ...")
    pub_date = email.utils.format_datetime(
        datetime.datetime.now(datetime.timezone.utc)
    )
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)

    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日全球奇闻趣事、影视文娱与体坛焦点 20-25 条客观深入播报。</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{audio_size}" type="audio/mpeg"/>
      <guid>{audio_url}</guid>
    </item>"""

    content = ""
    if os.path.exists(feed_path):
        with open(feed_path, "r", encoding="utf-8") as f:
            content = f.read()

    if not content and repo:
        owner, repo_name = repo.split("/")
        urls_to_try = [
            f"https://raw.githubusercontent.com/{repo}/gh-pages/feed.xml",
            f"https://{owner}.github.io/{repo_name}/feed.xml",
        ]
        for online_url in urls_to_try:
            try:
                resp = requests.get(online_url, timeout=10)
                if resp.status_code == 200 and "<rss" in resp.text:
                    content = resp.text
                    print(f"✅ 成功从线上继承 feed.xml: {online_url}")
                    break
            except Exception:
                pass

    existing_items = []
    if content:
        existing_items = re.findall(r"(<item>.*?</item>)", content, re.DOTALL)
        existing_items = [
            item for item in existing_items if audio_url not in item
        ]

    all_items = [new_item] + existing_items[:24]
    items_block = "\n".join(all_items)

    feed_template = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>每日全球速递（要闻与奇闻文体）</title>
    <link>https://github.com/{repo}</link>
    <language>zh-cn</language>
    <description>每日自动汇总全球权威要闻、奇闻怪事、影视文娱与体坛焦点。</description>
{items_block}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_template)
    print(f"✅ public/feed.xml 已更新，当前共有 {len(all_items)} 期单集。")


async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    repo = os.getenv("GITHUB_REPOSITORY")

    bj_time = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")

    episode_title = f"【奇闻娱乐】全球奇闻娱乐与体育速递 ({date_display})"
    tag = f"ent-episode-{time_str}"
    audio_filename = f"ent-{time_str}.mp3"

    with open("ent_current_tag.txt", "w") as f:
        f.write(tag)
    with open("ent_current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 抓取候选池（扩大至 60 条有效信息素材）
    raw_news = fetch_candidate_news(candidate_limit=60)

    # 2. 改写为 4000~4500 字口播稿
    broadcast_script = rewrite_with_gemini(
        raw_news, api_key, "奇闻文体速递"
    )

    # 3. 合成音频
    await text_to_speech(broadcast_script, audio_filename)

    # 4. 发布单集
    file_size = os.path.getsize(audio_filename)
    audio_url = (
        f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    )
    update_podcast_feed(audio_url, file_size, episode_title, repo)
    print("🎉 本期文体奇闻播客制作完成！")


if __name__ == "__main__":
    asyncio.run(main())
