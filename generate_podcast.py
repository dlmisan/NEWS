import os
import re
import time
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

VOICE = "zh-CN-YunxiNeural"  # 微软 Edge TTS 专业新闻男声
HISTORY_FILE = "history_ids.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_history(history_set):
    # 只保留最近 100 条已播记录，既能防重，又避免长时间测试造成信源枯竭
    recent_history = list(history_set)[-100:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent_history))

# 抓取候选池：确保充足素材（目标 40~50 条）
def fetch_candidate_news(candidate_limit=50):
    history = load_history()
    source_buckets = {}
    all_raw_entries = []

    print(f"正在从 {len(NEWS_SOURCES)} 个权威媒体源收集最新资讯...")

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            valid_entries = []
            for entry in feed.entries:
                entry_id = getattr(entry, 'id', entry.link)
                all_raw_entries.append((entry, entry_id, name))
                if entry_id not in history:
                    valid_entries.append((entry, entry_id, name))
            source_buckets[name] = valid_entries
            print(f"  - [{name}] 抓取到 {len(feed.entries)} 条实时资讯 (其中全新未播: {len(valid_entries)} 条)")
        except Exception as e:
            print(f"  - [{name}] 抓取跳过 (网络或解析异常: {e})")
            source_buckets[name] = []

    candidates = []
    new_ids = set()
    max_loops = max([len(v) for v in source_buckets.values()], default=0)

    # 第一轮：优先提取从未播报过的全新新闻
    for i in range(max_loops):
        for name, entries in source_buckets.items():
            if i < len(entries):
                entry, entry_id, media_name = entries[i]
                if entry_id not in new_ids:
                    title = entry.title
                    summary = entry.summary if hasattr(entry, 'summary') else ""
                    # 清洗掉可能存在的 HTML 标签
                    summary = re.sub(r'<[^>]+>', '', summary).strip()
                    candidates.append(f"【媒体：{media_name}】 标题: {title} | 概要: {summary}")
                    new_ids.add(entry_id)
                    if len(candidates) >= candidate_limit:
                        break
        if len(candidates) >= candidate_limit:
            break

    # 保底防饥饿机制：若全新新闻不足 30 条（如连续手动测试），放宽历史补充最新热点
    if len(candidates) < 30:
        print(f"提示：全新资讯不足 30 条 (当前 {len(candidates)} 条)，启动保底机制回填实时最新要闻...")
        for entry, entry_id, media_name in all_raw_entries:
            if entry_id not in new_ids:
                title = entry.title
                summary = entry.summary if hasattr(entry, 'summary') else ""
                summary = re.sub(r'<[^>]+>', '', summary).strip()
                candidates.append(f"【媒体：{media_name}】 标题: {title} | 概要: {summary}")
                new_ids.add(entry_id)
                if len(candidates) >= candidate_limit:
                    break

    # 保存本次入选记录
    history.update(new_ids)
    save_history(history)
    
    print(f"--> 总计精选出 {len(candidates)} 条充足素材送入 AI 处理！")
    return "\n".join(candidates)

# 调用 Gemini：忠于事实、15条完整要闻、字数达标
def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在调用大模型提炼【{period_name}】国际要闻广播稿...")
    prompt = f"""
你是一位国家级广播电台的高级国际新闻主播。
请根据以下收集到的国际新闻素材，提炼并撰写一篇专业、严谨、客观的中文【{period_name}】广播口播稿。

【核心采编规则】：
1. 精选 15 条重大独立要闻：
   - 仔细比对素材，同一事件若有多家媒体报道，必须合并为一条（可注明“据多家媒体报道”），严禁同一事件分两段播报。
   - 优先选题：俄乌战场及地缘局势、美国与伊朗局势、以色列与中东冲突及周边动态。素材中若有则优先入选；若无则按其他重大国际经济/政治要闻顺延，严禁无中生有。
   - 内容过滤：若素材涉及中国国内政局或特定领导人，一律剔除，不予采纳。

2. 绝对真实客观，杜绝虚构脑补（核心红线）：
   - 严格基于所提供的素材事实复述（时间、地点、涉事主体、发生经过、官方结论）。
   - 严禁自行脑补未提及的历史前因后果、严禁加入推测性假设、AI 评价或主观情绪化形容词。

3. 播音规范与格式：
   - 交代消息出处：每条新闻开门见山点明来源（例如：“据英国广播公司报道……”、“德国之声中文网消息……”、“半岛电视台报道称……”）。
   - 彻底删除转场套话：严禁出现“下一条消息”、“另一项动态”、“下面关注”、“与此同时”等过渡词，直接切入事实。
   - 开篇与结尾：开篇仅一句问候（“听众朋友们好，欢迎收听国际要闻{period_name}。”），严禁播报具体几点几分。结尾仅一句致谢（“以上是本次要闻播报，感谢收听。”）。
   - 纯文本格式：严禁输出任何 Markdown 符号（严禁出现 **、# 等）、括号或网址。

4. 篇幅要求：
   - 15 条新闻必须完整逐条播报，每条新闻篇幅控制在 130~170 字，交代清楚核心要素。
   - 全篇总字数务必达到 2200~2600 字左右（播音时长约 9~11 分钟），确保结构完整，严禁截断。

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

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
            data = response.json()
            if "candidates" in data and len(data["candidates"]) > 0:
                script = data['candidates'][0]['content']['parts'][0]['text']
                print(f"--> 大模型生成成功！广播稿总字数: {len(script)} 字")
                return script
            
            error_msg = data.get("error", {}).get("message", str(data))
            print(f"API 响应异常 (尝试 {attempt}/{max_retries}): {error_msg}")
            if attempt < max_retries:
                time.sleep(attempt * 6)
            else:
                raise RuntimeError(f"Gemini API 响应异常: {data}")
        except requests.exceptions.RequestException as req_err:
            print(f"网络异常 (尝试 {attempt}/{max_retries}): {req_err}")
            if attempt < max_retries:
                time.sleep(attempt * 6)
            else:
                raise

# 语音合成
async def text_to_speech(text, output_file):
    print(f"正在进行语音合成: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)
    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"--> 语音合成完成！音频大小: {size_mb:.2f} MB")

# 更新播客 RSS XML
def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)
    
    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日聚合全球 8 大权威媒体要闻深度客观播报。</description>
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
    <description>每日早晚自动汇总全球多源权威国际要闻，AI 严谨播报。</description>
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

    # 1. 抓取多源候选（确保 40~50 条足额素材）
    raw_news = fetch_candidate_news(candidate_limit=50)
    
    # 2. 改写为 15 条纯客观、事实准确的广播稿（2200~2600字）
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)
    
    # 3. 合成完整音频
    await text_to_speech(broadcast_script, audio_filename)
    
    # 4. 发布单集
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("本期高质量国际要闻播客制作完成！")

if __name__ == "__main__":
    asyncio.run(main())
