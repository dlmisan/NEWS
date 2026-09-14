import os
import re
import asyncio
import datetime
import email.utils
import json
import requests
import feedparser
import edge_tts

# ==========================================
# 1. 在这里配置你的新闻源（可随时增减）
# ==========================================
NEWS_SOURCES = [
    {
        "name": "BBC 国际新闻",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml"
    },
    {
        "name": "德国之声 DW",
        "url": "https://rss.dw.com/rdf/rss-en-world"
    },
    {
        "name": "NPR 全球要闻",
        "url": "https://feeds.npr.org/1004/rss.xml"
    },
    {
        "name": "国际通讯社聚合",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD"
    }
]

# VOICE = "zh-CN-YunxiNeural"  # 微软 Edge TTS 推荐新闻男声：云希
VOICE = "zh-CN-YunyangNeural"  # 微软 Edge TTS 推荐新闻男声：云杨
HISTORY_FILE = "history_ids.txt"

# 读取历史已播新闻，防止内容重复
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

# 保存已播记录，仅保留最近 100 条
def save_history(history_set):
    recent_history = list(history_set)[-100:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))

# 多源抓取 + 交叉轮流抽取去重
def fetch_top_news(limit=10):
    history = load_history()
    source_buckets = {}  # 存放每个媒体过滤后可用的新闻

    print(f"正在从 {len(NEWS_SOURCES)} 个媒体源并发收集最新要闻...")

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            feed = feedparser.parse(url)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, 'id', entry.link)
                if entry_id not in history:
                    valid_entries.append((entry, entry_id, name))
            source_buckets[name] = valid_entries
            print(f"  - [{name}] 抓取成功，发现 {len(valid_entries)} 条全新资讯")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过 (网络或解析异常: {e})")
            source_buckets[name] = []

    # 轮流从各个媒体抽取新闻（Round-Robin），保证内容多样性
    selected_news = []
    new_ids = set()
    
    max_loops = max([len(v) for v in source_buckets.values()], default=0)
    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in new_ids:
                    title = entry.title
                    summary = entry.summary if hasattr(entry, 'summary') else ""
                    selected_news.append(f"[{media_name}] 标题: {title}\n摘要: {summary}\n")
                    new_ids.add(entry_id)
                    if len(selected_news) >= limit:
                        break
        if len(selected_news) >= limit:
            break

    # 更新历史去重记录
    history.update(new_ids)
    save_history(history)
    
    print(f"总计挑选出 {len(selected_news)} 条多源国际要闻！")
    return "\n".join(selected_news)

# 调用 Gemini 模型润色为早报/晚报口播稿
def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在生成【{period_name}】口播稿...")
    prompt = f"""
你是一位专业的新闻电台主播。以下内容收集自多家全球权威通讯社和媒体（带有媒体标签）。
请将这 10 条国际新闻提炼并改写为一篇中文【{period_name}】口播广播稿：

要求：
1. 开篇有亲切的【{period_name}】问候，并播报当前北京时间，结尾有简短的收尾致谢。
2. 融合成一篇连贯的新闻快讯，条目之间加上自然的新闻主播转场过渡词（如“另一条要闻是……”、“在经济领域……”、“德国之声报道……”）。
3. 语言必须口语化，适合直接朗读，严禁出现 Markdown 标记（如 **、#）、括号、网址或特殊符号。
4. 全文字数控制在 1000~1500 字左右。

新闻素材如下：
{raw_news}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
    data = response.json()
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        raise RuntimeError(f"Gemini API 响应异常: {data}") from e

# 微软 Edge TTS 语音合成
async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

# 生成与更新播客 RSS XML
def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)
    
    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日聚合全球多源权威国际要闻 10 条口播摘要。</description>
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
    <description>每日自动汇总全球多源热点 10 条新闻，AI 自动播报。</description>
{items_block}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_template)

async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    repo = os.getenv("GITHUB_REPOSITORY")
    
    # 北京时间计算 (UTC+8)
    bj_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")
    
    hour = bj_time.hour
    if 4 <= hour < 12:
        period_name = "早报"
    elif 12 <= hour < 19:
        period_name = "晚报"
    else:
        period_name = "特别快报"

    episode_title = f"国际要闻{period_name} ({date_display} {bj_time.strftime('%H:%M')})"
    tag = f"episode-{time_str}"
    audio_filename = f"news-{time_str}.mp3"

    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 多源抓取与改写
    raw_news = fetch_top_news(limit=10)
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)
    
    # 2. 配音合成
    await text_to_speech(broadcast_script, audio_filename)
    
    # 3. 产出 RSS
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("全自动多源播客制作完成！")

if __name__ == "__main__":
    asyncio.run(main())
