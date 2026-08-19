import feedparser
import hashlib
import json
import os
import requests
import datetime
from openai import OpenAI

# --- 配置区 ---
KEYWORDS = ["AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "AI4S", "算法", "Cursor", "编程"]
client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")

def fetch_and_pool():
    """抓取素材并存入周池子（修复版：支持增量存储）"""
    # 🌟 修复：先读取现有的池子和历史，而不是重置为空
    history = json.load(open("history.json", "r")) if os.path.exists("history.json") else []
    if os.path.exists("weekly_pool.json"):
        with open("weekly_pool.json", "r", encoding='utf-8') as f:
            try:
                pool = json.load(f)
            except:
                pool = []
    else:
        pool = []

    sources = [
        {"name": "Google-AI-Blog", "url": "https://blog.google/technology/ai/rss/"}, # Gemini及基础研究
        {"name": "HuggingFace-Blog", "url": "https://huggingface.co/blog/feed.xml"}, # 开源社区风向标
        {"name": "DeepMind-Blog", "url": "https://deepmind.com/blog/feed/basic/"}, # 深度学习前沿进展

        # --- 行业高频聚合与媒体 (High-frequency Tickers) ---
        {"name": "Techmeme-AI", "url": "https://www.techmeme.com/feed.xml"}, # 科技圈公认最高效率的聚合流
        {"name": "MIT-Tech-Review-AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/"}, # 深度趋势分析
        {"name": "VentureBeat-AI", "url": "https://venturebeat.com/category/ai/feed/"}, # 产业应用与投融资快讯
        {"name": "MarkTechPost", "url": "https://www.marktechpost.com/feed/"}, # 极速总结最新论文与工具发布

        # --- 深度研究与社区 (Deep Tech & Community) ---
        {"name": "HF-Daily-Papers", "url": "https://papers.takara.ai/api/feed"}, # 社区维护的每日热门论文简报
        {"name": "Reddit-ML", "url": "https://www.reddit.com/r/MachineLearning/.rss"}, # 开发者讨论最集中的地方
    
        # --- 中文垂直源 (Chinese Vertical) ---
        {"name": "Synced-机器之心", "url": "https://www.jiqizhixin.com/rss"} # 国内最权威的AI垂直媒体
    ]
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    print(f"开始抓取 RSS 源... 当前池子已有 {len(pool)} 条素材")
    new_count = 0
    for s in sources:
        try:
            resp = requests.get(s['url'], headers=headers, timeout=20)
            feed = feedparser.parse(resp.text)
            for entry in feed.entries:
                item_id = hashlib.md5(entry.link.encode()).hexdigest()
                # 🌟 加入去重逻辑，防止重复抓取同一篇
                if item_id not in history:
                    if any(key.lower() in entry.title.lower() for key in KEYWORDS):
                        new_item = {"title": entry.title, "link": entry.link, "source": s['name']}
                        pool.append(new_item)
                        history.append(item_id)
                        new_count += 1
        except Exception as e:
            print(f"抓取 {s['name']} 失败: {e}")
            continue
    
    # 存回历史和池子
    with open("history.json", "w") as f: 
        json.dump(history[-1000:], f)
    with open("weekly_pool.json", "w", encoding='utf-8') as f: 
        json.dump(pool, f, ensure_ascii=False, indent=2)
    
    print(f"本次新增: {new_count} 条，池子总计: {len(pool)} 条")
    return pool

# ... summarize_weekly 和 upload_to_notion 函数保持不变 ...

if __name__ == "__main__":
    current_pool = fetch_and_pool() 
    
    bj_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    weekday = bj_time.weekday() # 0 是周一
    
    print(f"当前北京时间: {bj_time.strftime('%Y-%m-%d %H:%M')}, 星期{weekday+1}")

    # 只要是周一，就尝试发货
    if weekday == 0: 
        print("🚀 检测到周一发布日，正在生成周报...")
        if len(current_pool) > 0: # 🌟 修复：用 len 判断更稳
            report = summarize_weekly(current_pool)
            success = upload_to_notion(report, f"AI水文周报 | {bj_time.strftime('%Y-%m-%d')}")
            
            if success:
                with open("weekly_pool.json", "w", encoding='utf-8') as f:
                    json.dump([], f)
                print("🔥 本周素材已清空，开启新循环。")
        else:
            print("⚠️ 池子是空的，可能这周没有匹配到关键词的新闻。")
    else:
        print(f"今天星期{weekday+1}，仅完成存稿。周一 08:30 自动交稿！")
