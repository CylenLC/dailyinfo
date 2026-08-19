import feedparser
import hashlib
import json
import os
import requests
import time
from openai import OpenAI

# --- 1. 配置区 ---
DIRECT_SOURCES = [
    {"name": "Solidot官方", "url": "https://www.solidot.org/index.rss"},
    {"name": "V2EX官方", "url": "https://www.v2ex.com/index.xml"},
    {"name": "Arxiv-AI论文", "url": "https://rss.arxiv.org/rss/cs.AI"}
]

# 核心关键词：只有包含这些词，才会被挑出来展示和提炼（省钱核心）
KEYWORDS = [
    "AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", 
    "DeepSeek", "GPT", "Claude", "机器人", "开源", "自动化"
]

# AI 实例化 (加入环境变量检测)
api_key = os.getenv("DEEPSEEK_API_KEY")
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com") if api_key else None

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
            resp = requests.get(f"{source['url']}?t={int(time.time())}", headers=headers, timeout=20)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries:
                    item_id = hashlib.md5(entry.link.encode()).hexdigest()
                    if item_id not in history:
                        title = entry.title
                        # 语义筛选
                        if any(key.lower() in title.lower() for key in KEYWORDS):
                            new_stories.append({
                                "title": title,
                                "link": entry.link,
                                "source": source['name']
                            })
                        history.append(item_id)
        except Exception as e:
            print(f"抓取 {source['name']} 失败: {e}")

    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f)
    
    return new_stories

def generate_ai_report(news_list):
    # 如果没配 API Key 或者没有新资讯，直接跳过
    if not client or not news_list:
        return None
    
    # 构造素材 (限制前15条，极致省钱)
    raw_text = "\n".join([f"- {n['title']} ({n['source']})" for n in news_list[:15]])
    
    prompt = f"""
    你现在是《ai水文信息战》主编。请根据以下素材撰写一段今日简报。
    
    风格要求：极客感、宏大叙事、带点2026年的科幻感。
    参考文风：'2026年的春天，AI圈的关键词不再是“聊天”，而是“接管”。一边是OpenClaw这种数字员工学会了动手，另一边是AI4S正从理论走向战场...'
    
    任务：
    1. 概括今日一个核心进化点。
    2. 从素材中选出2个最硬核的动态进行犀利点评。
    3. 结尾：一句话总结生产力底层的全谱系进化。

    素材如下：
    {raw_text}
    """
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"AI 提炼出错 (可能是余额或网络问题): {e}")
        return None

if __name__ == "__main__":
    filtered_news = fetch_content()
    
    print("\n" + "="*20 + " 筛选结果 " + "="*20)
    for idx, item in enumerate(filtered_news):
        print(f"【{idx+1}】{item['title']}")

    # 尝试生成 AI 简报
    report = generate_ai_report(filtered_news)
    
    if report:
        print("\n" + "="*20 + " AI 硬核提炼 " + "="*20)
        print(report)
        print("="*52)
    else:
        print("\n[提示] 未生成 AI 简报。原因可能是：没有新硬核资讯、未配置 API Key 或 API 调用失败。")
