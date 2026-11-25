import asyncio
import datetime
import email.utils
import json
import os
import re
import time
import feedparser
import requests
import edge_tts

NEWS_SOURCES = [
    # ------------------ 保留的国际与商业/中立/区域媒体 ------------------
    {
        "name": "法国广播电台 RFI (中文)",
        "url": "https://www.rfi.fr/cn/rss",
    },
    {
        "name": "华尔街日报 WSJ (国际新闻)",
        "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    },
    {
        "name": "南华早报 SCMP (亚洲与世界)",
        "url": "https://www.scmp.com/rss/2/feed",
    },
    {
        "name": "印度时报 Times of India (世界频道)",
        "url": "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms",
    },
    {
        "name": "TechCrunch (科技创投)",
        "url": "https://techcrunch.com/feed/",
    },
    {
        "name": "雅虎财经 Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rss",
    },
    {
        "name": "BBC 中文网",
        "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    },
    {
        "name": "德国之声 DW 中文",
        "url": "https://rss.dw.com/rdf/rss-chi-all",
    },
    {
        "name": "半岛电视台 Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    {
        "name": "法国 24 台 France 24",
        "url": "https://www.france24.com/en/rss",
    },
    {
        "name": "国际通讯社聚合 Google News",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD",
    },

    # ------------------ 保守派/特色视角媒体 ------------------
    {
        "name": "福克斯新闻 Fox News (世界新闻)",
        "url": "https://moxie.foxnews.com/google-publisher/world.xml",
    },
    {
        "name": "纽约邮报 New York Post (新闻)",
        "url": "https://nypost.com/world/feed/",
    },
    {
        "name": "华盛顿时报 The Washington Times (世界新闻)",
        "url": "https://www.washingtontimes.com/rss/headlines/news/world/",
    },
    {
        "name": "Newsmax (全球新闻)",
        "url": "https://www.newsmax.com/rss/Newsfront/16/",
    }
]

# 模型降级备用梯队：优先使用高性价比主模型，遭遇过载自动顺延
CANDIDATE_MODELS = [
    "gemini-3.6-flash",         # 主力：性能与速度平衡
    "gemini-3.5-flash",         # 备选1：官方标准版主力
    "gemini-3.5-flash-lite",    # 备选2：低延迟轻量级，算力充足不易拥堵
    "gemini-3.1-pro-preview"    # 备选3：深度理解版，保底使用
]

VOICE_ZH = "zh-CN-YunyangNeural"  # 微软 Edge TTS 中文新闻男声：云杨
VOICE_EN = "en-US-AndrewNeural"   # 微软 Edge TTS 英文新闻男声：Andrew
HISTORY_FILE = "history_ids.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_history(history_set):
    recent_history = list(history_set)[-200:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))


def fetch_candidate_news(candidate_limit=60):
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个媒体源收集候选资讯...")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            # 严格控制单源网络抓取超时，避免死锁
            resp = session.get(url, timeout=(5, 10))
            if resp.status_code != 200:
                print(f"  - [{name}] 抓取跳过 (HTTP 状态码: {resp.status_code})")
                source_buckets[name] = []
                continue

            feed = feedparser.parse(resp.content)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, "id", getattr(entry, "link", None))
                if entry_id and entry_id not in history:
                    valid_entries.append((entry, entry_id, name))
            source_buckets[name] = valid_entries
            print(f"  - [{name}] 抓取到 {len(valid_entries)} 条候选资讯")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过 (网络或解析异常: {e})")
            source_buckets[name] = []

    candidates = []
    new_ids = set()
    max_loops = max([len(v) for v in source_buckets.values()], default=0)

    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in new_ids:
                    title = getattr(entry, "title", "")
                    summary = entry.summary if hasattr(entry, "summary") else ""
                    candidates.append(f"【来源：{media_name}】 标题: {title}\n摘要: {summary}\n")
                    new_ids.add(entry_id)
                    if len(candidates) >= candidate_limit:
                        break
        if len(candidates) >= candidate_limit:
            break

    history.update(new_ids)
    save_history(history)

    print(f"总计收集到 {len(candidates)} 条候选素材，准备交由大模型提炼...")
    return "\n".join(candidates)


def call_gemini(prompt, api_key, task_name):
    """通用 Gemini 调用器：完整保留四级模型降级与重试保护机制"""
    print(f"正在执行【{task_name}】...")
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    timeout_seconds = 75

    for model_name in CANDIDATE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        print(f"\n🔄 [{task_name}] 尝试调用模型 [{model_name}] ...")

        for attempt in range(2):  # 每个模型最多尝试 2 次
            try:
                print(f"  - 发起请求 (第 {attempt + 1}/2 次)...")
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds
                )
                data = response.json()

                # 1. 成功获取结果
                if "candidates" in data and len(data["candidates"]) > 0:
                    text_parts = data["candidates"][0]["content"]["parts"]
                    if text_parts and "text" in text_parts[0]:
                        print(f"✅ 模型 [{model_name}] 成功完成【{task_name}】！")
                        return text_parts[0]["text"]

                # 2. API 报错处理
                if "error" in data:
                    err_msg = data["error"].get("message", "未知错误")
                    err_code = str(data["error"].get("code", ""))
                    print(f"  ⚠️ [{model_name}] 报错: {err_msg}")

                    # 检测到集群负载高或 503，直接跳出当前模型，无缝进入下一个备选模型
                    if "high demand" in err_msg.lower() or "503" in err_code or "resource has been exhausted" in err_msg.lower():
                        print(f"  ⚡ 检测到 [{model_name}] 算力紧张/配额过载，立即切换至备选模型...")
                        break
                else:
                    print(f"  ⚠️ 返回格式异常: {data}")

            except requests.exceptions.Timeout:
                print(f"  ⚠️ 请求超时 ({timeout_seconds}s)")
            except Exception as e:
                print(f"  ⚠️ 网络异常: {e}")

            # 若需要同模型重试，等待 8 秒
            if attempt == 0:
                print("  ⏳ 等待 8 秒后重试该模型...")
                time.sleep(8)

    raise RuntimeError(f"【{task_name}】所有备选 Gemini 模型均处于拥堵或不可用状态，请检查网络或稍后重新触发。")


def draft_english_broadcast(raw_news, api_key, period_name_en):
    """第一步：直接基于原始素材生成英文主稿"""
    prompt = f"""
You are a rigorous, professional international radio news anchor (similar to BBC World Service or NPR).
Below is raw news material collected from multiple international media sources:

【Core Tasks & Requirements】:
1. Select 30 to 35 independent major news stories:
   - Carefully cross-check the material; strictly prohibit reporting the same event twice.
   - Prioritize the following topics if present in the material (do NOT fabricate if absent; fill remaining slots with other major global headlines): Russia-Ukraine war, US-Iran tensions, Israel war / Middle East conflict, Donald Trump (accurately distinguish whether he is current or former president), Xi Jinping, Elon Musk, and Artificial Intelligence (AI).
   - Include 1 to 2 sports news items at the very end.
   - Content filtering: Exclude stories from outlets widely recognized as partisan far-left media.

2. Date & Source Formatting:
   - Do NOT mention the news agency or media source name.
   - Start each story with the news date (month and day only, no year, e.g., "On November 26," or "Reporting on November 26,").

3. Opening, Closing & Zero Transition Fluff:
   - Open with a brief greeting and today's date (e.g., "Welcome to the Global News Briefing. Today is [Month] [Day].").
   - Strictly forbid any broadcast transition clichés between stories (NEVER use "Moving on to our next story", "In other news", "Meanwhile", etc.). Start each item directly with the date and facts.
   - End with a brief sign-off (e.g., "That concludes this global news briefing. Thank you for listening.").

4. Strict Objectivity & Plain Text Only:
   - Stick strictly to objective facts (time, location, parties involved, events, and official statements). If a raw news item is overly brief, flesh out established background facts of the event without speculation, subjective adjectives, or AI commentary.
   - Pure plain text only: strictly NO Markdown formatting (no **, #), NO parentheses/brackets, NO URLs, and NO special symbols.

5. Length Control:
   - Keep each story around 90 to 110 English words, with a total length of roughly 2,600 to 3,000 English words. Ensure the script is complete and never truncated.

Raw news material:
{raw_news}
"""
    return call_gemini(prompt, api_key, f"{period_name_en}-英文主稿生成")


def translate_to_chinese(en_script, api_key, period_name_zh):
    """第二步：将英文主稿一对一精准译配为中文电台播报稿"""
    prompt = f"""
你是一位严谨、专业的国家级新闻广播电台播音员。
请将以下英文国际新闻广播稿，精准翻译并润色为客观、地道、适合中文新闻电台口播的纯文本稿件。

【核心要求】：
1. 100% 忠实原文与结构：
   - 必须与英文原稿的新闻条数、排列顺序、核心事实、数据及官方表态完全一致，严禁增删、合并或凭空发挥。
2. 符合中文新闻电台播报规范：
   - 开篇保留简短问候及播报当日时间（例如：“欢迎收听国际要闻，今天是*月*日。”）。
   - 每条新闻不需要交代消息来源，直接以“*月*日消息，”开头展开事实（不需要年份）。
   - 严禁使用任何广播转场套话（绝对不要出现“下一条消息”、“另一项国际动态是”、“下面关注”、“与此同时”等过渡词）。
   - 结尾简短致谢收尾（例如：“以上是本次国际要闻播报，感谢收听。”）。
3. 纯文本输出：
   - 严禁出现 Markdown 标记（如 **、#）、括号、网址或特殊符号，确保可直接用于语音合成（TTS）。

待翻译的英文播报稿如下：
{en_script}
"""
    return call_gemini(prompt, api_key, f"{period_name_zh}-中文稿译配")


async def text_to_speech(text, output_file, voice):
    print(f"正在合成音频 [{voice}]: {output_file} ...")
    communicate = edge_tts.Communicate(text, voice)
    # 为音频合成加上 240 秒保护，防止 WebSocket 假死
    await asyncio.wait_for(communicate.save(output_file), timeout=240)


def update_podcast_feed(feed_path, channel_title, language, channel_desc, item_desc, audio_url, audio_size, episode_title):
    print(f"正在增量更新 {feed_path} ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    os.makedirs(os.path.dirname(feed_path), exist_ok=True)

    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>{item_desc}</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{audio_size}" type="audio/mpeg"/>
      <guid>{audio_url}</guid>
    </item>"""

    existing_items = []
    if os.path.exists(feed_path):
        with open(feed_path, "r", encoding="utf-8") as f:
            content = f.read()
            existing_items = re.findall(r"(<item>.*?</item>)", content, re.DOTALL)

    all_items = [new_item] + existing_items[:9]
    items_block = "\n".join(all_items)

    feed_template = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{channel_title}</title>
    <link>https://github.com</link>
    <language>{language}</language>
    <description>{channel_desc}</description>
{items_block}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_template)


async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    repo = os.getenv("GITHUB_REPOSITORY")

    if not api_key:
        raise ValueError("缺少环境变量 GEMINI_API_KEY")

    bj_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")

    hour = bj_time.hour
    period_name_zh = "早间新闻" if hour < 12 else "晚间新闻"
    period_name_en = "Morning Briefing" if hour < 12 else "Evening Briefing"

    episode_title_zh = f"{period_name_zh} ({date_display})"
    episode_title_en = f"{period_name_en} ({date_display})"

    tag = f"episode-{time_str}"
    audio_zh = f"news-zh-{time_str}.mp3"
    audio_en = f"news-en-{time_str}.mp3"

    # 1. 抓取候选池（带超时）
    raw_news = fetch_candidate_news(candidate_limit=60)
    if not raw_news.strip():
        print("未抓取到有效新闻素材，跳过本次生成。")
        return

    # 2. 先直接生成英文主稿，再一对一译配为中文播报稿（多模型降级轮询）
    en_script = draft_english_broadcast(raw_news, api_key, period_name_en)
    zh_script = translate_to_chinese(en_script, api_key, period_name_zh)

    # 3. 并发合成中英文双语音频（各自带 240s 异步超时保护）
    await asyncio.gather(
        text_to_speech(en_script, audio_en, VOICE_EN),
        text_to_speech(zh_script, audio_zh, VOICE_ZH),
    )

    # 4. 校验双音频均成功生成且非空后，再写入中间标记文件供工作流读取
    size_zh = os.path.getsize(audio_zh)
    size_en = os.path.getsize(audio_en)
    if size_zh == 0 or size_en == 0:
        raise RuntimeError(f"音频文件大小异常 (zh={size_zh}, en={size_en})，终止发布。")

    with open("current_tag.txt", "w", encoding="utf-8") as f:
        f.write(tag)
    with open("current_audio_zh.txt", "w", encoding="utf-8") as f:
        f.write(audio_zh)
    with open("current_audio_en.txt", "w", encoding="utf-8") as f:
        f.write(audio_en)

    # 5. 发布双语单集至各自独立的 RSS
    base_release_url = f"https://github.com/{repo}/releases/download/{tag}"

    update_podcast_feed(
        feed_path="public/feed.xml",
        channel_title="每日国际要闻速递",
        language="zh-cn",
        channel_desc="每日自动汇总全球热点 30 条要闻，AI 严谨播报。",
        item_desc="今日聚合全球权威要闻 30 条客观播报。",
        audio_url=f"{base_release_url}/{audio_zh}",
        audio_size=size_zh,
        episode_title=episode_title_zh,
    )

    update_podcast_feed(
        feed_path="public/feed-en.xml",
        channel_title="Daily Global News Briefing",
        language="en-us",
        channel_desc="Daily objective audio briefing of 30 major international news stories.",
        item_desc="Objective audio briefing covering 30 major global headlines.",
        audio_url=f"{base_release_url}/{audio_en}",
        audio_size=size_en,
        episode_title=episode_title_en,
    )

    print("本期中英文双语要闻播客制作完成！")


if __name__ == "__main__":
    asyncio.run(main())
