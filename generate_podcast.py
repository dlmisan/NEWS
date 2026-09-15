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
    {
        "name": "纽约时报 (中文版)",
        "url": "https://cn.nytimes.com/rss/",
        # 优势：深度国际时政分析，对美国大选和中东局势有极高价值的深度报道
    },
    {
        "name": "法国广播电台 RFI (中文)",
        "url": "https://www.rfi.fr/cn/rss",
        # 优势：欧洲视角，对俄乌战争和欧洲地缘政治报道非常及时
    },
    {
        "name": "CNN (世界新闻)",
        "url": "http://rss.cnn.com/rss/edition_world.rss",
        # 优势：突发新闻极快，美国视角的全球要闻
    },
    {
        "name": "纽约时报 NYT (世界新闻)",
        "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        # 优势：全球最具影响力的报纸之一，事实核查严谨
    },
    {
        "name": "华盛顿邮报 WP (世界新闻)",
        "url": "https://feeds.washingtonpost.com/rss/world",
        # 优势：离白宫最近的媒体，对美国外交政策、军事情报报道独道
    },
    {
        "name": "华尔街日报 WSJ (国际新闻)",
        "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        # 优势：兼顾地缘政治与宏观经济，对制裁、能源危机报道精准
    },
    {
        "name": "英国卫报 The Guardian (国际)",
        "url": "https://www.theguardian.com/world/rss",
        # 优势：免费且高质量的英国左翼大报，对气候、人权、中东有大量报道
    },
    {
        "name": "南华早报 SCMP (亚洲与世界)",
        "url": "https://www.scmp.com/rss/2/feed",
        # 优势：立足香港，全英文播报亚洲与全球宏观动态
    },
    {
        "name": "印度时报 Times of India (世界频道)",
        "url": "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms",
        # 优势：南亚最大的英文媒体，提供“全球南方”国家的独特视角
    },
    {
        "name": "TechCrunch (科技创投)",
        "url": "https://techcrunch.com/feed/",
        # 优势：全球科技圈第一手资讯，涉及马斯克、OpenAI等AI巨头动态必看
    },
    {
        "name": "雅虎财经 Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rss",
        # 优势：聚合了彭博社、路透社的大量财经和政商跨界新闻
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
        "name": "英国广播公司 BBC (国际)",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
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
        "name": "美国国家公共电台 NPR",
        "url": "https://feeds.npr.org/1004/rss.xml",
    },
    {
        "name": "国际通讯社聚合 Google News",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD",
    },
]

VOICE = "zh-CN-YunyangNeural"  # 微软 Edge TTS 推荐新闻男声：云杨
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


# 扩大抓取池到 60 条，确保筛选 30 条独立事件时素材充足
def fetch_candidate_news(candidate_limit=60):
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个媒体源收集候选资讯...")

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            # 【修复1】强制传入 User-Agent，伪装成浏览器，防止被直接 403 拦截
            feed = feedparser.parse(url, agent=USER_AGENT)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, "id", entry.link)
                if entry_id not in history:
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
                    title = entry.title
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


def rewrite_with_gemini(raw_news, api_key, period_name, max_retries=3):
    print(f"正在生成【{period_name}】客观口播稿...")
    prompt = f"""
你是一位严谨、专业的国家级新闻广播电台播音员。
以下是从多家国际权威媒体收集的新闻素材：

【核心任务与要求】：
1. 筛选 25 至 30 条独立重大要闻：
   - 仔细比对素材，若多家媒体报道同一事件，必须融合成一条（可注明“据综合消息”），严禁同一事件分两段播报。
   - 优先关注并入选以下主题：俄乌战争、美国伊朗局势、以色列战争/中东冲突、特朗普（注意分辨特朗普是现任总统还是前总统）、习近平、马斯克、AI（素材中若有则优先入选；若无此类报道则按其他重大要闻顺延，严禁凭空编造）。
   - 新闻最后可加入1-2条体育消息。
   - 内容过滤：无。

2. 严格保留新闻来源：
   - 每条新闻必须明确交代消息来源（例如：“据英国广播公司报道……”、“德国之声报道称……”、“据美国国家公共电台消息……”）。

3. 彻底删除转场套话与时间播报：
   - 开篇仅作简短问候（例如：“听众朋友们好，欢迎收听国际要闻{period_name}。”），严禁播报具体的当前时间或日期。
   - 严禁使用任何广播转场套话（绝对不要出现“下一条消息”、“另一项国际动态是”、“下面关注”、“与此同时”等过渡词），每条新闻直接以来源和事实展开播报。
   - 结尾简短致谢收尾（例如：“以上是本次国际要闻播报，感谢收听。”）。

4. 绝对忠实原稿，杜绝主观发挥：
   - 严禁加入任何未经素材提及的推测、主观分析、形容词或 AI 观点，仅客观复述事实（时间、地点、主体、发生情况与官方表态）。
   - 纯文本输出：严禁出现 Markdown 标记（如 **、#）、括号、网址或特殊符号。

5. 篇幅：每条新闻保留核心事实，全篇总字数控制在 4000~4800 字左右，确保结构完整，切勿中途截断。

原始新闻素材如下：
{raw_news}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    # 【新增】自动重试循环
    for attempt in range(max_retries):
        try:
            # 保持 timeout=360，防止无响应挂起
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=360)
            data = response.json()
            
            # 1. 成功情况：如果顺利拿到 candidates，直接返回结果
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
                
            # 2. 失败情况A：API 返回了内容，但提示报错（如 503 拥堵）
            if "error" in data:
                error_msg = data['error'].get('message', '未知 API 错误')
                print(f"⚠️ 第 {attempt + 1} 次请求失败: API 报错 - {error_msg}")
            else:
                print(f"⚠️ 第 {attempt + 1} 次请求失败: 返回格式异常 - {data}")
                
        # 3. 失败情况B：网络连接问题或超时抛出异常
        except Exception as e:
            print(f"⚠️ 第 {attempt + 1} 次请求发生网络异常: {e}")
            
        # 如果还没到最后一次尝试，就等待 30 秒再试
        if attempt < max_retries - 1:
            print(f"⏳ 等待 30 秒后自动进行第 {attempt + 2} 次重试...\n")
            time.sleep(30)
            
    # 如果 3 次全失败了，就抛出致命错误结束程序
    raise RuntimeError(f"Gemini API 严重拥堵或异常，{max_retries} 次重试后依然失败，请稍后手动运行。")


async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)


def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)

    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日聚合全球权威要闻 30 条客观播报。</description>
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
    <title>每日国际要闻速递</title>
    <link>https://github.com</link>
    <language>zh-cn</language>
    <description>每日自动汇总全球热点 30 条要闻，AI 严谨播报。</description>
{items_block}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_template)


async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    repo = os.getenv("GITHUB_REPOSITORY")

    bj_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")

    hour = bj_time.hour
    if hour < 12:
        period_name = "早间新闻"
    else:
        period_name = "晚间新闻"
    episode_title = f"{period_name} ({date_display})"
    tag = f"episode-{time_str}"
    audio_filename = f"news-{time_str}.mp3"

    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 抓取候选池（扩大至 60 条候选）
    raw_news = fetch_candidate_news(candidate_limit=60)

    # 2. 改写为 30 条纯客观事实播报稿（带自动重试机制）
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)

    # 3. 合成音频
    await text_to_speech(broadcast_script, audio_filename)

    # 4. 发布单集
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("本期 30 条要闻播客制作完成！")


if __name__ == "__main__":
    asyncio.run(main())
