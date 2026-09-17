import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import email.utils
import json
import os
import re
import time
import edge_tts
import feedparser
import requests
import trafilatura

# ================= 新闻源配置（猎奇 / 娱乐 / 体育） =================
NEWS_SOURCES = [
    # ---------- 【猎奇 / 奇闻趣事】 ----------
    {
        "name": "合众国际社 UPI (奇闻频道)",
        "url": "https://rss.upi.com/news/odd_news.rss",
    },
    {
        "name": "英国 Metro (奇闻频道)",
        "url": "https://metro.co.uk/news/weird/feed/",
    },
    {
        "name": "新德里电视台 NDTV (Offbeat 趣闻)",
        "url": "https://www.ndtv.com/rss/offbeat.xml",
    },
    {
        "name": "Google News (全球奇闻聚合)",
        "url": "https://news.google.com/rss/search?q=odd+news+OR+weird+news+when:2d&hl=en-US&gl=US&ceid=US:en",
    },
    # ---------- 【娱乐 / 影视 / 流行文化】 ----------
    {"name": "Variety (综艺杂志)", "url": "https://variety.com/feed/"},
    {
        "name": "The Hollywood Reporter (好莱坞报道)",
        "url": "https://www.hollywoodreporter.com/feed/",
    },
    {"name": "Deadline (好莱坞前沿)", "url": "https://deadline.com/feed/"},
    {
        "name": "BBC 娱乐与艺术",
        "url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    },
    {"name": "Billboard (公告牌)", "url": "https://www.billboard.com/feed/"},
    {
        "name": "UPI (全球娱乐要闻)",
        "url": "https://rss.upi.com/news/entertainment_news.rss",
    },
    {
        "name": "Google News (娱乐中文聚合)",
        "url": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
    # ---------- 【体育 / 竞技焦点】 ----------
    {
        "name": "ESPN (全球体育权威)",
        "url": "https://www.espn.com/espn/rss/news",
    },
    {
        "name": "BBC Sport (BBC 体育)",
        "url": "https://feeds.bbci.co.uk/sport/rss.xml",
    },
    {
        "name": "Yahoo Sports (雅虎体育)",
        "url": "https://sports.yahoo.com/rss/",
    },
    {"name": "Sky Sports (天空体育)", "url": "https://www.skysports.com/rss/12040"},
    {
        "name": "Google News (体育中文聚合)",
        "url": "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
]

VOICE = "zh-CN-YunyangNeural"  # 微软 Edge TTS 新闻男声：云杨
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


def fetch_full_text_single(item):
    """
    单篇新闻正文多线程抓取函数（带严格 5 秒超时保护）
    如果网页正文提取成功，截取前 1500 字符；若失败或反爬，自动回退到 RSS 摘要
    """
    entry, entry_id, media_name = item
    title = entry.title
    link = entry.link
    raw_summary = getattr(entry, "summary", "")

    full_text = ""
    try:
        # 设置 5 秒严格超时，防止单个慢网站拖慢整个脚本
        resp = requests.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=5,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            extracted = trafilatura.extract(
                resp.content,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            )
            if extracted and len(extracted) > 120:
                # 截取前 1500 字，足以包含事件经过、数据、原话细节，同时节约处理开销
                full_text = extracted[:1500]
    except Exception:
        pass

    # 优先采用深度网页正文；若提取失败，无缝回退到原本的 RSS summary
    final_content = full_text if full_text else raw_summary
    clean_content = re.sub(r"<[^>]+>", "", final_content).strip()

    formatted_article = f"【来源：{media_name}】 标题: {title}\n详细事实报道:\n{clean_content}\n"
    return formatted_article, entry_id


def fetch_candidate_news(candidate_limit=26):
    """
    1. 从各大源筛选出未播报过的候选文章
    2. 使用多线程并发提取网页深度全文，5~8 秒即可完成全部抓取
    """
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个文体奇闻源抓取最新动态...")

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
            print(f"  - [{name}] 获取到 {len(valid_entries)} 条新线索")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过: {e}")
            source_buckets[name] = []

    # 轮流从各分类挑选条目，保证奇闻、娱乐、体育分布均衡
    to_fetch_items = []
    seen_ids = set()
    max_loops = max([len(v) for v in source_buckets.values()], default=0)

    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in seen_ids:
                    to_fetch_items.append((entry, entry_id, media_name))
                    seen_ids.add(entry_id)
                    if len(to_fetch_items) >= candidate_limit:
                        break
        if len(to_fetch_items) >= candidate_limit:
            break

    print(
        f"🚀 正在启动 15 个并发线程深度抓取 {len(to_fetch_items)} 篇新闻网页正文..."
    )
    start_time = time.time()

    candidates = []
    new_ids = set()

    # 采用 15 个并发线程同时发起请求
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [
            executor.submit(fetch_full_text_single, item)
            for item in to_fetch_items
        ]
        for future in as_completed(futures):
            try:
                formatted_article, entry_id = future.result()
                candidates.append(formatted_article)
                new_ids.add(entry_id)
            except Exception as e:
                print(f"  - 单篇抓取异常: {e}")

    cost_seconds = round(time.time() - start_time, 2)
    print(
        f"✅ 全文抓取完成！共耗时仅 {cost_seconds} 秒，成功收录 {len(candidates)} 篇深度报道素材。"
    )

    history.update(new_ids)
    save_history(history)
    return "\n".join(candidates)


def rewrite_with_gemini(raw_news, api_key, period_name, max_retries=3):
    print(
        f"正在调用 Gemini 提炼【{period_name}】深度长篇口播稿（目标 4000~4500 字）..."
    )

    prompt = f"""
你是一位极具专业素养、叙事生动且严谨客观的金牌电台广播播音员。
以下是从全球主流媒体、体育专栏及奇闻频道爬取到的【深度新闻报道全文素材】。这些素材包含了丰富的事件原委、具体数据、当事人原话以及现场细节。请将其整合成一篇高质量的【全球奇闻、娱乐与体育】口播播报稿。

【核心任务与要求】：
1. 筛选并整合成 20 至 22 条独立事件：
   - 内容均衡覆盖三大板块：
     * 【环球奇闻与新鲜事】约 7-8 条（离奇事件、冷门纪录、动植物趣事、奇特自然现象）。
     * 【影视文娱与流行文化】约 6-7 条（新片热剧动态、音乐榜单、颁奖季、明星名流趣事）。
     * 【体坛风云与赛事焦点】约 6-7 条（国际足球、篮球、网球大满贯、极限赛事及球星赛场表现）。
   - 若有多家媒体报道同一事件，必须整合为一条详实的报道，严禁分拆。

2. 严格保留新闻来源：
   - 每条新闻开篇必须自然交代媒体来源（例如：“据合众国际社奇闻版块报道……”、“《好莱坞报道》披露……”、“据 ESPN 消息……”）。

3. 充分利用素材事实，杜绝简讯（字数硬性指标）：
   - 全文总字数必须严格保持在 4000 至 4500 汉字之间！
   - 严禁写成一两句话的一笔带过式短讯。由于素材已提供详实的报道正文，请充分引用素材中的【事件前因后果、关键数据/比分/金额、当事人原话表态、官方裁决、现场细节】，将每一条事件写成 180 至 220 字的完整饱满报道。
   - 必须完全忠实于提供的素材事实，严禁凭空捏造虚假信息。

4. 语言风格与结构：
   - 语调生动富有画面感，通俗流畅，完全适合广播听觉收听。
   - 严禁使用陈旧生硬的广播转场套话（绝对不要出现“下一条消息是”、“下面关注”、“让我们把视线转向”、“与此同时”等过渡句），每条直接以消息来源和核心事实切入。
   - 开篇直接播报简短日期和栏目名称（例如：“听众朋友们好，今天是某月某日，欢迎收听全球奇闻娱乐与体育速递。”），不讲废话客套。
   - 结尾简短有力收尾（例如：“播报完毕，感谢收听，我们下期再见。”）。
   - 严禁中途截断，必须输出到结束语为止。

5. 纯文本格式输出：
   - 严禁出现任何 Markdown 标记（绝对不要出现 **、#、*、-），不要输出阿拉伯数字序号（直接自然换行分段），不要输出任何括号、链接或英文符号，输出直接可供机器朗读的纯中文文本。

原始素材内容如下：
{raw_news}
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.65,  # 兼顾生动性与新闻真实性
        },
    }

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
                word_count = len(text_result)
                print(
                    f"✅ 口播稿生成成功！当前总字数：{word_count} 字。"
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
            print(f"⚠️ 第 {attempt + 1} 次请求发生异常: {e}")

        if attempt < max_retries - 1:
            print(f"⏳ 等待 30 秒后进行第 {attempt + 2} 次重试...\n")
            time.sleep(30)

    raise RuntimeError(
        f"Gemini API 响应失败或超时，已达最大重试次数（{max_retries} 次）。"
    )


async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)


def update_podcast_feed(audio_url, audio_size, episode_title, repo):
    """增量合并至公共主播客订阅源 public/feed.xml"""
    print("正在增量合并至公共订阅源 public/feed.xml ...")
    pub_date = email.utils.format_datetime(
        datetime.datetime.now(datetime.timezone.utc)
    )
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)

    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日全球奇闻趣事、影视文娱与体坛焦点 20-25 条深度客观播报。</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{audio_size}" type="audio/mpeg"/>
      <guid>{audio_url}</guid>
    </item>"""

    content = ""

    # 1. 优先读取本地已有的 feed.xml
    if os.path.exists(feed_path):
        with open(feed_path, "r", encoding="utf-8") as f:
            content = f.read()

    # 2. 如果本地不存在（Actions 干净环境），自动拉取线上已发布的最新 feed.xml
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
                    print(
                        f"✅ 成功从线上拉取并继承现有的 feed.xml: {online_url}"
                    )
                    break
            except Exception:
                pass

    existing_items = []
    if content:
        existing_items = re.findall(r"(<item>.*?</item>)", content, re.DOTALL)
        existing_items = [
            item for item in existing_items if audio_url not in item
        ]

    # 合并新单集并保留最近 25 期历史
    all_items = [new_item] + existing_items[:24]
    items_block = "\n".join(all_items)

    feed_template = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>每日全球速递（要闻与奇闻文体）</title>
    <link>https://github.com/{repo}</link>
    <language>zh-cn</language>
    <description>每日汇聚全球权威要闻、离奇趣事、影视文娱与体坛焦点。</description>
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

    # 单集标题统一带前缀
    episode_title = f"【奇闻娱乐】全球奇闻娱乐与体育速递 ({date_display})"
    tag = f"ent-episode-{time_str}"
    audio_filename = f"ent-{time_str}.mp3"

    with open("ent_current_tag.txt", "w") as f:
        f.write(tag)
    with open("ent_current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 抓取候选池（带多线程正文提取）
    raw_news = fetch_candidate_news(candidate_limit=26)

    # 2. 改写为 4000~4500 字口播稿
    broadcast_script = rewrite_with_gemini(
        raw_news, api_key, "全球奇闻娱乐与体育速递"
    )

    # 3. 合成音频
    await text_to_speech(broadcast_script, audio_filename)

    # 4. 合并入统一的 public/feed.xml
    file_size = os.path.getsize(audio_filename)
    audio_url = (
        f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    )
    update_podcast_feed(audio_url, file_size, episode_title, repo)
    print("🎉 本期文体奇闻已成功发布并合流到主播客源！")


if __name__ == "__main__":
    asyncio.run(main())
