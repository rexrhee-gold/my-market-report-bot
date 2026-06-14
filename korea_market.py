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

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 시간 함수
# =========================

def now_kst():
    return datetime.now(KST)


def since_utc(hours=24):
    return datetime.now(UTC) - timedelta(hours=hours)


def alpha_time_from(hours=72):
    target_time = datetime.now(UTC) - timedelta(hours=hours)
    return target_time.strftime("%Y%m%dT%H%M")


def yyyymmdd_kst(days_ago=0):
    target_date = now_kst() - timedelta(days=days_ago)
    return target_date.strftime("%Y%m%d")


def korean_time_label():
    now = now_kst()
    hour = now.hour
    minute = now.minute

    if hour < 12:
        return f"오전 {hour}:{minute:02d}"
    if hour == 12:
        return f"오후 12:{minute:02d}"
    return f"오후 {hour - 12}:{minute:02d}"


# =========================
# 리포트 모드
# =========================

REPORT_CONFIG = {
    "0740": {
        "name": "프리마켓 대응 리포트",
        "prompt_path": "prompts/korea_0740.md",
        "subject_prefix": "[자동] 프리마켓 대응 리포트",
        "slack_title": "🇰🇷 프리마켓 대응 리포트",
    },
    "0840": {
        "name": "장전 최종 체크",
        "prompt_path": "prompts/korea_0840.md",
        "subject_prefix": "[자동] 장전 최종 체크",
        "slack_title": "✅ 장전 최종 체크",
    },
    "intraday": {
        "name": "장중 시장 업데이트",
        "prompt_path": "prompts/korea_intraday.md",
        "subject_prefix": "[자동] 장중 시장 업데이트",
        "slack_title": "📊 장중 시장 업데이트",
    },
}


def normalize_report_mode(raw_mode):
    mode = (raw_mode or "0740").strip().lower()

    aliases = {
        "korea_0740": "0740",
        "premarket": "0740",
        "pre_market": "0740",
        "morning_0740": "0740",
        "korea_0840": "0840",
        "preopen": "0840",
        "pre_open": "0840",
        "open_check": "0840",
        "intraday_1030": "intraday",
        "intraday_1330": "intraday",
        "korea_intraday": "intraday",
        "midday": "intraday",
    }

    return aliases.get(mode, mode)


def get_report_config():
    mode = normalize_report_mode(os.getenv("REPORT_MODE", "0740"))

    if mode not in REPORT_CONFIG:
        raise ValueError(
            f"지원하지 않는 REPORT_MODE입니다: {mode}. "
            "가능한 값: 0740, 0840, intraday"
        )

    return mode, REPORT_CONFIG[mode]


# =========================
# 관심종목 함수
# =========================

def load_watchlist():
    path = "watchlist.txt"

    if not os.path.exists(path):
        print("watchlist.txt 파일이 없습니다. 관심 종목 분석은 건너뜁니다.")
        return []

    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    watchlist = []

    for line in lines:
        item = line.strip()

        if not item or item.startswith("#"):
            continue

        watchlist.append(item)

    print(f"관심 종목 {len(watchlist)}개 로드 완료")
    return watchlist


def load_dart_watchlist():
    path = "dart_watchlist.txt"

    if not os.path.exists(path):
        print("dart_watchlist.txt 파일이 없습니다. OpenDART 관심 공시 수집은 건너뜁니다.")
        return []

    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    watchlist = []

    for line in lines:
        item = line.strip()

        if not item or item.startswith("#"):
            continue

        watchlist.append(item)

    print(f"OpenDART 관심 기업 {len(watchlist)}개 로드 완료")
    return watchlist


# =========================
# 뉴스 수집
# =========================

def fetch_newsapi(report_mode):
    if not NEWSAPI_KEY:
        print("NEWSAPI_KEY가 없습니다. 뉴스 수집을 건너뜁니다.")
        return []

    if report_mode == "0740":
        query = (
            "Korea stock market OR KOSPI OR KOSDAQ OR Samsung Electronics OR SK Hynix OR "
            "Nvidia OR semiconductor OR AI OR Federal Reserve OR US Treasury yield OR "
            "dollar index OR oil price OR Wall Street OR Nasdaq OR S&P 500"
        )
    elif report_mode == "0840":
        query = (
            "KOSPI futures OR Korea premarket OR Korea stock market OR Samsung Electronics OR "
            "SK Hynix OR semiconductor OR battery OR won dollar OR Asia markets OR US futures"
        )
    else:
        query = (
            "KOSPI OR KOSDAQ OR Korea stock market OR Samsung Electronics OR SK Hynix OR "
            "semiconductor OR battery OR biotechnology OR defense stocks OR shipbuilding OR "
            "foreign investors Korea stocks"
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

    print("NewsAPI 데이터 수집 시작")
    print("NewsAPI query:", query)

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()

    print("NewsAPI status:", data.get("status"))
    print("NewsAPI totalResults:", data.get("totalResults"))
    print("NewsAPI message:", data.get("message"))

    result = []

    for article in data.get("articles", []):
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

    print(f"NewsAPI 기사 {len(result)}개 수집 완료")
    return result


def fetch_alpha_vantage_news(report_mode):
    if not ALPHAVANTAGE_KEY:
        print("ALPHAVANTAGE_KEY가 없습니다. Alpha Vantage 뉴스 수집을 건너뜁니다.")
        return []

    url = "https://www.alphavantage.co/query"

    if report_mode == "intraday":
        topics = "financial_markets,economy_macro,technology,earnings"
        hours = 24
    else:
        topics = "financial_markets,economy_monetary,economy_macro,technology"
        hours = 72

    params = {
        "function": "NEWS_SENTIMENT",
        "topics": topics,
        "time_from": alpha_time_from(hours),
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

    for key in ["Information", "Note", "Error Message"]:
        if key in data:
            print(f"Alpha Vantage {key}:", data.get(key))
            return []

    result = []

    for item in data.get("feed", []):
        title = item.get("title")
        article_url = item.get("url")

        if not title or not article_url:
            continue

        related_tickers = []

        for ticker_item in item.get("ticker_sentiment", [])[:10]:
            ticker = ticker_item.get("ticker")

            if ticker:
                related_tickers.append(
                    {
                        "ticker": ticker,
                        "relevance_score": ticker_item.get("relevance_score"),
                        "sentiment_label": ticker_item.get("ticker_sentiment_label"),
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
# OpenDART
# =========================

DART_GRADE_KEYWORDS = {
    "A": [
        "상장폐지", "거래정지", "관리종목", "불성실공시", "감사의견",
        "의견거절", "부적정", "한정", "횡령", "배임", "회생절차",
        "파산", "부도", "소송", "유상증자", "전환사채", "신주인수권",
        "교환사채", "최대주주", "경영권",
    ],
    "B": [
        "영업실적", "잠정실적", "매출액", "영업이익", "실적", "배당",
        "현금배당", "자기주식", "자사주", "공급계약", "단일판매",
        "수주", "계약체결", "대규모", "합병", "분할", "영업양수",
        "영업양도", "타법인", "취득", "처분", "출자",
    ],
    "C": [
        "대표이사", "임원", "주주총회", "이사회", "정정",
        "분기보고서", "반기보고서", "사업보고서", "사외이사",
        "감사보고서",
    ],
}

DART_GRADE_SCORE = {"A": 100, "B": 50, "C": 10, "기타": 0}


def score_dart_disclosure(disclosure):
    report_name = disclosure.get("report_name") or ""

    matched_keywords = []
    grade = "기타"

    for current_grade in ["A", "B", "C"]:
        for keyword in DART_GRADE_KEYWORDS[current_grade]:
            if keyword.lower() in report_name.lower():
                matched_keywords.append(keyword)

        if matched_keywords:
            grade = current_grade
            break

    disclosure["importance_grade"] = grade
    disclosure["importance_score"] = DART_GRADE_SCORE.get(grade, 0)
    disclosure["matched_keywords"] = matched_keywords

    return disclosure


def count_dart_grades(disclosures):
    counts = {"A": 0, "B": 0, "C": 0, "기타": 0}

    for disclosure in disclosures:
        grade = disclosure.get("importance_grade", "기타")
        counts[grade] = counts.get(grade, 0) + 1

    return counts


def filter_important_dart_disclosures(disclosures, max_items=30):
    scored = [score_dart_disclosure(item) for item in disclosures]

    important = [
        item for item in scored
        if item.get("importance_grade") in ["A", "B", "C"]
    ]

    important.sort(
        key=lambda x: (
            x.get("importance_score", 0),
            x.get("receipt_date") or "",
        ),
        reverse=True,
    )

    return important[:max_items]


def fetch_dart_corp_codes():
    if not OPENDART_API_KEY:
        print("OPENDART_API_KEY가 없습니다. 회사 고유번호 수집을 건너뜁니다.")
        return []

    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    params = {"crtfc_key": OPENDART_API_KEY}

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

    bgn_de = yyyymmdd_kst(days_ago=3)
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


def build_opendart_section(data_packet):
    important_disclosures = data_packet.get("opendart_disclosures", [])
    total_count = data_packet.get("opendart_disclosure_count", 0)
    important_count = data_packet.get("opendart_important_disclosure_count", len(important_disclosures))

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
            f"| {grade} | {corp_name} | {stock_code} | {report_name} | "
            f"{receipt_date} | {keyword_text} | {interpretation} | {viewer_url} |\n"
        )

    if other_count > 0:
        section += f"\n기타 일반 공시 {other_count}개는 중요도 기준에서 표 표시를 제외했습니다.\n"

    return section


# =========================
# 데이터 패킷
# =========================

def build_market_data_status(report_mode):
    if report_mode == "0740":
        return {
            "sp500": "확인 필요",
            "nasdaq": "확인 필요",
            "dow": "확인 필요",
            "russell2000": "확인 필요",
            "semiconductor_index": "확인 필요",
            "us_10y_yield": "확인 필요",
            "dollar_index": "확인 필요",
            "usd_krw_or_ndf": "확인 필요",
            "wti": "확인 필요",
            "brent": "확인 필요",
            "gold": "확인 필요",
            "big_tech": "확인 필요",
            "us_sector_flow": "확인 필요",
        }

    if report_mode == "0840":
        return {
            "kospi200_futures": "확인 필요",
            "us_futures": "확인 필요",
            "usd_krw_or_ndf": "확인 필요",
            "dollar_index": "확인 필요",
            "us_10y_yield": "확인 필요",
            "wti_brent": "확인 필요",
            "asia_market_preopen": "확인 필요",
            "domestic_premarket_news": "뉴스/공시 데이터 기반으로 확인",
        }

    return {
        "kospi_current": "확인 필요",
        "kosdaq_current": "확인 필요",
        "kospi200_futures": "확인 필요",
        "foreign_spot_flow": "확인 필요",
        "foreign_futures_flow": "확인 필요",
        "institution_flow": "확인 필요",
        "retail_flow": "확인 필요",
        "usd_krw": "확인 필요",
        "samsung_electronics": "확인 필요",
        "sk_hynix": "확인 필요",
        "leading_sectors_by_volume": "확인 필요",
    }


def build_data_packet(report_mode, report_config):
    watchlist = load_watchlist()

    newsapi_articles = fetch_newsapi(report_mode)
    alpha_articles = fetch_alpha_vantage_news(report_mode)

    articles = dedupe_articles(newsapi_articles + alpha_articles)

    dart_disclosures = fetch_opendart_disclosures()
    scored_dart_disclosures = [score_dart_disclosure(item) for item in dart_disclosures]
    important_dart_disclosures = filter_important_dart_disclosures(scored_dart_disclosures, max_items=30)
    dart_grade_counts = count_dart_grades(scored_dart_disclosures)

    data_packet = {
        "system_name": "한국증시 장전·장중 자동화 시스템",
        "report_mode": report_mode,
        "report_name": report_config["name"],
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "generated_time_label_kst": korean_time_label(),
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
        "market_data_status": build_market_data_status(report_mode),
        "important_note": (
            "KOSPI/KOSDAQ 실시간 지수, 외국인·기관·개인 수급, 거래대금 상위 업종, "
            "KOSPI200 선물 등은 아직 자동 수집되지 않습니다. "
            "명확하지 않은 항목은 반드시 '확인 필요'로 표시하세요."
        ),
    }

    print(f"보고서 모드: {report_mode}")
    print(f"보고서 이름: {report_config['name']}")
    print(f"NewsAPI 기사 수: {len(newsapi_articles)}개")
    print(f"Alpha Vantage 기사 수: {len(alpha_articles)}개")
    print(f"OpenDART 전체 공시 수: {len(dart_disclosures)}개")
    print(f"OpenDART 중요 공시 수: {len(important_dart_disclosures)}개")
    print(f"OpenDART 등급별 공시 수: {dart_grade_counts}")
    print(f"최종 기사 수: {len(articles)}개")
    print(f"관심 종목 수: {len(watchlist)}개")

    return data_packet


# =========================
# OpenAI 보고서 생성
# =========================

def load_prompt(prompt_path):
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            f"{prompt_path} 파일이 없습니다. "
            "GitHub 저장소의 prompts 폴더에 프롬프트 파일을 추가해 주세요."
        )

    with open(prompt_path, "r", encoding="utf-8") as file:
        return file.read()


def generate_report(data_packet, report_config):
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 없습니다.")

    prompt = load_prompt(report_config["prompt_path"])

    user_input = f"""
오늘 날짜: {now_kst().strftime('%Y-%m-%d %H:%M KST')}
작성 기준 시각: {korean_time_label()} KST
보고서 모드: {data_packet.get("report_mode")}
보고서 이름: {data_packet.get("report_name")}

아래 DATA는 현재 자동 수집 가능한 뉴스, 감성 데이터, OpenDART 공시, 관심 종목, 데이터 상태입니다.

중요:
- DATA에 없는 실시간 시장 수치나 수급 수치는 임의로 만들지 마세요.
- 명확하지 않은 항목은 반드시 "확인 필요"라고 표시하세요.
- 한국 투자자가 실제 장전/장중 대응에 참고할 수 있도록 짧고 실전적으로 작성하세요.
- 투자 권유가 아니라 참고용 분석 자료임을 표시하세요.
- OpenDART 공시 제목만으로 호재/악재를 단정하지 말고 "공시 원문 확인 필요"라고 표시하세요.
- "## 한국 기업 공시 체크" 섹션은 Python 코드가 보고서 마지막에 자동으로 추가하므로 본문에서 중복 작성하지 마세요.

DATA:
{json.dumps(data_packet, ensure_ascii=False, indent=2)}
"""

    print("OpenAI 보고서 생성 시작")

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=prompt,
        input=user_input,
        max_output_tokens=5000,
    )

    report = response.output_text

    if not report or not report.strip():
        raise ValueError("OpenAI 응답이 비어 있습니다.")

    print("OpenAI 보고서 생성 완료")
    return report


# =========================
# 이메일 발송
# =========================

def send_email(report, report_config):
    if not EMAIL_TO:
        print("EMAIL_TO가 없어서 이메일 발송을 건너뜁니다.")
        return

    if not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP_USER 또는 SMTP_PASSWORD가 없어서 이메일 발송을 건너뜁니다.")
        return

    subject = f"{report_config['subject_prefix']} - {now_kst().strftime('%Y-%m-%d %H:%M KST')}"

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

def extract_first_lines(report, max_lines=16):
    lines = []

    for line in report.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue

        if cleaned.startswith("|---") or cleaned.startswith("| ---"):
            continue

        lines.append(cleaned)

        if len(lines) >= max_lines:
            break

    return lines


def truncate_slack_message(message, max_chars=9000):
    if len(message) <= max_chars:
        return message

    shortened = message[:max_chars]

    if "\n" in shortened:
        shortened = shortened.rsplit("\n", 1)[0]

    shortened += "\n\n...(Slack 요약이 길어 일부 생략했습니다. 전체 보고서는 Notion에서 확인하세요.)"
    return shortened


def build_slack_summary(report, data_packet, report_config, notion_url=None):
    lines = extract_first_lines(report, max_lines=18)

    newsapi_count = data_packet.get("newsapi_article_count", 0)
    alpha_count = data_packet.get("alpha_vantage_article_count", 0)
    opendart_count = data_packet.get("opendart_disclosure_count", 0)
    opendart_important_count = data_packet.get("opendart_important_disclosure_count", 0)
    watchlist_count = data_packet.get("watchlist_count", 0)

    grade_counts = data_packet.get("opendart_grade_counts", {})
    a_count = grade_counts.get("A", 0)
    b_count = grade_counts.get("B", 0)
    c_count = grade_counts.get("C", 0)

    message = f"{report_config['slack_title']} 생성 완료\n\n"

    message += "*요약 미리보기*\n"
    for line in lines:
        cleaned = line.replace("#", "").replace("**", "").strip()
        if cleaned:
            message += f"• {cleaned}\n"

    message += "\n*수집 데이터*\n"
    message += f"• NewsAPI: {newsapi_count}개\n"
    message += f"• Alpha Vantage: {alpha_count}개\n"
    message += f"• OpenDART 전체 공시: {opendart_count}개\n"
    message += f"• OpenDART 중요 공시: {opendart_important_count}개\n"
    message += f"• 공시 등급: A {a_count}개 / B {b_count}개 / C {c_count}개\n"
    message += f"• 관심 종목: {watchlist_count}개\n"

    message += "\n*전체 보고서*\n"

    if notion_url:
        message += f"• Notion에서 보기: {notion_url}\n"
    else:
        message += "• Notion 링크 없음. 이메일 또는 Notion 페이지를 직접 확인하세요.\n"

    message += "\n_이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다._"

    return truncate_slack_message(message, max_chars=9000)


def send_slack(report, data_packet, report_config, notion_url=None):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 없어서 Slack 발송을 건너뜁니다.")
        return

    slack_text = build_slack_summary(
        report=report,
        data_packet=data_packet,
        report_config=report_config,
        notion_url=notion_url,
    )

    print("Slack 메시지 발송 시작")

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": slack_text},
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Slack 발송 실패: {response.status_code} {response.text}")

    print("Slack 메시지 발송 완료")


# =========================
# Notion 저장
# =========================

def split_text(text, size=1800):
    return [text[i:i + size] for i in range(0, len(text), size)]


def rich_text(text):
    return [
        {
            "type": "text",
            "text": {
                "content": text
            }
        }
    ]


def make_text_block(block_type, text):
    return {
        "object": "block",
        "type": block_type,
        block_type: {
            "rich_text": rich_text(text)
        }
    }


def make_divider_block():
    return {
        "object": "block",
        "type": "divider",
        "divider": {}
    }


def clean_markdown_text(text):
    return text.strip().replace("**", "")


def markdown_to_notion_blocks(markdown_text):
    blocks = []

    for line in markdown_text.splitlines():
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

        for index, chunk in enumerate(split_text(text, size=1800)):
            if index == 0:
                blocks.append(make_text_block(block_type, chunk))
            else:
                blocks.append(make_text_block("paragraph", chunk))

    return blocks


def append_blocks_to_notion_page(page_id, blocks, headers):
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


def send_notion(report, data_packet, report_config):
    if not NOTION_TOKEN:
        print("NOTION_TOKEN이 없어서 Notion 저장을 건너뜁니다.")
        return None

    if not NOTION_PARENT_PAGE_ID:
        print("NOTION_PARENT_PAGE_ID가 없어서 Notion 저장을 건너뜁니다.")
        return None

    created_at = now_kst()
    title = f"{report_config['name']} - {created_at.strftime('%Y-%m-%d %H:%M KST')}"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    grade_counts = data_packet.get("opendart_grade_counts", {})

    intro_blocks = [
        make_text_block("heading_1", title),
        make_text_block("paragraph", f"생성 시각: {created_at.strftime('%Y-%m-%d %H:%M:%S KST')}"),
        make_text_block("paragraph", "한국증시 장전·장중 자동화 시스템에서 생성한 보고서입니다."),
        make_text_block("paragraph", "주의: 이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다."),
        make_divider_block(),
        make_text_block("heading_2", "수집 데이터 요약"),
        make_text_block("bulleted_list_item", f"NewsAPI 기사: {data_packet.get('newsapi_article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"Alpha Vantage 기사: {data_packet.get('alpha_vantage_article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"최종 뉴스 기사: {data_packet.get('article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"OpenDART 전체 공시: {data_packet.get('opendart_disclosure_count', 0)}개"),
        make_text_block("bulleted_list_item", f"OpenDART 중요 공시: {data_packet.get('opendart_important_disclosure_count', 0)}개"),
        make_text_block(
            "bulleted_list_item",
            f"OpenDART 등급: A {grade_counts.get('A', 0)}개 / B {grade_counts.get('B', 0)}개 / C {grade_counts.get('C', 0)}개 / 기타 {grade_counts.get('기타', 0)}개"
        ),
        make_text_block("bulleted_list_item", f"관심 종목: {data_packet.get('watchlist_count', 0)}개"),
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
    print("한국증시 장전·장중 자동화 시스템 시작")

    report_mode, report_config = get_report_config()

    data_packet = build_data_packet(report_mode, report_config)

    if data_packet["article_count"] == 0:
        print("수집된 뉴스가 없습니다. 그래도 보고서를 생성합니다.")

    report = generate_report(data_packet, report_config)

    if not report:
        raise ValueError("OpenAI 보고서 생성 결과가 비어 있습니다.")

    report = report + build_opendart_section(data_packet)

    send_email(report, report_config)

    notion_url = send_notion(report, data_packet, report_config)

    send_slack(
        report=report,
        data_packet=data_packet,
        report_config=report_config,
        notion_url=notion_url,
    )

    print("한국증시 장전·장중 자동화 시스템 완료")


if __name__ == "__main__":
    main()
