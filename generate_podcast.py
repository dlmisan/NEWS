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
    # ------------------ 保守派媒体 ------------------
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


def fetch_candidate_news(candidate_limit=60):
    history = load_history()
    source_buckets = {}

    print(f"正在从 {len(NEWS_SOURCES)} 个媒体源收集候选资讯...")

    # 使用显式超时的 Session 抓取，防止单个 RSS 源卡死整个流水线
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for src in NEWS_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            # 连接超时 5 秒，读取内容超时 10 秒
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


def rewrite_with_gemini(raw_news, api_key, period_name, max_retries=2):
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
    
    # 单次调用超时设置为 90 秒，配合 2 次重试，总耗时控制在 3-4 分钟内
    timeout_seconds = 90

    for attempt in range(max_retries + 1):
        try:
            print(f"正在向 Gemini 发起生成请求 (第 {attempt + 1}/{max_retries + 1} 次)...")
            response = requests.post(
                url, 
                json=payload, 
                headers={"Content-Type": "application/json"}, 
                timeout=timeout_seconds
            )
            data = response.json()
            
            # 1. 成功情况
            if "candidates" in data and len(data["candidates"]) > 0:
                return data["candidates"][0]["content"]["parts"][0]["text"]
                
            # 2. API 报错（如服务不可用、超频等）
            if "error" in data:
                error_msg = data['error'].get('message', '未知 API 错误')
                print(f"⚠️ 第 {attempt + 1} 次请求失败: API 报错 - {error_msg}")
            else:
                print(f"⚠️ 第 {attempt + 1} 次请求失败: 返回格式异常 - {data}")
                
        except requests.exceptions.Timeout:
            print(f"⚠️ 第 {attempt + 1} 次请求超时 ({timeout_seconds}s)")
        except Exception as e:
            print(f"⚠️ 第 {attempt + 1} 次请求发生网络异常: {e}")
            
        # 重试退避等待
        if attempt < max_retries:
            wait_time = (attempt + 1) * 10
            print(f"⏳ 等待 {wait_time} 秒后进行下一次重试...\n")
            time.sleep(wait_time)
            
    raise RuntimeError(f"Gemini API 响应异常或网络超时，已重试 {max_retries} 次仍未成功。")


async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    # 为音频合成任务加上 240 秒强制超时，防止 WebSocket 假死无限挂起
    await asyncio.wait_for(communicate.save(output_file), timeout=240)


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

    if not api_key:
        raise ValueError("缺少环境变量 GEMINI_API_KEY")

    bj_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    time_str = bj_time.strftime("%Y%m%d-%H%M")
    date_display = bj_time.strftime("%Y-%m-%d")

    hour = bj_time.hour
    period_name = "早间新闻" if hour < 12 else "晚间新闻"
    episode_title = f"{period_name} ({date_display})"
    tag = f"episode-{time_str}"
    audio_filename = f"news-{time_str}.mp3"

    # 1. 抓取候选池（有超时保护）
    raw_news = fetch_candidate_news(candidate_limit=60)
    if not raw_news.strip():
        print("未抓取到有效新闻素材，跳过本次生成。")
        return

    # 2. 改写为客观事实播报稿（有 90s 超时和自动重试）
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)

    # 3. 合成音频（有 240s 异步超时保护）
    await text_to_speech(broadcast_script, audio_filename)

    # 只有在音频真正生成成功后，再写入 tag 和文件名，防止下游 Release 读到空文件
    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 4. 发布单集
    file_size = os.path.getsize(audio_filename)
    audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
    update_podcast_feed(audio_url, file_size, episode_title)
    print("本期 30 条要闻播客制作完成！")


if __name__ == "__main__":
    asyncio.run(main())
