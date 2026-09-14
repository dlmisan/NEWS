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
# 新闻源配置（覆盖英、德、美及全球聚合）
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

VOICE = "zh-CN-YunxiNeural"  # 微软 Edge TTS 推荐新闻男声：云希
HISTORY_FILE = "history_ids.txt"

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_history(history_set):
    recent_history = list(history_set)[-150:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))

# 抓取阶段：抓取 20 条候选新闻（为语义去重留出充足冗余）
def fetch_candidate_news(candidate_limit=20):
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个媒体源收集候选资讯...")

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
            print(f"  - [{name}] 抓取到 {len(valid_entries)} 条全新候选资讯")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过 (网络或解析异常: {e})")
            source_buckets[name] = []

    # 轮流从各源抽取候选素材
    candidates = []
    new_ids = set()
    max_loops = max([len(v) for v in source_buckets.values()], default=0)

    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in new_ids:
                    title = entry.title
                    summary = entry.summary if hasattr(entry, 'summary') else ""
                    candidates.append(f"[{media_name}] 标题: {title}\n摘要: {summary}\n")
                    new_ids.add(entry_id)
                    if len(candidates) >= candidate_limit:
                        break
        if len(candidates) >= candidate_limit:
            break

    # 保存已阅读链接，防止跨期重复
    history.update(new_ids)
    save_history(history)
    
    print(f"总计收集到 {len(candidates)} 条候选素材，准备由 AI 进行语义比对与去重...")
    return "\n".join(candidates)

# 调用 Gemini：识别相同事件、融合信息、确保提炼出 10 个互不重叠的独立事件
def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在调用大模型进行【语义去重 + 话题融合 + {period_name}口播稿撰写】...")
    prompt = f"""
你是一位严谨、客观的国家级新闻广播电台播音员。
以下是从多家国际权威媒体收集的新闻素材：

【核心任务与规则】：
1. 筛选 30 条独立要闻：
   - 仔细比对素材，如果多家媒体报道同一事件，必须融合成一条，严禁同一事件分两段播报。
   - 优先选择涉及以下主题的事件（素材中若有则优先入选；若无此类报道则按其他重大要闻顺延，切勿无中生有）：俄乌局势、中东局势/以色列相关军事行动、美伊局势等。
   - 内容过滤：若素材中涉及中国国内政治、特定领导人等内容，请直接剔除，不予采纳。

2. 绝对忠实于素材事实（核心红线）：
   - 严格以第三人称客观复述素材中明确提及的事实（时间、地点、涉事主体、发生事件与官方通报）。
   - 严禁自行引申、脑补背景、添加素材中没有的评论或推测，杜绝主观形容词。

3. 播音风格与排版：
   - 开篇简短问候（{period_name}），播报当前时间，结尾简短致谢。
   - 30 个事件依次播报，条目间使用简短自然的广播转场词（如“下一条消息”、“另一项国际动态是……”）。
   - 纯文本输出：严禁出现 Markdown 标记（严禁出现 **、# 等符号）、括号、网址。

4. 篇幅控制：
   - 每条新闻保留 100~130 字的核心事实提炼，全篇总字数严格控制在 3500~4500 字左右，确保结构完整，切勿被截断。

原始新闻素材如下：
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
      <description>今日聚合全球权威要闻 10 大热点口播摘要（已完成智能语义去重与多源融合）。</description>
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
    <description>每日早晚自动汇总全球多源热点 30 条新闻，AI 自动语义去重与配音播报。</description>
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

    # 1. 扩大抓取池（抓取 50 条）
    raw_news = fetch_candidate_news(candidate_limit=50)
    
    # 2. 由 Gemini 执行语义去重并生成 10 个独立事件广播稿
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)
    
    # 3. 配音合成
    await text_to_speech(broadcast_script, audio_filename)
    
    # 4. 生成 RSS
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("制作完成！本次已彻底杜绝内容重复！")

if __name__ == "__main__":
    asyncio.run(main())
