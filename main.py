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
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5.5").strip()

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

# REPORT_MODE
# - morning: 아침 증시 분석 보고서
# - close: 장마감 리뷰 보고서
REPORT_MODE = os.getenv("REPORT_MODE", "morning").strip().lower()

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 보고서 모드 함수
# =========================

def get_report_mode():
    """
    실행 모드를 반환합니다.
    GitHub Actions에서 REPORT_MODE=close로 주면 장마감 리뷰로 실행됩니다.
    """
    mode = (os.getenv("REPORT_MODE", REPORT_MODE) or "morning").strip().lower()

    if mode in ["close", "closing", "market_close", "after_close", "end"]:
        return "close"

    return "morning"


def get_report_label(mode=None):
    mode = mode or get_report_mode()

    if mode == "close":
        return "장마감 리뷰"

    return "증시 분석"


def get_report_emoji(mode=None):
    mode = mode or get_report_mode()

    if mode == "close":
        return "📉"

    return "📈"


def get_report_title(created_at=None, mode=None):
    mode = mode or get_report_mode()
    created_at = created_at or now_kst()
    label = get_report_label(mode)
    return f"{label} 보고서 - {created_at.strftime('%Y-%m-%d %H:%M KST')}"


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


def yyyymmdd_kst(days_ago=0):
    """
    OpenDART API용 날짜 형식입니다.
    예: 20260614
    """
    target_date = now_kst() - timedelta(days=days_ago)
    return target_date.strftime("%Y%m%d")


# =========================
# 파일 로드 함수
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


# =========================
# 뉴스 수집: NewsAPI
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
# 뉴스 수집: Alpha Vantage NEWS_SENTIMENT
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

        related_tickers = []

        for ticker_item in item.get("ticker_sentiment", [])[:10]:
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


# =========================
# OpenDART 중요 공시 필터링
# =========================

DART_GRADE_KEYWORDS = {
    "A": [
        "상장폐지",
        "거래정지",
        "관리종목",
        "불성실공시",
        "감사의견",
        "의견거절",
        "부적정",
        "한정",
        "횡령",
        "배임",
        "회생절차",
        "파산",
        "부도",
        "소송",
        "유상증자",
        "전환사채",
        "신주인수권",
        "교환사채",
        "최대주주",
        "경영권",
    ],
    "B": [
        "영업실적",
        "잠정실적",
        "매출액",
        "영업이익",
        "실적",
        "배당",
        "현금배당",
        "자기주식",
        "자사주",
        "공급계약",
        "단일판매",
        "수주",
        "계약체결",
        "대규모",
        "합병",
        "분할",
        "영업양수",
        "영업양도",
        "타법인",
        "취득",
        "처분",
        "출자",
    ],
    "C": [
        "대표이사",
        "임원",
        "주주총회",
        "이사회",
        "정정",
        "분기보고서",
        "반기보고서",
        "사업보고서",
        "사외이사",
        "감사보고서",
    ],
}

DART_GRADE_SCORE = {
    "A": 100,
    "B": 50,
    "C": 10,
    "기타": 0,
}


def score_dart_disclosure(disclosure):
    """
    OpenDART 공시를 A/B/C 등급으로 분류합니다.
    A급 키워드가 하나라도 있으면 A급,
    없으면 B급,
    없으면 C급,
    없으면 기타로 분류합니다.
    """
    report_name = disclosure.get("report_name") or ""

    matched_keywords = []
    grade = "기타"

    for current_grade in ["A", "B", "C"]:
        current_matches = []

        for keyword in DART_GRADE_KEYWORDS[current_grade]:
            if keyword.lower() in report_name.lower():
                current_matches.append(keyword)

        if current_matches:
            grade = current_grade
            matched_keywords = current_matches
            break

    scored = dict(disclosure)
    scored["importance_grade"] = grade
    scored["importance_score"] = DART_GRADE_SCORE.get(grade, 0)
    scored["matched_keywords"] = matched_keywords

    return scored


def score_all_dart_disclosures(disclosures):
    return [score_dart_disclosure(item) for item in disclosures]


def filter_important_dart_disclosures(scored_disclosures, max_items=30):
    """
    OpenDART 공시 중 A/B/C 등급 공시만 선별합니다.
    기타 공시는 보고서 표에서는 제외합니다.
    """
    important = [
        item for item in scored_disclosures
        if item.get("importance_grade") in ["A", "B", "C"]
    ]

    important.sort(
        key=lambda x: (
            x.get("importance_score", 0),
            x.get("receipt_date") or ""
        ),
        reverse=True,
    )

    return important[:max_items]


def count_dart_grades(scored_disclosures):
    """
    OpenDART 공시 등급별 개수를 계산합니다.
    """
    counts = {
        "A": 0,
        "B": 0,
        "C": 0,
        "기타": 0,
    }

    for disclosure in scored_disclosures:
        grade = disclosure.get("importance_grade", "기타")
        counts[grade] = counts.get(grade, 0) + 1

    return counts


# =========================
# OpenDART 공시 수집
# =========================

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

            if target_clean.isdigit() and len(target_clean) == 6:
                if stock_code == target_clean:
                    matched = company
                    break
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
# 위험도 점수
# =========================

def get_risk_level(score):
    """
    위험도 점수를 사람이 읽기 쉬운 등급으로 변환합니다.
    """
    if score <= 3:
        return "안정"
    elif score <= 6:
        return "보통"
    elif score <= 8:
        return "주의"
    else:
        return "고위험"


def calculate_market_risk(data_packet):
    """
    수집된 뉴스, Alpha Vantage 감성, OpenDART 공시를 바탕으로
    오늘의 시장 위험도 점수를 계산합니다.

    점수 기준:
    1~3점: 안정
    4~6점: 보통
    7~8점: 주의
    9~10점: 고위험
    """
    score = 4
    reasons = []

    article_count = data_packet.get("article_count", 0)
    alpha_count = data_packet.get("alpha_vantage_article_count", 0)
    opendart_total_count = data_packet.get("opendart_disclosure_count", 0)
    opendart_important_count = data_packet.get("opendart_important_disclosure_count", 0)

    grade_counts = data_packet.get("opendart_grade_counts", {})
    a_count = grade_counts.get("A", 0)
    b_count = grade_counts.get("B", 0)

    articles = data_packet.get("articles", [])

    if a_count >= 3:
        score += 3
        reasons.append(f"OpenDART A급 공시가 {a_count}건 발생했습니다.")
    elif a_count >= 1:
        score += 2
        reasons.append(f"OpenDART A급 공시가 {a_count}건 발생했습니다.")

    if b_count >= 10:
        score += 2
        reasons.append(f"OpenDART B급 공시가 {b_count}건으로 많습니다.")
    elif b_count >= 5:
        score += 1
        reasons.append(f"OpenDART B급 공시가 {b_count}건 발생했습니다.")

    if opendart_important_count >= 20:
        score += 2
        reasons.append(f"중요 공시가 {opendart_important_count}건으로 많습니다.")
    elif opendart_important_count >= 10:
        score += 1
        reasons.append(f"중요 공시가 {opendart_important_count}건 발생했습니다.")

    alpha_articles = [
        item for item in articles
        if item.get("provider") == "Alpha Vantage"
    ]

    bearish_count = 0

    for item in alpha_articles:
        label = (item.get("overall_sentiment_label") or "").lower()

        if "bearish" in label:
            bearish_count += 1

    if alpha_count > 0:
        bearish_ratio = bearish_count / alpha_count

        if bearish_ratio >= 0.4:
            score += 2
            reasons.append(f"Alpha Vantage 부정 감성 뉴스 비중이 높습니다. Bearish {bearish_count}/{alpha_count}건")
        elif bearish_ratio >= 0.2:
            score += 1
            reasons.append(f"Alpha Vantage 부정 감성 뉴스가 일부 확인됩니다. Bearish {bearish_count}/{alpha_count}건")

    if article_count < 10:
        score += 1
        reasons.append("수집된 뉴스가 적어 시장 해석의 불확실성이 있습니다.")

    if opendart_total_count >= 100:
        score += 1
        reasons.append(f"OpenDART 전체 공시가 {opendart_total_count}건으로 많아 이벤트 점검이 필요합니다.")

    score = max(1, min(10, score))

    if not reasons:
        reasons.append("특별한 고위험 신호는 제한적입니다.")

    return {
        "score": score,
        "level": get_risk_level(score),
        "reasons": reasons[:5],
    }


def build_risk_section(data_packet):
    """
    보고서 맨 위에 붙일 위험도 점수 섹션을 만듭니다.
    """
    risk = data_packet.get("market_risk", {})
    mode = data_packet.get("report_mode") or get_report_mode()

    score = risk.get("score", "확인 필요")
    level = risk.get("level", "확인 필요")
    reasons = risk.get("reasons", [])

    heading = "장마감 위험도 점검" if mode == "close" else "오늘의 위험도 점수"
    section = f"## {heading}\n\n"
    section += f"- 점수: {score} / 10\n"
    section += f"- 판단: {level}\n"
    section += "- 근거:\n"

    for reason in reasons:
        section += f"  - {reason}\n"

    section += "\n"

    return section


# =========================
# 데이터 패킷 생성
# =========================

def build_data_packet():
    mode = get_report_mode()
    report_label = get_report_label(mode)

    articles = []
    watchlist = load_watchlist()

    newsapi_articles = fetch_newsapi()
    alpha_articles = fetch_alpha_vantage_news()
    dart_disclosures = fetch_opendart_disclosures()

    scored_dart_disclosures = score_all_dart_disclosures(dart_disclosures)
    important_dart_disclosures = filter_important_dart_disclosures(
        scored_dart_disclosures,
        max_items=30,
    )
    dart_grade_counts = count_dart_grades(scored_dart_disclosures)

    articles.extend(newsapi_articles)
    articles.extend(alpha_articles)
    articles = dedupe_articles(articles)

    data_packet = {
        "report_mode": mode,
        "report_label": report_label,
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "article_count": len(articles),
        "newsapi_article_count": len(newsapi_articles),
        "alpha_vantage_article_count": len(alpha_articles),
        "opendart_disclosure_count": len(dart_disclosures),
        "opendart_important_disclosure_count": len(important_dart_disclosures),
        "opendart_grade_counts": dart_grade_counts,
        "watchlist_count": len(watchlist),
        "watchlist": watchlist,
        "articles": articles[:100],
        "opendart_disclosures": important_dart_disclosures,
        "opendart_all_disclosures_sample": scored_dart_disclosures[:20],
        "instruction": f"NewsAPI, Alpha Vantage, OpenDART 중요 공시 데이터를 근거로 오늘의 {report_label} 보고서를 작성하라.",
    }

    market_risk = calculate_market_risk(data_packet)
    data_packet["market_risk"] = market_risk

    print(f"보고서 모드: {report_label}")
    print(f"NewsAPI 기사 수: {len(newsapi_articles)}개")
    print(f"Alpha Vantage 기사 수: {len(alpha_articles)}개")
    print(f"OpenDART 전체 공시 수: {len(dart_disclosures)}개")
    print(f"OpenDART 중요 공시 수: {len(important_dart_disclosures)}개")
    print(f"OpenDART 등급별 공시 수: {dart_grade_counts}")
    print(f"최종 기사 수: {len(articles)}개")
    print(f"관심 종목 수: {len(watchlist)}개")
    print(f"오늘의 위험도 점수: {market_risk['score']} / 10")
    print(f"오늘의 위험도 판단: {market_risk['level']}")

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


def build_report_user_input(data_packet):
    """
    보고서 모드에 따라 OpenAI에 전달할 작성 요청을 만듭니다.
    """
    mode = data_packet.get("report_mode") or get_report_mode()
    report_label = data_packet.get("report_label") or get_report_label(mode)
    generated_at = now_kst().strftime('%Y-%m-%d %H:%M KST')
    data_json = json.dumps(data_packet, ensure_ascii=False, indent=2)

    common_rules = f"""
DATA:
{data_json}

공통 작성 원칙:
- 한국어로 작성하세요.
- Markdown 형식으로 작성하세요.
- 가능한 경우 기사 출처와 URL을 함께 표시하세요.
- 확실하지 않은 데이터는 반드시 "확인 필요"라고 표시하세요.
- 투자 권유가 아니라 참고용 분석 자료라는 문구를 포함하세요.
- 문단을 너무 길게 쓰지 말고 짧게 나누세요.

위험도 점수 활용 요구사항:
- data_packet 안의 market_risk를 참고하세요.
- 위험도 점수 섹션은 Python 코드가 보고서 맨 위에 자동 추가하므로 본문에서 같은 섹션을 반복하지 마세요.
- 다만 리스크 요인과 체크리스트에는 위험도 점수의 근거를 반영하세요.

OpenDART 공시 활용 요구사항:
- data_packet 안의 opendart_disclosures 항목은 중요도 A/B/C 기준으로 선별된 공시입니다.
- OpenDART 공시는 한국 기업 분석에서 공식 자료로 취급하세요.
- 중요 공시가 있으면 관심 종목 영향 분석과 리스크 요인에 반영하세요.
- 공시 제목만 보고 과도하게 해석하지 말고, 확인이 필요한 부분은 "공시 원문 확인 필요"라고 표시하세요.
- "## 한국 기업 공시 체크" 섹션은 Python 코드가 보고서 맨 마지막에 자동으로 추가하므로 본문에서는 중복 표를 만들지 마세요.

Alpha Vantage 데이터 활용 요구사항:
- provider가 "Alpha Vantage"인 뉴스는 감성 분석 정보를 함께 참고하세요.
- overall_sentiment_label이 Bullish, Bearish, Neutral 중 무엇인지 확인하세요.
- related_tickers가 있으면 관련 종목 영향 분석에 반영하세요.
- NewsAPI와 Alpha Vantage가 같은 이슈를 다루면 중복 이슈로 보지 말고 하나의 핵심 이슈로 통합하세요.

관심 종목 분석 요구사항:
- data_packet 안의 watchlist 항목을 반드시 확인하세요.
- 각 관심 종목에 대해 오늘 뉴스와 직접 관련이 있으면 상세히 분석하세요.
- 직접 관련 뉴스가 없더라도 매크로, 금리, 환율, 섹터 흐름 관점에서 간단히 영향도를 평가하세요.
- 관심 종목별 영향도는 긍정 / 부정 / 중립 / 확인 필요 중 하나로 표시하세요.
- 관심 종목 분석은 표로 작성하세요.

Notion 저장 형식 요구사항:
- 큰 섹션은 ## 제목 형식을 사용하세요.
- 하위 섹션은 ### 제목 형식을 사용하세요.
- 핵심 항목은 - 목록 형식으로 작성하세요.
- 표가 필요한 경우 Markdown 표 형식으로 작성하세요.
"""

    if mode == "close":
        return f"""
오늘 날짜: {generated_at}
보고서 종류: {report_label}

아래는 한국장 마감 이후 확인할 시장 관련 데이터입니다.
실제 KOSPI/KOSDAQ 등락률, 종가, 거래대금 데이터가 DATA에 없으면 임의로 만들지 말고 "확인 필요"라고 표시하세요.
뉴스, Alpha Vantage 감성, OpenDART 공시, 관심 종목 목록을 바탕으로 장마감 리뷰 보고서를 작성하세요.

작성 요구사항:
- 보고서 맨 위 본문 섹션은 반드시 `## 오늘 장마감 한 줄 리뷰`로 시작하세요.
- 오늘 한국장이 어떤 분위기로 마감했는지 복기하세요.
- KOSPI, KOSDAQ, 환율, 금리, 유가 데이터가 없으면 "확인 필요"로 표시하세요.
- 오늘 확인된 핵심 이슈 5개를 정리하세요.
- 반도체, 2차전지, 자동차, 금융, 플랫폼/AI 등 주요 섹터별 장마감 영향을 정리하세요.
- 관심 종목은 장마감 관점에서 긍정 / 부정 / 중립 / 확인 필요로 평가하세요.
- OpenDART 공시가 있으면 장마감 이후 또는 익일 주가에 영향을 줄 수 있는지 구분하세요.
- 마지막에는 `## 내일 체크포인트` 섹션을 작성하세요.

출력 구조:

## 오늘 장마감 한 줄 리뷰

## 한국장 마감 리뷰

## 오늘 확인된 핵심 이슈 5개

## 주요 섹터 장마감 영향

## 관심 종목 장마감 리뷰

## 리스크 요인

## 내일 체크포인트

## 출처 및 확인 필요 사항
{common_rules}
"""

    return f"""
오늘 날짜: {generated_at}
보고서 종류: {report_label}

아래는 최근 72시간 내 수집한 시장 관련 데이터입니다.
기사 전문이 아니라 제목, 요약, 출처, URL, 발행시각 중심으로 제공합니다.

작성 요구사항:
- 보고서 맨 위 본문 섹션은 반드시 `## 오늘 한 줄 요약`으로 시작하세요.
- 핵심 이슈 5개를 정리하세요.
- 미국 증시 영향, 한국 증시 영향, 환율/금리/유가 영향을 분리해서 설명하세요.
- 주목할 섹터와 리스크 요인을 정리하세요.
- 오늘 투자자가 확인해야 할 체크리스트를 작성하세요.

출력 구조:

## 오늘 한 줄 요약

## 핵심 이슈 5개

## 한국 증시 영향

## 미국 증시 영향

## 환율·금리·유가 영향

## 주목할 섹터

## 관심 종목 영향 분석

## 리스크 요인

## 오늘 체크리스트

## 출처 및 확인 필요 사항
{common_rules}
"""


def generate_report(data_packet):
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 없습니다.")

    expert_prompt = load_expert_prompt()
    user_input = build_report_user_input(data_packet)

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
    A/B/C 등급 공시만 표로 보여주고,
    기타 공시는 개수만 표시합니다.
    """
    important_disclosures = data_packet.get("opendart_disclosures", [])
    total_count = data_packet.get("opendart_disclosure_count", 0)
    important_count = data_packet.get(
        "opendart_important_disclosure_count",
        len(important_disclosures)
    )

    grade_counts = data_packet.get("opendart_grade_counts", {})
    a_count = grade_counts.get("A", 0)
    b_count = grade_counts.get("B", 0)
    c_count = grade_counts.get("C", 0)
    other_count = grade_counts.get("기타", 0)

    section = "\n\n## 한국 기업 공시 체크\n\n"

    section += "- OpenDART 공시 중요도 기준\n"
    section += "  - A급: 즉시 확인 필요\n"
    section += "  - B급: 중요하지만 해석 필요\n"
    section += "  - C급: 참고용\n\n"

    section += f"- OpenDART 전체 공시 수: {total_count}개\n"
    section += f"- 중요 공시 수: {important_count}개\n"
    section += f"- A급: {a_count}개\n"
    section += f"- B급: {b_count}개\n"
    section += f"- C급: {c_count}개\n"
    section += f"- 기타: {other_count}개\n\n"

    if not important_disclosures:
        section += "관심 기업 기준 최근 OpenDART 주요 공시 없음\n"
        return section

    section += "| 등급 | 회사 | 종목코드 | 공시명 | 접수일 | 중요 키워드 | 해석 | 원문 링크 |\n"
    section += "|---|---|---:|---|---|---|---|---|\n"

    for item in important_disclosures[:30]:
        grade = item.get("importance_grade") or "기타"
        corp_name = item.get("corp_name") or ""
        stock_code = item.get("stock_code") or ""
        report_name = item.get("report_name") or ""
        receipt_date = item.get("receipt_date") or ""
        viewer_url = item.get("viewer_url") or ""
        matched_keywords = item.get("matched_keywords") or []

        keyword_text = ", ".join(matched_keywords) if matched_keywords else "확인 필요"

        if grade == "A":
            interpretation = "즉시 확인 필요, 공시 원문 확인 필요"
        elif grade == "B":
            interpretation = "중요 공시, 세부 내용 확인 필요"
        elif grade == "C":
            interpretation = "참고용 공시, 필요 시 원문 확인"
        else:
            interpretation = "공시 원문 확인 필요"

        section += (
            f"| {grade} "
            f"| {corp_name} "
            f"| {stock_code} "
            f"| {report_name} "
            f"| {receipt_date} "
            f"| {keyword_text} "
            f"| {interpretation} "
            f"| {viewer_url} |\n"
        )

    if other_count > 0:
        section += f"\n기타 일반 공시 {other_count}개는 중요도 기준에서 표 표시를 제외했습니다.\n"

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

    report_label = get_report_label()
    subject = f"[자동] {report_label} 보고서 - {now_kst().strftime('%Y-%m-%d')}"

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
    """
    lines = report.splitlines()
    collecting = False
    result = []

    for line in lines:
        raw_line = line.strip()

        if not raw_line:
            continue

        if raw_line.startswith("## ") and section_title in raw_line:
            collecting = True
            continue

        if collecting and raw_line.startswith("## "):
            break

        if collecting:
            cleaned = raw_line.replace("**", "").strip()
            cleaned = cleaned.replace("### ", "").strip()
            cleaned = cleaned.lstrip("-").strip()
            cleaned = cleaned.lstrip("*").strip()

            if cleaned.startswith("|---") or cleaned.startswith("| ---"):
                continue

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

    if "\n" in shortened:
        shortened = shortened.rsplit("\n", 1)[0]

    shortened += "\n\n...(Slack 요약이 길어 일부 생략했습니다. 전체 보고서는 Notion에서 확인하세요.)"
    return shortened


def build_slack_summary(report, data_packet=None, notion_url=None):
    """
    Slack에 보낼 확장 요약 메시지를 만듭니다.
    REPORT_MODE에 따라 아침 보고서와 장마감 리뷰의 요약 구조를 다르게 만듭니다.
    """
    data_packet = data_packet or {}

    mode = data_packet.get("report_mode") or get_report_mode()
    report_label = data_packet.get("report_label") or get_report_label(mode)
    emoji = get_report_emoji(mode)

    market_risk = data_packet.get("market_risk", {})
    risk_score = market_risk.get("score", "확인 필요")
    risk_level = market_risk.get("level", "확인 필요")
    risk_reasons = market_risk.get("reasons", [])

    if mode == "close":
        one_line_summary = extract_section_lines(report, "오늘 장마감 한 줄 리뷰", max_lines=6)
        market_review_lines = extract_section_lines(report, "한국장 마감 리뷰", max_lines=8)
        key_issues = extract_section_lines(report, "오늘 확인된 핵심 이슈", max_lines=12)
        sector_lines = extract_section_lines(report, "주요 섹터 장마감 영향", max_lines=8)
        watchlist_lines = extract_section_lines(report, "관심 종목 장마감 리뷰", max_lines=8)
        risk_lines = extract_section_lines(report, "리스크 요인", max_lines=6)
        checklist_lines = extract_section_lines(report, "내일 체크포인트", max_lines=5)
    else:
        one_line_summary = extract_section_lines(report, "오늘 한 줄 요약", max_lines=6)
        market_review_lines = []
        key_issues = extract_section_lines(report, "핵심 이슈 5개", max_lines=12)
        sector_lines = extract_section_lines(report, "주목할 섹터", max_lines=8)
        watchlist_lines = extract_section_lines(report, "관심 종목 영향 분석", max_lines=8)
        risk_lines = extract_section_lines(report, "리스크 요인", max_lines=6)
        checklist_lines = extract_section_lines(report, "오늘 체크리스트", max_lines=5)

    dart_lines = extract_section_lines(report, "한국 기업 공시 체크", max_lines=8)

    if not one_line_summary:
        one_line_summary = ["보고서 생성 완료. 전체 내용은 Notion 또는 이메일에서 확인하세요."]

    newsapi_count = data_packet.get("newsapi_article_count", 0)
    alpha_count = data_packet.get("alpha_vantage_article_count", 0)
    opendart_count = data_packet.get("opendart_disclosure_count", 0)
    opendart_important_count = data_packet.get("opendart_important_disclosure_count", 0)
    grade_counts = data_packet.get("opendart_grade_counts", {})
    a_count = grade_counts.get("A", 0)
    b_count = grade_counts.get("B", 0)
    c_count = grade_counts.get("C", 0)
    total_article_count = data_packet.get("article_count", 0)
    watchlist_count = data_packet.get("watchlist_count", 0)

    message = f"{emoji} *오늘의 {report_label} 보고서 생성 완료*\n\n"

    risk_title = "장마감 위험도 점검" if mode == "close" else "오늘의 위험도 점수"
    message += f"*{risk_title}*\n"
    message += f"• {risk_score} / 10 · {risk_level}\n"

    for reason in risk_reasons[:3]:
        message += f"• {reason}\n"

    summary_title = "1. 장마감 한 줄 리뷰" if mode == "close" else "1. 오늘 한 줄 요약"
    message += f"\n*{summary_title}*\n"
    for line in one_line_summary:
        message += f"• {line}\n"

    if market_review_lines:
        message += "\n*2. 한국장 마감 리뷰*\n"
        for line in market_review_lines:
            message += f"• {line}\n"

    if key_issues:
        issue_no = "3" if market_review_lines else "2"
        message += f"\n*{issue_no}. 핵심 이슈*\n"
        for line in key_issues:
            message += f"• {line}\n"

    if sector_lines:
        message += "\n*주요 섹터*\n"
        for line in sector_lines:
            message += f"• {line}\n"

    if watchlist_lines:
        title = "관심 종목 장마감 리뷰" if mode == "close" else "관심 종목 영향 분석"
        message += f"\n*{title}*\n"
        for line in watchlist_lines:
            message += f"• {line}\n"

    if risk_lines:
        message += "\n*주요 리스크*\n"
        for line in risk_lines:
            message += f"• {line}\n"

    if dart_lines:
        message += "\n*한국 기업 공시 체크*\n"
        for line in dart_lines:
            message += f"• {line}\n"

    if checklist_lines:
        checklist_title = "내일 체크포인트" if mode == "close" else "오늘 체크리스트"
        message += f"\n*{checklist_title}*\n"
        for line in checklist_lines:
            message += f"• {line}\n"

    message += "\n*수집 데이터*\n"
    message += f"• NewsAPI: {newsapi_count}개\n"
    message += f"• Alpha Vantage: {alpha_count}개\n"
    message += f"• OpenDART 전체 공시: {opendart_count}개\n"
    message += f"• OpenDART 중요 공시: {opendart_important_count}개\n"
    message += f"• 공시 등급: A {a_count}개 / B {b_count}개 / C {c_count}개\n"
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
    Slack에는 전체 보고서가 아니라 요약 메시지를 보냅니다.
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
# Notion 저장
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
    text = text.replace("**", "")
    return text


def markdown_to_notion_blocks(markdown_text):
    """
    OpenAI가 만든 Markdown 보고서를 Notion 블록으로 변환합니다.
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
        chunks = split_text(text, size=1800)

        for index, chunk in enumerate(chunks):
            if index == 0:
                blocks.append(make_text_block(block_type, chunk))
            else:
                blocks.append(make_text_block("paragraph", chunk))

    return blocks


def append_blocks_to_notion_page(page_id, blocks, headers):
    """
    블록이 많을 경우 Notion 페이지에 나눠서 추가합니다.
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


def send_notion(report, data_packet=None):
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

    data_packet = data_packet or {}
    mode = data_packet.get("report_mode") or get_report_mode()
    report_label = data_packet.get("report_label") or get_report_label(mode)
    title = get_report_title(created_at=created_at, mode=mode)

    market_risk = data_packet.get("market_risk", {})
    risk_score = market_risk.get("score", "확인 필요")
    risk_level = market_risk.get("level", "확인 필요")
    risk_reasons = market_risk.get("reasons", [])

    newsapi_count = data_packet.get("newsapi_article_count", 0)
    alpha_count = data_packet.get("alpha_vantage_article_count", 0)
    total_article_count = data_packet.get("article_count", 0)

    opendart_total_count = data_packet.get("opendart_disclosure_count", 0)
    opendart_important_count = data_packet.get("opendart_important_disclosure_count", 0)

    watchlist_count = data_packet.get("watchlist_count", 0)

    grade_counts = data_packet.get("opendart_grade_counts", {})
    dart_a_count = grade_counts.get("A", 0)
    dart_b_count = grade_counts.get("B", 0)
    dart_c_count = grade_counts.get("C", 0)
    dart_other_count = grade_counts.get("기타", 0)

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    risk_heading = "장마감 위험도 점검" if mode == "close" else "오늘의 위험도 점수"

    risk_blocks = [
        make_text_block("heading_2", risk_heading),
        make_text_block("paragraph", f"{risk_score} / 10 · {risk_level}"),
    ]

    for reason in risk_reasons[:5]:
        risk_blocks.append(
            make_text_block("bulleted_list_item", reason)
        )

    risk_blocks.append(make_divider_block())

    intro_blocks = [
        make_text_block("heading_1", title),
        make_text_block("paragraph", f"생성 시각: {created_at.strftime('%Y-%m-%d %H:%M:%S KST')}"),
        make_text_block("paragraph", f"자동 생성된 {report_label} 보고서입니다."),
        make_text_block("paragraph", "주의: 이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다."),
        make_divider_block(),
    ] + risk_blocks + [
        make_text_block("heading_2", "수집 데이터 요약"),
        make_text_block("bulleted_list_item", f"NewsAPI 기사: {newsapi_count}개"),
        make_text_block("bulleted_list_item", f"Alpha Vantage 기사: {alpha_count}개"),
        make_text_block("bulleted_list_item", f"최종 뉴스 기사: {total_article_count}개"),
        make_text_block("bulleted_list_item", f"OpenDART 전체 공시: {opendart_total_count}개"),
        make_text_block("bulleted_list_item", f"OpenDART 중요 공시: {opendart_important_count}개"),
        make_text_block("bulleted_list_item", f"OpenDART 등급: A {dart_a_count}개 / B {dart_b_count}개 / C {dart_c_count}개 / 기타 {dart_other_count}개"),
        make_text_block("bulleted_list_item", f"관심 종목: {watchlist_count}개"),
        make_divider_block(),
    ]

    report_blocks = markdown_to_notion_blocks(report)
    all_blocks = intro_blocks + report_blocks

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
    mode = get_report_mode()
    report_label = get_report_label(mode)
    print(f"Daily Market Report 시작: {report_label}")

    data_packet = build_data_packet()

    if data_packet["article_count"] == 0:
        print("수집된 뉴스가 없습니다. 그래도 보고서를 생성합니다.")

    report = generate_report(data_packet)

    if not report:
        raise ValueError("OpenAI 보고서 생성 결과가 비어 있습니다.")

    report = build_risk_section(data_packet) + report
    report = report + build_opendart_section(data_packet)

    send_email(report)

    notion_url = send_notion(report, data_packet=data_packet)

    send_slack(
        report=report,
        data_packet=data_packet,
        notion_url=notion_url,
    )

    print(f"Daily Market Report 완료: {report_label}")


if __name__ == "__main__":
    main()
