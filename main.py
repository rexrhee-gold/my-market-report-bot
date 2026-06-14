import os
import json
import smtplib
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def now_kst():
    return datetime.now(KST)


def since_utc(hours=24):
    return datetime.now(UTC) - timedelta(hours=hours)


def dedupe(items):
    seen = set()
    result = []

    for item in items:
        key_raw = f"{item.get('title','')}|{item.get('url','')}"
        key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()

        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


def fetch_newsapi():
    api_key = os.getenv("NEWSAPI_KEY")
    if not api_key:
        return []

    query = (
        "stock market OR equities OR inflation OR fed OR rates OR "
        "semiconductor OR AI OR oil OR dollar OR Korea market OR KOSPI"
    )

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": since_utc(24).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 50,
        "apiKey": api_key,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    articles = r.json().get("articles", [])
    return [
        {
            "source": a.get("source", {}).get("name"),
            "title": a.get("title"),
            "description": a.get("description"),
            "url": a.get("url"),
            "published_at": a.get("publishedAt"),
            "provider": "NewsAPI",
        }
        for a in articles
        if a.get("title") and a.get("url")
    ]


def fetch_alpha_vantage_news():
    api_key = os.getenv("ALPHAVANTAGE_KEY")
    if not api_key:
        return []

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "financial_markets,economy_monetary,economy_macro,technology",
        "sort": "LATEST",
        "limit": 50,
        "apikey": api_key,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    feed = r.json().get("feed", [])
    return [
        {
            "source": a.get("source"),
            "title": a.get("title"),
            "description": a.get("summary"),
            "url": a.get("url"),
            "published_at": a.get("time_published"),
            "overall_sentiment_score": a.get("overall_sentiment_score"),
            "overall_sentiment_label": a.get("overall_sentiment_label"),
            "provider": "Alpha Vantage",
        }
        for a in feed
        if a.get("title") and a.get("url")
    ]


def build_data_packet():
    articles = []
    articles.extend(fetch_newsapi())
    articles.extend(fetch_alpha_vantage_news())

    articles = dedupe(articles)

    return {
        "generated_at_kst": now_kst().isoformat(),
        "article_count": len(articles),
        "articles": articles[:80],
        "instruction": "이 데이터만 근거로 오늘의 증시 분석 보고서를 작성하라.",
    }


def generate_report(data_packet):
    with open("prompts/expert.md", "r", encoding="utf-8") as f:
        expert_prompt = f.read()

    user_input = f"""
오늘 날짜: {now_kst().strftime('%Y-%m-%d %H:%M KST')}

아래는 최근 24시간 내 수집한 시장 관련 데이터다.
기사 전문이 아니라 제목, 요약, 출처, URL, 발행시각 중심으로 제공한다.

DATA:
{json.dumps(data_packet, ensure_ascii=False, indent=2)}

요구사항:
- 한국어로 작성
- Markdown 형식
- 출처와 링크를 가능한 한 유지
- 투자 권유가 아니라 분석 참고자료로 작성
"""

    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        instructions=expert_prompt,
        input=user_input,
        max_output_tokens=6000,
    )

    return response.output_text


def send_slack(report):
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        return

    payload = {
        "text": f"*오늘의 증시 분석 보고서*\n\n{report}"
    }

    r = requests.post(webhook, json=payload, timeout=30)
    r.raise_for_status()


def send_notion(report):
    token = os.getenv("NOTION_TOKEN")
    parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID")

    if not token or not parent_page_id:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    title = f"증시 분석 보고서 - {now_kst().strftime('%Y-%m-%d')}"

    chunks = [report[i:i + 1800] for i in range(0, len(report), 1800)]

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": chunk}
                    }
                ]
            },
        }
        for chunk in chunks[:80]
    ]

    payload = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": [
                {
                    "text": {
                        "content": title
                    }
                }
            ]
        },
        "children": children,
    }

    r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def send_email(report):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_host, smtp_user, smtp_password, email_to]):
        return

    subject = f"[자동] 증시 분석 보고서 - {now_kst().strftime('%Y-%m-%d')}"
    msg = MIMEText(report, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = email_to

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def main():
    data_packet = build_data_packet()
    report = generate_report(data_packet)

    send_slack(report)
    send_notion(report)
    send_email(report)

    print("Report sent successfully.")


if __name__ == "__main__":
    main()