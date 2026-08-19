import feedparser
import hashlib
import json
import os
import requests
import time
import datetime
from openai import OpenAI

# --- 配置区 ---
KEYWORDS = ["AI", "智能体", "Agent", "模型", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "机器人", "AI4S", "算法", "Cursor", "编程"]
client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")

def fetch_and_pool():
    # A. 加载数据
    history = json.load(open("history.json", "r")) if os.path.exists("history.json") else []
    pool = json.load(open("weekly_pool.json", "r")) if os.path.exists("weekly_pool.json") else []

    sources = [
        {"name": "Solidot", "url": "https://www.solidot.org/index.rss"},
        {"name": "V2EX", "url": "https://www.v2ex.com/index.xml"},
        {"name": "Arxiv-AI", "url": "https://rss.arxiv.org/rss/cs.AI"}
    ]
    
    new_found = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    for s in sources:
        try:
            resp = requests.get(s['url'], headers=headers, timeout=20)
            feed = feedparser.parse(resp.text)
            for entry in feed.entries:
                item_id = hashlib.md5(entry.link.encode()).hexdigest()
                if item_id not in history:
                    if any(key.lower() in entry.title.lower() for key in KEYWORDS):
                        new_item = {"title": entry.title, "link": entry.link, "source": s['name']}
                        new_found.append(new_item)
                        pool.append(new_item)
                    history.append(item_id)
        except: continue

    with open("history.json", "w") as f: json.dump(history[-1000:], f)
    with open("weekly_pool.json", "w") as f: json.dump(pool, f, ensure_ascii=False)
    return pool

def summarize_weekly(pool):
    if not pool: return "本周池子是空的，暂无硬核资讯。"
    
    # 自动校准北京时间
    beijing_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y.%m.%d')
    
    # 预分类逻辑
    models = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["模型", "GPT", "Claude", "LLM", "发布", "Qwen", "Llama", "DeepSeek"])]
    tools = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["OpenClaw", "Agent", "工具", "Cursor", "编程", "自动化", "插件", "Vibe"])]
    science = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["科学", "AI4S", "鼠脑", "物理", "气象", "生物", "架构", "芯片"])]

    def get_text(l): return "\n".join([f"- {i['title']} ({i['source']})" for i in l[:20]]) if l else "暂无核心波动。"

    prompt = f"""
    你现在是《AI水文信息站》主笔。今天是 {beijing_time}。
    请撰写本周的 AI 深度观察周报。
    
    【写作人格】：2026年的极客观察家，语气冷静、叙事感强。
    【板块强制要求】：
    1. 进化：模型前线的意志坍缩（探讨模型发布与底层架构）
    2. 工具：接管工作的数字员工（探讨Agent、OpenClaw、编程辅助）
    3. 战场：AI4S 与底层重构（探讨科学发现、鼠脑冷冻、芯片架构）

    按照三个板块，每个板块分成一个大类：
    第一个板块内容是每星期发布的国内外新前沿AI模型，介绍模型所在公司，技术创新，适用场景，主要是探讨模型发布和底层架构；
    第二个板块内容是每星期新发布的AI工具，帮助我们更好的使用AI，例如：编程辅助、Agent等等，进行简要介绍；
    第三个板块主要针对AI FOR SCIENCE，同样是每周学术界发生的AI变革，科学发现。

    【本周素材】：
    模型类：{get_text(models)}
    工具类：{get_text(tools)}
    科学类：{get_text(science)}
    """
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.8
    )
    return f"**【ai水文信息站】周报 | {beijing_time}**\n\n" + response.choices[0].message.content

if __name__ == "__main__":
    current_pool = fetch_and_pool()
    now = datetime.datetime.utcnow()
    
    # 调试技巧：如果你想现在就看总结，把下面这行改为 if True:
    if now.weekday() == 6 and now.hour >= 14: 
        print("--- 触发周日晚间总结模式 ---")
        report = summarize_weekly(current_pool)
        print(report)
        with open("weekly_pool.json", "w") as f: json.dump([], f)
    else:
        print(f"--- 蓄水模式：本周已存入 {len(current_pool)} 条硬核资讯 ---")
