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
# 1. 精选 8 大全球权威公开信源池（中英双轨、极度稳定）
# ==========================================
NEWS_SOURCES = [
    {
        "name": "BBC 中文网",
        "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"
    },
    {
        "name": "德国之声 DW 中文",
        "url": "https://rss.dw.com/rdf/rss-chi-all"
    },
    {
        "name": "英国广播公司 BBC (国际)",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml"
    },
    {
        "name": "半岛电视台 Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml"
    },
    {
        "name": "法国 24 台 France 24",
        "url": "https://www.france24.com/en/rss"
    },
    {
        "name": "美国国家公共电台 NPR",
        "url": "https://feeds.npr.org/1004/rss.xml"
    },
    {
        "name": "联合国新闻 UN News",
        "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"
    },
    {
        "name": "国际通讯社聚合 Google News",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD"
    }
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
            feed = feedparser.parse(url)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, 'id', entry.link)
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
                    summary = entry.summary if hasattr(entry, 'summary') else ""
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

def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在生成【{period_name}】客观口播稿...")
    prompt = f"""
你是一位严谨、专业的国家级新闻广播电台播音员。
以下是从多家国际权威媒体收集的新闻素材：

【核心任务与要求】：
1. 筛选 30 条独立重大要闻：
   - 仔细比对素材，若多家媒体报道同一事件，必须融合成一条（可注明“据多家媒体综合报道”），严禁同一事件分两段播报。
   - 优先关注并入选以下主题：俄乌战争、美国伊朗局势、以色列战争/中东冲突（素材中若有则优先入选；若无此类报道则按其他重大要闻顺延，严禁凭空编造）。
   - 内容过滤：若素材中涉及中国国内政局、特定领导人等内容，直接剔除，不予采纳。

2. 严格保留新闻来源：
   - 每条新闻必须明确交代消息来源（例如：“据英国广播公司报道……”、“德国之声报道称……”、“据美国国家公共电台消息……”）。

3. 彻底删除转场套话与时间播报：
   - 开篇仅作简短问候（例如：“听众朋友们好，欢迎收听国际要闻{period_name}。”），严禁播报具体的当前时间或日期。
   - 严禁使用任何广播转场套话（绝对不要出现“下一条消息”、“另一项国际动态是”、“下面关注”、“与此同时”等过渡词），每条新闻直接以来源和事实展开播报。
   - 结尾简短致谢收尾（例如：“以上是本次国际要闻播报，感谢收听。”）。

4. 绝对忠实原稿，杜绝主观发挥：
   - 严禁加入任何未经素材提及的推测、主观分析、形容词或 AI 观点，仅客观复述事实（时间、地点、主体、发生情况与官方表态）。
   - 纯文本输出：严禁出现 Markdown 标记（如 **、#）、括号、网址或特殊符号。

5. 篇幅：每条新闻保留核心事实，全篇总字数控制在 3500~4500 字左右，确保结构完整，切勿中途截断。

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
    if 4 <= hour < 12:
        period_name = "早报"
    elif 12 <= hour < 19:
        period_name = "晚报"
    else:
        period_name = "特别快报"

    episode_title = f"国际要闻{period_name} ({date_display})"
    tag = f"episode-{time_str}"
    audio_filename = f"news-{time_str}.mp3"

    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 抓取候选池（扩大至 60 条候选）
    raw_news = fetch_candidate_news(candidate_limit=60)
    
    # 2. 改写为 30 条纯客观事实播报稿（去转场、带来源、无当前时间）
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
