import os
import re
import io
import json
import zipfile
import smtplib
import hashlib
import requests
import xml.etree.ElementTree as ET
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
ALPHAVANTAGE_KEY = os.getenv("ALPHAVANTAGE_KEY")
OPENDART_API_KEY = os.getenv("OPENDART_API_KEY")

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

def alpha_time_from(hours=72):
    """
    Alpha Vantage NEWS_SENTIMENT API용 시간 형식입니다.
    예: 20260614T0700
    """
    target_time = datetime.now(UTC) - timedelta(hours=hours)
    return target_time.strftime("%Y%m%dT%H%M")    

def now_kst():
    return datetime.now(KST)


def since_utc(hours=24):
    return datetime.now(UTC) - timedelta(hours=hours)


def yyyymmdd_kst(days_ago=0):
    target_date = now_kst() - timedelta(days=days_ago)
    return target_date.strftime("%Y%m%d")
    

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
# 뉴스 수집 NEWSAPI
# =========================

def fetch_newsapi():
    if not NEWSAPI_KEY:
        print("NEWSAPI_KEY가 없습니다. 뉴스 수집을 건너뜁니다.")
        return []

    query = (
        "stock OR market OR economy OR inflation OR rates OR "
        "Federal Reserve OR Fed OR Nvidia OR Apple OR Microsoft OR Tesla OR "
        "AI OR semiconductor OR oil OR dollar OR bond OR yield OR "
        "Korea OR KOSPI OR Samsung OR SK Hynix"
    )

    url = "https://newsapi.org/v2/everything"

    params = {
        "q": query,
        "from": since_utc(72).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 50,
        "apiKey": NEWSAPI_KEY,
    }

    print("뉴스 데이터 수집 시작")
    print("NewsAPI query:", query)

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


# =========================
# 뉴스 수집 Alpha Vantage NEWS
# =========================

def fetch_alpha_vantage_news():
    """
    Alpha Vantage NEWS_SENTIMENT API에서 시장 뉴스와 감성 데이터를 가져옵니다.
    """
    if not ALPHAVANTAGE_KEY:
        print("ALPHAVANTAGE_KEY가 없습니다. Alpha Vantage 뉴스 수집을 건너뜁니다.")
        return []

    url = "https://www.alphavantage.co/query"

    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "financial_markets,economy_monetary,economy_macro,technology",
        "time_from": alpha_time_from(72),
        "sort": "LATEST",
        "limit": 50,
        "apikey": ALPHAVANTAGE_KEY,
    }

    print("Alpha Vantage 뉴스 데이터 수집 시작")
    print("Alpha Vantage topics:", params["topics"])
    print("Alpha Vantage time_from:", params["time_from"])

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()

    if "Information" in data:
        print("Alpha Vantage Information:", data.get("Information"))
        return []

    if "Note" in data:
        print("Alpha Vantage Note:", data.get("Note"))
        return []

    if "Error Message" in data:
        print("Alpha Vantage Error:", data.get("Error Message"))
        return []

    feed = data.get("feed", [])

    result = []

    for item in feed:
        title = item.get("title")
        article_url = item.get("url")

        if not title or not article_url:
            continue

        ticker_sentiment = item.get("ticker_sentiment", [])

        related_tickers = []
        for ticker_item in ticker_sentiment[:10]:
            ticker = ticker_item.get("ticker")
            relevance_score = ticker_item.get("relevance_score")
            sentiment_label = ticker_item.get("ticker_sentiment_label")

            if ticker:
                related_tickers.append(
                    {
                        "ticker": ticker,
                        "relevance_score": relevance_score,
                        "sentiment_label": sentiment_label,
                    }
                )

        result.append(
            {
                "source": item.get("source"),
                "title": title,
                "description": item.get("summary"),
                "url": article_url,
                "published_at": item.get("time_published"),
                "provider": "Alpha Vantage",
                "overall_sentiment_score": item.get("overall_sentiment_score"),
                "overall_sentiment_label": item.get("overall_sentiment_label"),
                "topics": item.get("topics", []),
                "related_tickers": related_tickers,
            }
        )

    print(f"Alpha Vantage 뉴스 {len(result)}개 수집 완료")
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

    newsapi_articles = fetch_newsapi()
    alpha_articles = fetch_alpha_vantage_news()
    dart_disclosures = fetch_opendart_disclosures()
    important_dart_disclosures = filter_important_dart_disclosures(
    dart_disclosures,
    max_items=30,
)
    articles.extend(newsapi_articles)
    articles.extend(alpha_articles)

    articles = dedupe_articles(articles)

    data_packet = {
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "article_count": len(articles),
        "newsapi_article_count": len(newsapi_articles),
        "alpha_vantage_article_count": len(alpha_articles),
        "opendart_disclosure_count": len(dart_disclosures),
        "opendart_important_disclosure_count": len(important_dart_disclosures),
        "watchlist_count": len(watchlist),
        "watchlist": watchlist,
        "articles": articles[:100],
        "opendart_disclosures": important_dart_disclosures,
        "opendart_all_disclosures_sample": dart_disclosures[:20],
        "instruction": "NewsAPI, Alpha Vantage, OpenDART 중요 공시 데이터를 근거로 오늘의 증시 분석 보고서를 작성하라.",
    }

    print(f"NewsAPI 기사 수: {len(newsapi_articles)}개")
    print(f"Alpha Vantage 기사 수: {len(alpha_articles)}개")
    print(f"OpenDART 전체 공시 수: {len(dart_disclosures)}개")
    print(f"OpenDART 중요 공시 수: {len(important_dart_disclosures)}개")
    print(f"최종 기사 수: {len(articles)}개")
    print(f"관심 종목 수: {len(watchlist)}개")

    return data_packet



# =========================
# OpenDART 공시 수집
# =========================

IMPORTANT_DART_KEYWORDS = [
    # 실적 관련
    "실적",
    "영업실적",
    "매출액",
    "영업이익",
    "잠정실적",
    "결산실적",
    "분기보고서",
    "반기보고서",
    "사업보고서",

    # 자금 조달 / 주식 수
    "유상증자",
    "무상증자",
    "전환사채",
    "신주인수권",
    "교환사채",
    "사채권",
    "CB",
    "BW",

    # 주주환원
    "자기주식",
    "자사주",
    "배당",
    "현금배당",
    "주식배당",

    # 계약 / 수주
    "단일판매",
    "공급계약",
    "수주",
    "계약체결",
    "대규모",

    # 지배구조 / 경영권
    "최대주주",
    "대표이사",
    "임원",
    "경영권",
    "주주총회",
    "이사회",

    # 구조 변화
    "합병",
    "분할",
    "영업양수",
    "영업양도",
    "타법인",
    "출자",
    "취득",
    "처분",

    # 리스크
    "소송",
    "횡령",
    "배임",
    "감사의견",
    "거래정지",
    "상장폐지",
    "불성실공시",
    "관리종목",
]

def score_dart_disclosure(disclosure):
    """
    OpenDART 공시의 중요도를 점수화합니다.
    공시명에 중요한 키워드가 포함될수록 점수가 올라갑니다.
    """
    report_name = disclosure.get("report_name") or ""
    score = 0
    matched_keywords = []

    for keyword in IMPORTANT_DART_KEYWORDS:
        if keyword.lower() in report_name.lower():
            score += 1
            matched_keywords.append(keyword)

    disclosure["importance_score"] = score
    disclosure["matched_keywords"] = matched_keywords

    return disclosure


def filter_important_dart_disclosures(disclosures, max_items=30):
    """
    OpenDART 공시 중 중요한 공시만 선별합니다.
    중요 키워드가 포함된 공시를 우선 표시합니다.
    """
    scored = []

    for disclosure in disclosures:
        scored.append(score_dart_disclosure(disclosure))

    important = [
        item for item in scored
        if item.get("importance_score", 0) > 0
    ]

    # 중요도 높은 순, 최신 접수일 순으로 정렬
    important.sort(
        key=lambda x: (
            x.get("importance_score", 0),
            x.get("receipt_date") or ""
        ),
        reverse=True,
    )

    return important[:max_items]


def load_dart_watchlist():
    """
    dart_watchlist.txt 파일에서 한국 종목코드 또는 회사명을 읽습니다.
    종목코드 예:
    005930
    000660
    """
    path = "dart_watchlist.txt"

    if not os.path.exists(path):
        print("dart_watchlist.txt 파일이 없습니다. OpenDART 관심 공시 수집은 건너뜁니다.")
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

    print(f"OpenDART 관심 기업 {len(watchlist)}개 로드 완료")
    return watchlist


def fetch_dart_corp_codes():
    """
    OpenDART에서 전체 회사 고유번호 목록을 가져옵니다.
    corpCode.xml은 ZIP 파일 안에 XML로 들어옵니다.
    """
    if not OPENDART_API_KEY:
        print("OPENDART_API_KEY가 없습니다. 회사 고유번호 수집을 건너뜁니다.")
        return []

    url = "https://opendart.fss.or.kr/api/corpCode.xml"

    params = {
        "crtfc_key": OPENDART_API_KEY
    }

    print("OpenDART 회사 고유번호 수집 시작")

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    # 인증키 오류 등으로 ZIP이 아닌 응답이 올 수 있으므로 확인
    content_type = response.headers.get("Content-Type", "")

    if "zip" not in content_type.lower() and not response.content.startswith(b"PK"):
        print("OpenDART 회사 고유번호 응답이 ZIP이 아닙니다.")
        print(response.text[:500])
        return []

    zip_file = zipfile.ZipFile(io.BytesIO(response.content))
    xml_data = zip_file.read("CORPCODE.xml").decode("utf-8")

    root = ET.fromstring(xml_data)

    corp_codes = []

    for item in root.findall("list"):
        corp_codes.append(
            {
                "corp_code": item.findtext("corp_code"),
                "corp_name": item.findtext("corp_name"),
                "stock_code": item.findtext("stock_code"),
                "modify_date": item.findtext("modify_date"),
            }
        )

    print(f"OpenDART 회사 고유번호 {len(corp_codes)}개 수집 완료")
    return corp_codes


def find_dart_companies(corp_codes, watchlist):
    """
    dart_watchlist.txt의 종목코드 또는 회사명으로 OpenDART corp_code를 찾습니다.
    """
    companies = []

    for target in watchlist:
        target_clean = target.strip()

        matched = None

        for company in corp_codes:
            stock_code = (company.get("stock_code") or "").strip()
            corp_name = (company.get("corp_name") or "").strip()

            # 6자리 종목코드로 매칭
            if target_clean.isdigit() and len(target_clean) == 6:
                if stock_code == target_clean:
                    matched = company
                    break

            # 회사명으로 매칭
            else:
                if target_clean.lower() == corp_name.lower():
                    matched = company
                    break

        if matched:
            companies.append(matched)
        else:
            print(f"OpenDART 회사 매칭 실패: {target_clean}")

    print(f"OpenDART 매칭 기업 수: {len(companies)}개")
    return companies


def fetch_opendart_disclosures():
    """
    dart_watchlist.txt에 있는 한국 기업의 최근 공시를 가져옵니다.
    """
    if not OPENDART_API_KEY:
        print("OPENDART_API_KEY가 없습니다. OpenDART 공시 수집을 건너뜁니다.")
        return []

    watchlist = load_dart_watchlist()

    if not watchlist:
        print("OpenDART 관심 기업 목록이 비어 있습니다.")
        return []

    corp_codes = fetch_dart_corp_codes()

    if not corp_codes:
        print("OpenDART 회사 고유번호 목록이 비어 있습니다.")
        return []

    companies = find_dart_companies(corp_codes, watchlist)

    if not companies:
        print("OpenDART 매칭 기업이 없습니다.")
        return []

    bgn_de = yyyymmdd_kst(days_ago=7)
    end_de = yyyymmdd_kst(days_ago=0)

    disclosures = []

    print(f"OpenDART 공시 수집 시작: {bgn_de} ~ {end_de}")

    for company in companies:
        corp_code = company["corp_code"]
        corp_name = company["corp_name"]
        stock_code = company.get("stock_code")

        url = "https://opendart.fss.or.kr/api/list.json"

        params = {
            "crtfc_key": OPENDART_API_KEY,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_count": 100,
        }

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        status = data.get("status")
        message = data.get("message")

        # 000: 정상, 013: 조회된 데이터 없음
        if status == "013":
            print(f"OpenDART 공시 없음: {corp_name}({stock_code})")
            continue

        if status != "000":
            print(f"OpenDART 오류: {corp_name}({stock_code}) status={status}, message={message}")
            continue

        for item in data.get("list", []):
            rcept_no = item.get("rcept_no")

            disclosures.append(
                {
                    "provider": "OpenDART",
                    "corp_name": item.get("corp_name"),
                    "stock_code": item.get("stock_code"),
                    "corp_code": item.get("corp_code"),
                    "report_name": item.get("report_nm"),
                    "receipt_no": rcept_no,
                    "receipt_date": item.get("rcept_dt"),
                    "submitter": item.get("flr_nm"),
                    "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else None,
                }
            )

    print(f"OpenDART 공시 {len(disclosures)}개 수집 완료")
    return disclosures


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

OpenDART 공시 활용 요구사항:
- data_packet 안의 opendart_disclosures 항목은 중요 키워드 기준으로 선별된 공시입니다.
- OpenDART 공시는 한국 기업 분석에서 공식 자료로 취급하세요.
- 중요 공시가 있으면 관심 종목 영향 분석과 리스크 요인에 반영하세요.
- 공시 제목만 보고 과도하게 해석하지 말고, 확인이 필요한 부분은 “공시 원문 확인 필요”라고 표시하세요.
- 한국 기업 공시 체크 섹션은 Python 코드가 보고서 맨 마지막에 자동으로 추가하므로, 본문에서는 중복 표를 만들지 마세요.

OpenDART 공시 출력 위치 규칙:
- "## 한국 기업 공시 체크" 섹션은 반드시 보고서의 맨 마지막에 배치하세요.
- "## 출처 및 확인 필요 사항" 섹션이 있다면, 그 섹션보다도 뒤에 배치하세요.
- 보고서의 마지막 섹션 제목은 반드시 "## 한국 기업 공시 체크"여야 합니다.
- data_packet 안의 opendart_disclosures 개수가 1개 이상이면 이 섹션을 생략하지 마세요.


Alpha Vantage 데이터 활용 요구사항:
- provider가 "Alpha Vantage"인 뉴스는 감성 분석 정보를 함께 참고하세요.
- overall_sentiment_label이 Bullish, Bearish, Neutral 중 무엇인지 확인하세요.
- related_tickers가 있으면 관련 종목 영향 분석에 반영하세요.
- NewsAPI와 Alpha Vantage가 같은 이슈를 다루면 중복 이슈로 보지 말고 하나의 핵심 이슈로 통합하세요.

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


def build_opendart_section(data_packet):
    """
    OpenDART 중요 공시 데이터를 보고서 맨 마지막에 강제로 붙입니다.
    전체 공시 중 중요 공시만 표로 보여주고,
    나머지는 개수만 요약합니다.
    """
    important_disclosures = data_packet.get("opendart_disclosures", [])
    total_count = data_packet.get("opendart_disclosure_count", 0)
    important_count = data_packet.get("opendart_important_disclosure_count", len(important_disclosures))

    section = "\n\n## 한국 기업 공시 체크\n\n"

    section += f"- OpenDART 전체 공시 수: {total_count}개\n"
    section += f"- 중요 키워드 기준 선별 공시 수: {important_count}개\n\n"

    if not important_disclosures:
        section += "관심 기업 기준 최근 OpenDART 주요 공시 없음\n"
        return section

    section += "| 회사 | 종목코드 | 공시명 | 접수일 | 중요 키워드 | 해석 | 원문 링크 |\n"
    section += "|---|---:|---|---|---|---|---|\n"

    for item in important_disclosures[:30]:
        corp_name = item.get("corp_name") or ""
        stock_code = item.get("stock_code") or ""
        report_name = item.get("report_name") or ""
        receipt_date = item.get("receipt_date") or ""
        viewer_url = item.get("viewer_url") or ""
        matched_keywords = item.get("matched_keywords") or []

        keyword_text = ", ".join(matched_keywords) if matched_keywords else "확인 필요"

        section += (
            f"| {corp_name} "
            f"| {stock_code} "
            f"| {report_name} "
            f"| {receipt_date} "
            f"| {keyword_text} "
            f"| 공시 원문 확인 필요 "
            f"| {viewer_url} |\n"
        )

    if total_count > important_count:
        other_count = total_count - important_count
        section += f"\n기타 일반 공시 {other_count}개는 중요 키워드 기준에서 제외했습니다.\n"

    return section

    
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

def extract_section_lines(report, section_title, max_lines=8):
    """
    보고서에서 특정 섹션의 핵심 줄만 뽑습니다.
    예: '오늘 한 줄 요약', '핵심 이슈 5개', '관심 종목 영향 분석'
    """
    lines = report.splitlines()
    collecting = False
    result = []

    for line in lines:
        raw_line = line.strip()

        if not raw_line:
            continue

        # 원하는 섹션 시작
        if raw_line.startswith("## ") and section_title in raw_line:
            collecting = True
            continue

        # 다음 큰 섹션이 나오면 중단
        if collecting and raw_line.startswith("## "):
            break

        if collecting:
            cleaned = raw_line.replace("**", "").strip()

            # Markdown 제목 기호 정리
            cleaned = cleaned.replace("### ", "").strip()

            # 목록 기호 정리
            cleaned = cleaned.lstrip("-").strip()
            cleaned = cleaned.lstrip("*").strip()

            # Markdown 표 구분선 제거
            if cleaned.startswith("|---") or cleaned.startswith("| ---"):
                continue

            # 너무 의미 없는 줄 제거
            if cleaned in ["|", "---"]:
                continue

            if cleaned:
                result.append(cleaned)

            if len(result) >= max_lines:
                break

    return result


def truncate_slack_message(message, max_chars=9000):
    """
    Slack 메시지가 너무 길어지는 것을 방지합니다.
    """
    if len(message) <= max_chars:
        return message

    shortened = message[:max_chars]

    # 문장 중간에서 끊기보다 마지막 줄 기준으로 자르기
    if "\n" in shortened:
        shortened = shortened.rsplit("\n", 1)[0]

    shortened += "\n\n...(Slack 요약이 길어 일부 생략했습니다. 전체 보고서는 Notion에서 확인하세요.)"
    return shortened


def build_slack_summary(report, data_packet=None, notion_url=None):
    """
    Slack에 보낼 확장 요약 메시지를 만듭니다.
    """
    data_packet = data_packet or {}

    one_line_summary = extract_section_lines(report, "오늘 한 줄 요약", max_lines=6)
    key_issues = extract_section_lines(report, "핵심 이슈 5개", max_lines=12)
    watchlist_lines = extract_section_lines(report, "관심 종목 영향 분석", max_lines=8)
    risk_lines = extract_section_lines(report, "리스크 요인", max_lines=6)
    dart_lines = extract_section_lines(report, "한국 기업 공시 체크", max_lines=8)
    checklist_lines = extract_section_lines(report, "오늘 체크리스트", max_lines=5)

    if not one_line_summary:
        one_line_summary = ["보고서 생성 완료. 전체 내용은 Notion 또는 이메일에서 확인하세요."]

    newsapi_count = data_packet.get("newsapi_article_count", 0)
    alpha_count = data_packet.get("alpha_vantage_article_count", 0)
    opendart_count = data_packet.get("opendart_disclosure_count", 0)
    total_article_count = data_packet.get("article_count", 0)
    watchlist_count = data_packet.get("watchlist_count", 0)

    message = "📈 *오늘의 증시 분석 보고서 생성 완료*\n\n"

    message += "*1. 오늘 한 줄 요약*\n"
    for line in one_line_summary:
        message += f"• {line}\n"

    if key_issues:
        message += "\n*2. 핵심 이슈*\n"
        for line in key_issues:
            message += f"• {line}\n"

    if watchlist_lines:
        message += "\n*3. 관심 종목 영향 분석*\n"
        for line in watchlist_lines:
            message += f"• {line}\n"

    if risk_lines:
        message += "\n*4. 주요 리스크*\n"
        for line in risk_lines:
            message += f"• {line}\n"

    if dart_lines:
        message += "\n*5. 한국 기업 공시 체크*\n"
        for line in dart_lines:
            message += f"• {line}\n"

    if checklist_lines:
        message += "\n*6. 오늘 체크리스트*\n"
        for line in checklist_lines:
            message += f"• {line}\n"

    message += "\n*수집 데이터*\n"
    message += f"• NewsAPI: {newsapi_count}개\n"
    message += f"• Alpha Vantage: {alpha_count}개\n"
    message += f"• OpenDART 공시: {opendart_count}개\n"
    message += f"• 최종 뉴스 기사: {total_article_count}개\n"
    message += f"• 관심 종목: {watchlist_count}개\n"

    message += "\n*전체 보고서*\n"

    if notion_url:
        message += f"• Notion에서 보기: {notion_url}\n"
    else:
        message += "• Notion 링크 없음. 이메일 또는 Notion 페이지를 직접 확인하세요.\n"

    message += "\n_이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다._"

    return truncate_slack_message(message, max_chars=9000)

def send_slack(report, data_packet=None, notion_url=None):
    """
    Slack에는 전체 보고서가 아니라 짧은 요약 메시지만 보냅니다.
    """
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 없어서 Slack 발송을 건너뜁니다.")
        return

    slack_text = build_slack_summary(
        report=report,
        data_packet=data_packet,
        notion_url=notion_url,
    )

    payload = {
        "text": slack_text
    }

    print("Slack 요약 메시지 발송 시작")

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Slack 발송 실패: {response.status_code} {response.text}")

    print("Slack 요약 메시지 발송 완료")



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
        return None

    if not NOTION_PARENT_PAGE_ID:
        print("NOTION_PARENT_PAGE_ID가 없어서 Notion 저장을 건너뜁니다.")
        return None

    created_at = now_kst()
    title = f"증시 분석 보고서 - {created_at.strftime('%Y-%m-%d %H:%M KST')}"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    # Notion 페이지 맨 위에 들어갈 메타 정보
    intro_blocks = [
        make_text_block("heading_1", title),
        make_text_block("paragraph", f"생성 시각: {created_at.strftime('%Y-%m-%d %H:%M:%S KST')}"),
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

    notion_url = page.get("url")

    print("Notion 저장 완료")
    print(f"Notion URL: {notion_url}")

    return notion_url

# =========================
# 메인 실행
# =========================

def main():
    print("Daily Market Report 시작")

    data_packet = build_data_packet()

    if data_packet["article_count"] == 0:
        print("수집된 뉴스가 없습니다. 그래도 보고서를 생성합니다.")

    report = generate_report(data_packet)

    if not report:
        raise ValueError("OpenAI 보고서 생성 결과가 비어 있습니다.")

    report = report + build_opendart_section(data_packet)

    send_email(report)

    notion_url = send_notion(report)

    send_slack(
        report=report,
        data_packet=data_packet,
        notion_url=notion_url,
    )

    print("Daily Market Report 완료")


if __name__ == "__main__":
    main()
