import os
import re
import asyncio
import datetime
import email.utils
import json
import requests
import feedparser
import edge_tts

# 新闻源与音色配置
# RSS_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"
RSS_URL = "https://www.zaobao.com.sg/rss/realtime/world"
VOICE = "zh-CN-YunxiNeural"  # 微软 Edge TTS 推荐新闻男声：云希

def fetch_top_news(limit=10):
    print("正在抓取最新国际新闻...")
    feed = feedparser.parse(RSS_URL)
    news_items = []
    for entry in feed.entries[:limit]:
        title = entry.title
        summary = entry.summary if hasattr(entry, 'summary') else ""
        news_items.append(f"标题: {title}\n摘要: {summary}\n")
    return "\n".join(news_items)

def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在生成【{period_name}】口播稿...")
    prompt = f"""
你是一位专业的新闻电台主播。请将以下 10 条国际新闻提炼并改写为一篇连贯的中文【{period_name}】口播广播稿：
要求：
1. 开篇有亲切的【{period_name}】问候，并播报当前北京时间，结尾有简短的收尾致谢。
2. 条目之间加入流畅自然的转场过渡词（如“另一条要闻是……”、“在经济领域……”）。
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

async def text_to_speech(text, output_file):
    print(f"正在合成语音: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在生成/增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)
    
    # 构造本次单集条目
    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>热点国际要闻 10 条口播摘要。</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{audio_size}" type="audio/mpeg"/>
      <guid>{audio_url}</guid>
    </item>"""

    # 如果本地已有 feed.xml，保留历史前 9 期，与新一期组成最近 10 期节目
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
    <description>每日自动汇总全球热点 10 条新闻，AI 自动播报。</description>
{items_block}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_template)

async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    repo = os.getenv("GITHUB_REPOSITORY")
    
    # 计算北京时间 (UTC+8)
    bj_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")
    
    # 智能识别早报、晚报或特别快报
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

    # 将生成的唯一标签和文件名保存为临时文本，供 GitHub Actions 读取
    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 1. 抓取与改写
    raw_news = fetch_top_news(limit=10)
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)
    
    # 2. 语音合成
    await text_to_speech(broadcast_script, audio_filename)
    
    # 3. 更新 RSS feed
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("全部生成完毕！")

if __name__ == "__main__":
    asyncio.run(main())
