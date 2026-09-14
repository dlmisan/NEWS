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
# 1. 国际多媒体 RSS 源配置
# ==========================================
NEWS_SOURCES = [
    {
        "name": "英国广播公司 BBC",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml"
    },
    {
        "name": "德国之声 DW",
        "url": "https://rss.dw.com/rdf/rss-en-world"
    },
    {
        "name": "美国国家公共电台 NPR",
        "url": "https://feeds.npr.org/1004/rss.xml"
    },
    {
        "name": "国际通讯社综合",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD"
    }
]

VOICE = "zh-CN-YunxiNeural"  # 微软 Edge TTS 专业新闻男声：云希
HISTORY_FILE = "history_ids.txt"

# 读取历史已播链接
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

# 保存历史已播记录，保留最近 200 条
def save_history(history_set):
    recent_history = list(history_set)[-200:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))

# 抓取 50 条候选新闻供大模型筛选
def fetch_candidate_news(candidate_limit=50):
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
            print(f"  - [{name}] 抓取到 {len(valid_entries)} 条可用候选")
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
                    candidates.append(f"【媒体来源：{media_name}】 标题: {title}\n摘要: {summary}\n")
                    new_ids.add(entry_id)
                    if len(candidates) >= candidate_limit:
                        break
        if len(candidates) >= candidate_limit:
            break

    history.update(new_ids)
    save_history(history)
    
    print(f"总计获取 {len(candidates)} 条候选素材，准备由 AI 进行深度提炼...")
    return "\n".join(candidates)

# 调用 Gemini 提炼 20 条深度新闻口播稿
def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在生成【{period_name}】深度新闻口播稿（20 条长篇深度报道）...")
    prompt = f"""
你是一位严谨、专业的国家级新闻广播电台资深播音员。
以下是从多家国际权威媒体收集的新闻素材：

【核心任务与改写规则】：
1. 筛选 20 条独立重大要闻：
   - 仔细比对素材，多家媒体报道同一事件时必须融合成一条（可注明“据多家媒体综合报道”），严禁同一事件分两段播报。
   - 优先关注：俄乌战争局势、美国与伊朗局势、以色列与中东冲突相关行动（素材中若有则优先入选；若无此类报道则按其他重大要闻顺延，严禁无中生有）。
   - 内容过滤：若素材中涉及中国国内政局、特定领导人等内容，请直接剔除，不予采纳。

2. 丰富细节与深度展开（关键要求）：
   - 每条新闻必须深入、饱满，单条篇幅保持在 200~250 字左右。
   - 允许且要求：在严格忠实于原素材核心事实的基础上，结合公认的客观历史脉络、地理背景和相关协议条款做客观补充展开，让听众能完整理解来龙去脉。
   - 严厉禁止：严禁加入任何未经证实的推测、主观臆断、形容词修饰或 AI 评论。

3. 规范播报风格：
   - 必须指明来源：每条新闻开门见山说明消息出处（例如：“据英国广播公司报道……”、“德国之声消息指出……”、“美国国家公共电台报道称……”）。
   - 彻底删除转场词：严禁出现“下一条消息”、“另一项国际动态是”、“下面关注”、“与此同时”等过渡套话，直接逐条以客观事实切入。
   - 开篇与结尾：开篇仅做简短问候（例如：“听众朋友们好，欢迎收听国际要闻{period_name}。”），严禁播报具体的当前时间或日期。结尾简短致谢收尾。
   - 纯文本输出：严禁出现 Markdown 标记（严禁出现 **、# 等符号）、括号、网址。

4. 篇幅目标：
   - 全篇总字数控制在 4500~5200 字左右，结构必须完整，切勿被截断。

原始新闻素材如下：
{raw_news}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.2
        }
    }
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
    data = response.json()
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        raise RuntimeError(f"Gemini API 响应异常: {data}") from e

# 语音合成
async def text_to_speech(text, output_file):
    print(f"正在合成完整新闻音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

# 增量更新 RSS XML
def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)
    
    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日聚合全球权威要闻 20 条深度客观播报。</description>
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
    <description>每日早晚自动汇总全球权威 20 条深度国际要闻，AI 严谨播报。</description>
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

    # 1. 抓取 50 条候选素材
    raw_news = fetch_candidate_news(candidate_limit=50)
    
    # 2. 改写为 20 条深度客观播报稿
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)
    
    # 3. 合成音频
    await text_to_speech(broadcast_script, audio_filename)
    
    # 4. 更新单集信息
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("本期 20 条深度要闻播客制作完成！")

if __name__ == "__main__":
    asyncio.run(main())
