import os
import re
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
# 관심종목 함수
# =========================

def load_watchlist():
    """
    watchlist.txt 파일에서 관심 종목 목록을 읽습니다.
    """
    path = "watchlist.txt"

    if not os.path.exists(path):
        print("watchlist.txt 파일이 없습니다. 관심 종목 분석은 건너뜁니다.")
        return []

    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    watchlist = []

    for line in lines:
        item = line.strip()

        if not item:
            continue

        if item.startswith("#"):
            continue

        watchlist.append(item)

    print(f"관심 종목 {len(watchlist)}개 로드 완료")
    return watchlist


# =========================
# 뉴스 수집
# =========================

def fetch_newsapi():
    if not NEWSAPI_KEY:
        print("NEWSAPI_KEY가 없습니다. 뉴스 수집을 건너뜁니다.")
        return []

    query = (
    "stock OR market OR economy OR inflation OR interest rates OR "
    "Federal Reserve OR Nvidia OR Apple OR Tesla OR AI OR semiconductor OR "
    "oil OR dollar OR bond OR Korea OR KOSPI OR Samsung"
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

    data = response.json()

    print("NewsAPI status:", data.get("status"))
    print("NewsAPI totalResults:", data.get("totalResults"))
    print("NewsAPI message:", data.get("message"))

    articles = data.get("articles", [])

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
    watchlist = load_watchlist()

    articles.extend(fetch_newsapi())

    articles = dedupe_articles(articles)

    data_packet = {
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "article_count": len(articles),
        "watchlist_count": len(watchlist),
        "watchlist": watchlist,
        "articles": articles[:80],
        "instruction": "이 데이터와 관심 종목 목록을 근거로 오늘의 증시 분석 보고서를 작성하라.",
    }

    print(f"최종 기사 수: {len(articles)}개")
    print(f"관심 종목 수: {len(watchlist)}개")
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

관심 종목 분석 요구사항:
- data_packet 안의 watchlist 항목을 반드시 확인하세요.
- 각 관심 종목에 대해 오늘 뉴스와 직접 관련이 있으면 상세히 분석하세요.
- 직접 관련 뉴스가 없더라도 매크로, 금리, 환율, 섹터 흐름 관점에서 간단히 영향도를 평가하세요.
- 관심 종목별 영향도를 긍정 / 부정 / 중립 / 확인 필요 중 하나로 표시하세요.
- 관심 종목 분석은 표로 작성하세요.

Notion 저장 형식 요구사항:
- 섹션 제목은 반드시 Markdown 제목 형식으로 작성하세요.
- 큰 섹션은 ## 제목 형식을 사용하세요.
- 하위 섹션은 ### 제목 형식을 사용하세요.
- 핵심 항목은 - 목록 형식으로 작성하세요.
- 표가 필요한 경우 Markdown 표 형식으로 작성하세요.
- 문단을 너무 길게 쓰지 말고 짧게 나누세요.
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
# Notion 저장 - 개선 버전
# =========================

def split_text(text, size=1800):
    """
    Notion 한 블록에 너무 긴 텍스트가 들어가지 않도록 나눕니다.
    """
    return [text[i:i + size] for i in range(0, len(text), size)]


def rich_text(text):
    """
    Notion rich_text 형식으로 변환합니다.
    """
    return [
        {
            "type": "text",
            "text": {
                "content": text
            }
        }
    ]


def make_text_block(block_type, text):
    """
    Notion 텍스트 블록을 만듭니다.
    block_type 예시:
    paragraph, heading_1, heading_2, heading_3,
    bulleted_list_item, numbered_list_item, quote
    """
    return {
        "object": "block",
        "type": block_type,
        block_type: {
            "rich_text": rich_text(text)
        }
    }


def make_divider_block():
    """
    Notion 구분선을 만듭니다.
    """
    return {
        "object": "block",
        "type": "divider",
        "divider": {}
    }


def clean_markdown_text(text):
    """
    Markdown 기호를 Notion에 넣기 좋게 약간 정리합니다.
    """
    text = text.strip()

    # 굵게 표시용 ** 제거
    text = text.replace("**", "")

    return text


def markdown_to_notion_blocks(markdown_text):
    """
    OpenAI가 만든 Markdown 보고서를 Notion 블록으로 변환합니다.

    변환 규칙:
    # 제목      → heading_1
    ## 제목     → heading_2
    ### 제목    → heading_3
    - 항목      → bulleted_list_item
    1. 항목     → numbered_list_item
    > 문장      → quote
    ---        → divider
    일반 문장   → paragraph
    """
    blocks = []

    lines = markdown_text.splitlines()

    for line in lines:
        raw_line = line.strip()

        if not raw_line:
            continue

        if raw_line in ["---", "***", "___"]:
            blocks.append(make_divider_block())
            continue

        block_type = "paragraph"
        text = raw_line

        if raw_line.startswith("### "):
            block_type = "heading_3"
            text = raw_line.replace("### ", "", 1)

        elif raw_line.startswith("## "):
            block_type = "heading_2"
            text = raw_line.replace("## ", "", 1)

        elif raw_line.startswith("# "):
            block_type = "heading_1"
            text = raw_line.replace("# ", "", 1)

        elif raw_line.startswith("- "):
            block_type = "bulleted_list_item"
            text = raw_line.replace("- ", "", 1)

        elif raw_line.startswith("* "):
            block_type = "bulleted_list_item"
            text = raw_line.replace("* ", "", 1)

        elif re.match(r"^\d+\.\s+", raw_line):
            block_type = "numbered_list_item"
            text = re.sub(r"^\d+\.\s+", "", raw_line)

        elif raw_line.startswith("> "):
            block_type = "quote"
            text = raw_line.replace("> ", "", 1)

        text = clean_markdown_text(text)

        # 너무 긴 줄은 여러 블록으로 나눕니다.
        chunks = split_text(text, size=1800)

        for index, chunk in enumerate(chunks):
            # 제목이 너무 길어서 잘린 경우, 첫 줄만 제목으로 두고 나머지는 paragraph로 처리
            if index == 0:
                blocks.append(make_text_block(block_type, chunk))
            else:
                blocks.append(make_text_block("paragraph", chunk))

    return blocks


def append_blocks_to_notion_page(page_id, blocks, headers):
    """
    블록이 많을 경우 Notion 페이지에 나눠서 추가합니다.
    Notion API는 한 번에 너무 많은 children을 넣으면 실패할 수 있으므로 batch 처리합니다.
    """
    batch_size = 80

    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]

        response = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": batch},
            timeout=30,
        )

        if response.status_code != 200:
            raise Exception(f"Notion 블록 추가 실패: {response.status_code} {response.text}")


def send_notion(report):
    """
    생성된 보고서를 Notion 부모 페이지 아래에 보기 좋은 형식으로 저장합니다.
    """
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

    # Notion 페이지 맨 위에 들어갈 메타 정보
    intro_blocks = [
        make_text_block("heading_1", title),
        make_text_block("paragraph", f"생성 시각: {now_kst().strftime('%Y-%m-%d %H:%M:%S KST')}"),
        make_text_block("paragraph", "자동 생성된 증시 분석 보고서입니다."),
        make_text_block("paragraph", "주의: 이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다."),
        make_divider_block(),
    ]

    # OpenAI 보고서 본문을 Notion 블록으로 변환
    report_blocks = markdown_to_notion_blocks(report)

    all_blocks = intro_blocks + report_blocks

    # 처음 페이지 생성 시에는 일부 블록만 넣고,
    # 나머지는 append API로 추가합니다.
    first_blocks = all_blocks[:80]
    remaining_blocks = all_blocks[80:]

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
        "children": first_blocks,
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

    page = response.json()
    page_id = page["id"]

    if remaining_blocks:
        print(f"Notion 추가 블록 저장 시작: {len(remaining_blocks)}개")
        append_blocks_to_notion_page(page_id, remaining_blocks, headers)
        print("Notion 추가 블록 저장 완료")

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
