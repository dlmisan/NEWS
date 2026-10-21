import asyncio
import datetime
import email.utils
import os
import re
import time
import requests
import edge_tts

# ------------------ 配置区 ------------------

# 媒体来源域名池（直接填域名即可，无需找 RSS 链接）
NEWS_DOMAINS = [
    "rfi.fr", "wsj.com", "scmp.com", "indiatimes.com", "techcrunch.com", 
    "yahoo.com", "bbc.co.uk", "bbc.com", "dw.com", "aljazeera.com", "france24.com", 
    "foxnews.com", "nypost.com", "washingtontimes.com", "newsmax.com"
]

# 模型降级备用梯队：优先使用高性价比主模型，遭遇过载自动顺延
CANDIDATE_MODELS = [
    "gemini-3.6-flash",         # 主力：性能与速度平衡
    "gemini-3.5-flash",         # 备选1：官方标准版主力
    "gemini-3.5-flash-lite",    # 备选2：低延迟轻量级，算力充足不易拥堵
    "gemini-3.1-pro-preview"    # 备选3：深度理解版，保底使用
]

VOICE = "zh-CN-YunyangNeural"  # 微软 Edge TTS 推荐新闻男声：云杨
HISTORY_FILE = "history_ids.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# --------------------------------------------

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
    
    # 拼接 GDELT 查询语法
    domain_query = " OR ".join([f"domain:{d}" for d in NEWS_DOMAINS])
    query = f"({domain_query})"
    
    # 构建请求 URL：获取过去 24 小时、按混合相关度排序的最多 150 条记录
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc?"
        f"query={query}&mode=artlist&format=json&maxrecords=150"
        "&timespan=24h&sort=HybridRel"
    )
    
    print("🚀 正在通过 GDELT 引擎秒级拉取全球精选媒体要闻...")
    
    try:
        # GDELT 响应极快，设置 15 秒超时
        response = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        
        candidates = []
        new_ids = set()
        
        for art in articles:
            url_id = art.get("url", "")
            # 使用 URL 作为唯一去重 ID
            if not url_id or url_id in history or url_id in new_ids:
                continue
                
            title = art.get("title", "")
            domain = art.get("domain", "")
            raw_snippet = art.get("snippet", "")
            
            # 清理 GDELT 摘要中可能带有的高亮 HTML 标签 (如 <b>, </b>)
            clean_snippet = re.sub(r'<[^>]+>', '', raw_snippet)
            
            # 过滤掉内容过短的无效数据
            if len(clean_snippet) < 30:
                continue
                
            candidates.append(f"【来源：{domain}】\n标题: {title}\n详情: {clean_snippet}\n")
            new_ids.add(url_id)
            
            if len(candidates) >= candidate_limit:
                break
                
        # 更新历史记录
        history.update(new_ids)
        save_history(history)
        
        print(f"✅ 成功筛选出 {len(candidates)} 条高质量候选素材，已过滤重复历史。")
        return "\n".join(candidates)
        
    except Exception as e:
        print(f"❌ GDELT 获取失败: {e}")
        return ""

def rewrite_with_gemini(raw_news, api_key, period_name):
    print(f"正在生成【{period_name}】客观口播稿...")
    prompt = f"""
你是一位严谨、专业的国家级新闻广播电台播音员。
以下是从多家国际权威媒体收集的新闻素材：

【核心任务与要求】：
1. 筛选 35 至 40 条独立重大要闻：
   - 仔细比对素材，若多家媒体报道同一事件，必须融合成一条（可注明“据综合消息”），严禁同一事件分两段播报。
   - 优先关注并入选以下主题：俄乌战争、美国伊朗局势、以色列战争/中东冲突、特朗普（注意分辨特朗普是现任总统还是前总统）、习近平、马斯克、AI（素材中若有则优先入选；若无此类报道则按其他重大要闻顺延，严禁凭空编造）。
   - 新闻最后可加入1-2条体育消息。
   - 内容过滤：无。

2. 严格保留新闻来源：
   - 每条新闻必须明确交代消息来源（例如：“据bbc.com报道……”、“wsj.com报道称……”、“据综合消息……”）。

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
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    timeout_seconds = 75

    # 循环尝试备选模型梯队
    for model_name in CANDIDATE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        print(f"\n🔄 尝试调用模型 [{model_name}] ...")

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
                        print(f"✅ 模型 [{model_name}] 成功生成播报稿！")
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

    raise RuntimeError("所有备选 Gemini 模型均处于拥堵或不可用状态，请检查网络或稍后重新触发。")

async def text_to_speech(text, output_file):
    print(f"正在合成音频: {output_file} ...")
    communicate = edge_tts.Communicate(text, VOICE)
    # 为音频合成加上 240 秒保护，防止 WebSocket 假死
    await asyncio.wait_for(communicate.save(output_file), timeout=240)

def update_podcast_feed(audio_url, audio_size, episode_title):
    print("正在增量更新 podcast.xml ...")
    pub_date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
    feed_path = "public/feed.xml"
    os.makedirs("public", exist_ok=True)

    new_item = f"""    <item>
      <title>{episode_title}</title>
      <description>今日聚合全球权威要闻客观播报。</description>
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
    <description>每日自动汇总全球热点要闻，AI 严谨播报。</description>
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

    # 1. 抓取候选池（使用 GDELT）
    raw_news = fetch_candidate_news(candidate_limit=60)
    if not raw_news.strip():
        print("未抓取到有效新闻素材，跳过本次生成。")
        return

    # 2. 改写为客观事实播报稿
    broadcast_script = rewrite_with_gemini(raw_news, api_key, period_name)

    # 3. 合成音频
    await text_to_speech(broadcast_script, audio_filename)

    # 确保音频确实生成成功后，再写入中间标记文件
    with open("current_tag.txt", "w") as f:
        f.write(tag)
    with open("current_audio_filename.txt", "w") as f:
        f.write(audio_filename)

    # 4. 发布单集
    if repo:
        file_size = os.path.getsize(audio_filename)
        audio_url = f"https://github.com/{repo}/releases/download/{tag}/{audio_filename}"
        update_podcast_feed(audio_url, file_size, episode_title)
        print("本期要闻播客制作完成！")
    else:
        print("未检测到 GITHUB_REPOSITORY 环境变量，跳过 RSS Feed 更新。")

if __name__ == "__main__":
    asyncio.run(main())
