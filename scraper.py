import feedparser
import hashlib
import json
import os
import requests
import time

# --- 配置区 ---
DIRECT_SOURCES = [
    {"name": "Solidot官方", "url": "https://www.solidot.org/index.rss"},
    {"name": "V2EX官方", "url": "https://www.v2ex.com/index.xml"},
    {"name": "Arxiv-AI论文", "url": "https://rss.arxiv.org/rss/cs.AI"}
]

# 核心关键词：只有包含这些词，才会被挑出来展示
KEYWORDS = ["AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "机器人"]

def fetch_content():
    if os.path.exists("history.json"):
        with open("history.json", "r", encoding="utf-8") as f:
            try: history = json.load(f)
            except: history = []
    else:
        history = []

    new_stories = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for source in DIRECT_SOURCES:
        try:
            print(f"正在抓取: {source['name']}")
            resp = requests.get(f"{source['url']}?t={int(time.time())}", headers=headers, timeout=20)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries:
                    item_id = hashlib.md5(entry.link.encode()).hexdigest()
                    if item_id not in history:
                        title = entry.title
                        # --- 筛选逻辑：只有命中关键词才加入 new_stories ---
                        if any(key.lower() in title.lower() for key in KEYWORDS):
                            new_stories.append({
                                "title": title,
                                "link": entry.link,
                                "source": source['name']
                            })
                        history.append(item_id) # 无论是否命中，都标记为已读，避免下次重复处理
        except Exception as e:
            print(f"抓取 {source['name']} 失败: {e}")

    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f)
    
    return new_stories

if __name__ == "__main__":
    filtered_news = fetch_content()
    
    print("\n" + "="*20 + " 筛选结果预览 " + "="*20)
    print(f"从今日资讯中为你精选了 {len(filtered_news)} 条硬核动态：\n")
    
    if not filtered_news:
        print("暂时没有发现包含关键词的硬核资讯。")
    else:
        for idx, item in enumerate(filtered_news):
            print(f"【{idx+1}】[{item['source']}] {item['title']}")
            print(f"🔗 链接: {item['link']}\n")
    
    print("="*54)
