import os
import json
import smtplib
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from dotenv import load_dotenv
from openai import OpenAI


# =========================
# 기본 설정
# =========================

load_dotenv()

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

EMAIL_TO = os.getenv("EMAIL_TO")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 시간 함수
# =========================

def now_kst():
    return datetime.now(KST)


def since_utc(hours=24):
    return datetime.now(UTC) - timedelta(hours=hours)


# =========================
# 뉴스 수집
# =========================

def fetch_newsapi():
    if not NEWSAPI_KEY:
        print("NEWSAPI_KEY가 없습니다. 뉴스 수집을 건너뜁니다.")
        return []

    query = (
        "stock market OR equities OR inflation OR fed OR interest rates OR "
        "semiconductor OR AI OR oil OR dollar OR bond yield OR Korea market OR KOSPI"
    )

    url = "https://newsapi.org/v2/everything"

    params = {
        "q": query,
        "from": since_utc(24).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 50,
        "apiKey": NEWSAPI_KEY,
    }

    print("뉴스 데이터 수집 시작")

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    articles = response.json().get("articles", [])

    result = []

    for article in articles:
        title = article.get("title")
        article_url = article.get("url")

        if not title or not article_url:
            continue

        result.append(
            {
                "source": article.get("source", {}).get("name"),
                "title": title,
                "description": article.get("description"),
                "url": article_url,
                "published_at": article.get("publishedAt"),
                "provider": "NewsAPI",
            }
        )

    print(f"뉴스 {len(result)}개 수집 완료")
    return result


def dedupe_articles(articles):
    seen = set()
    result = []

    for article in articles:
        key_raw = f"{article.get('title', '')}|{article.get('url', '')}"
        key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()

        if key not in seen:
            seen.add(key)
            result.append(article)

    return result


def build_data_packet():
    articles = []

    articles.extend(fetch_newsapi())

    articles = dedupe_articles(articles)

    data_packet = {
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "article_count": len(articles),
        "articles": articles[:80],
        "instruction": "이 데이터만 근거로 오늘의 증시 분석 보고서를 작성하라.",
    }

    print(f"최종 기사 수: {len(articles)}개")
    return data_packet


# =========================
# OpenAI 보고서 생성
# =========================

def load_expert_prompt():
    prompt_path = "prompts/expert.md"

    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            "prompts/expert.md 파일이 없습니다. "
            "GitHub 저장소에 prompts/expert.md 파일을 만들어 주세요."
        )

    with open(prompt_path, "r", encoding="utf-8") as file:
        return file.read()


def generate_report(data_packet):
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 없습니다.")

    expert_prompt = load_expert_prompt()

    user_input = f"""
오늘 날짜: {now_kst().strftime('%Y-%m-%d %H:%M KST')}

아래는 최근 24시간 내 수집한 시장 관련 뉴스 데이터입니다.
기사 전문이 아니라 제목, 요약, 출처, URL, 발행시각 중심으로 제공합니다.

DATA:
{json.dumps(data_packet, ensure_ascii=False, indent=2)}

작성 요구사항:
- 한국어로 작성하세요.
- Markdown 형식으로 작성하세요.
- 보고서 맨 위에 '오늘 한 줄 요약'을 넣으세요.
- 핵심 이슈 5개를 정리하세요.
- 미국 증시 영향, 한국 증시 영향, 환율/금리/유가 영향을 분리해서 설명하세요.
- 주목할 섹터와 리스크 요인을 정리하세요.
- 가능한 경우 기사 출처와 URL을 함께 표시하세요.
- 투자 권유가 아니라 참고용 분석이라는 문구를 포함하세요.
"""

    print("OpenAI 보고서 생성 시작")

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=expert_prompt,
        input=user_input,
        max_output_tokens=6000,
    )

    report = response.output_text

    if not report or not report.strip():
        raise ValueError("OpenAI 응답이 비어 있습니다.")

    print("OpenAI 보고서 생성 완료")
    return report


# =========================
# 이메일 발송
# =========================

def send_email(report):
    if not EMAIL_TO:
        print("EMAIL_TO가 없어서 이메일 발송을 건너뜁니다.")
        return

    if not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP_USER 또는 SMTP_PASSWORD가 없어서 이메일 발송을 건너뜁니다.")
        return

    subject = f"[자동] 증시 분석 보고서 - {now_kst().strftime('%Y-%m-%d')}"

    msg = MIMEText(report, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    print("이메일 발송 시작")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print("이메일 발송 완료")


# =========================
# Slack 발송
# =========================

def send_slack(report):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 없어서 Slack 발송을 건너뜁니다.")
        return

    slack_text = report[:3500]

    if len(report) > 3500:
        slack_text += "\n\n...(보고서가 길어 일부만 표시했습니다. 전체 내용은 이메일 또는 Notion을 확인하세요.)"

    payload = {
        "text": f"*오늘의 증시 분석 보고서*\n\n{slack_text}"
    }

    print("Slack 발송 시작")

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Slack 발송 실패: {response.status_code} {response.text}")

    print("Slack 발송 완료")


# =========================
# Notion 저장
# =========================

def chunk_text(text, size=1800):
    return [text[i:i + size] for i in range(0, len(text), size)]


def send_notion(report):
    if not NOTION_TOKEN:
        print("NOTION_TOKEN이 없어서 Notion 저장을 건너뜁니다.")
        return

    if not NOTION_PARENT_PAGE_ID:
        print("NOTION_PARENT_PAGE_ID가 없어서 Notion 저장을 건너뜁니다.")
        return

    title = f"증시 분석 보고서 - {now_kst().strftime('%Y-%m-%d')}"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    children = []

    for chunk in chunk_text(report, size=1800):
        children.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": chunk
                            }
                        }
                    ]
                },
            }
        )

    payload = {
        "parent": {
            "type": "page_id",
            "page_id": NOTION_PARENT_PAGE_ID,
        },
        "properties": {
            "title": {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": title
                        }
                    }
                ]
            }
        },
        "children": children[:100],
    }

    print("Notion 저장 시작")

    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Notion 저장 실패: {response.status_code} {response.text}")

    print("Notion 저장 완료")


# =========================
# 메인 실행
# =========================

def main():
    print("Daily Market Report 시작")

    data_packet = build_data_packet()

    if data_packet["article_count"] == 0:
        print("수집된 뉴스가 없습니다. 그래도 보고서를 생성합니다.")

    report = generate_report(data_packet)

    send_email(report)
    send_slack(report)
    send_notion(report)

    print("Daily Market Report 완료")


if __name__ == "__main__":
    main()
