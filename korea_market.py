import os
import re
import io
import json
import zipfile
import smtplib
import hashlib
import requests
try:
    import yfinance as yf
except Exception:
    yf = None

try:
    from pykrx import stock as krx_stock
except Exception:
    krx_stock = None

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from contextlib import redirect_stdout, redirect_stderr

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
        "name": "한국증시 프리마켓 대응 리포트",
        "prompt_path": "prompts/korea_0740.md",
        "subject_prefix": "[자동] 한국증시 프리마켓 대응 리포트",
        "slack_title": "🇰🇷 한국증시 프리마켓 대응 리포트",
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

def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def fast_info_get(fast_info, key):
    try:
        return fast_info.get(key)
    except Exception:
        try:
            return getattr(fast_info, key)
        except Exception:
            return None


def calculate_change(last_price, previous_close):
    last_price = safe_float(last_price)
    previous_close = safe_float(previous_close)

    if last_price is None or previous_close is None or previous_close == 0:
        return None, None

    change = last_price - previous_close
    change_pct = (change / previous_close) * 100

    return change, change_pct


def format_number(value, decimals=2):
    value = safe_float(value)

    if value is None:
        return "확인 필요"

    return f"{value:,.{decimals}f}"


def quote_to_text(quote):
    if not quote or quote.get("status") != "ok":
        return "확인 필요"

    last_price = quote.get("last_price")
    change = quote.get("change")
    change_pct = quote.get("change_pct")

    if change is None or change_pct is None:
        return f"{format_number(last_price)}"

    return f"{format_number(last_price)} ({change:+.2f}, {change_pct:+.2f}%)"


def fetch_yfinance_quote(symbol, name):
    """
    Yahoo Finance/yfinance에서 지수, 환율, 종목, 원자재 데이터를 가져옵니다.

    주의:
    - 무료 공개 데이터라 지연 시세일 수 있습니다.
    - 일부 한국시장 데이터나 선물 데이터는 조회가 안 될 수 있습니다.
    - 조회 실패 시 보고서에서는 '확인 필요'로 표시합니다.
    """
    empty_result = {
        "symbol": symbol,
        "name": name,
        "status": "확인 필요",
        "last_price": None,
        "previous_close": None,
        "change": None,
        "change_pct": None,
        "currency": None,
    }

    if yf is None:
        empty_result["error"] = "yfinance 패키지가 설치되어 있지 않습니다."
        return empty_result

    try:
        ticker = yf.Ticker(symbol)

        last_price = None
        previous_close = None
        currency = None

        try:
            fast_info = ticker.fast_info
            last_price = safe_float(
                fast_info_get(fast_info, "last_price")
                or fast_info_get(fast_info, "lastPrice")
            )
            previous_close = safe_float(
                fast_info_get(fast_info, "previous_close")
                or fast_info_get(fast_info, "previousClose")
            )
            currency = fast_info_get(fast_info, "currency")
        except Exception:
            pass

        # 장중 데이터가 있으면 마지막 체결/지수값을 우선 사용
        try:
            intraday = ticker.history(period="1d", interval="5m")
            if intraday is not None and not intraday.empty:
                latest_close = safe_float(intraday["Close"].dropna().iloc[-1])
                if latest_close is not None:
                    last_price = latest_close
        except Exception:
            pass

        # 전일 종가 또는 현재가가 없으면 일봉 데이터로 보완
        try:
            daily = ticker.history(period="7d", interval="1d")
            if daily is not None and not daily.empty:
                closes = daily["Close"].dropna()

                if last_price is None and len(closes) >= 1:
                    last_price = safe_float(closes.iloc[-1])

                if previous_close is None and len(closes) >= 2:
                    previous_close = safe_float(closes.iloc[-2])
        except Exception:
            pass

        change, change_pct = calculate_change(last_price, previous_close)

        if last_price is None:
            empty_result["error"] = "시세 데이터 없음"
            return empty_result

        return {
            "symbol": symbol,
            "name": name,
            "status": "ok",
            "last_price": last_price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "currency": currency,
        }

    except Exception as error:
        empty_result["error"] = str(error)
        return empty_result


def first_ok_quote(candidates):
    """
    같은 데이터에 대해 여러 Yahoo Finance 심볼 후보를 시도합니다.
    예: 원/달러 환율은 KRW=X가 보통 사용되지만 환경에 따라 실패할 수 있습니다.
    """
    for symbol, name in candidates:
        quote = fetch_yfinance_quote(symbol, name)

        if quote.get("status") == "ok":
            return quote

    return {
        "symbol": candidates[0][0],
        "name": candidates[0][1],
        "status": "확인 필요",
        "last_price": None,
        "previous_close": None,
        "change": None,
        "change_pct": None,
        "currency": None,
    }



# =========================
# KRX / pykrx 한국시장 데이터
# =========================


def safe_pykrx_call(func, *args, **kwargs):
    """
    pykrx 내부에서 출력하는 반복 에러 로그를 숨기고,
    실패 시 None을 반환합니다.
    GitHub Actions 로그가 불필요하게 길어지는 것을 방지합니다.
    """
    try:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            return func(*args, **kwargs)
    except Exception as error:
        return None


def is_valid_ohlcv_dataframe(df):
    """
    pykrx OHLCV DataFrame이 정상 구조인지 확인합니다.
    pykrx/KRX 응답 구조가 바뀌거나 빈 응답이면 False를 반환합니다.
    """
    if df is None:
        return False

    try:
        if df.empty:
            return False

        required_any_columns = ["종가", "거래량", "거래대금", "등락률"]

        for col in required_any_columns:
            if col in df.columns:
                return True

        return False

    except Exception:
        return False


def normalize_number(value):
    """
    pandas/numpy 숫자 타입을 JSON 저장 가능한 기본 Python 숫자로 변환합니다.
    """
    try:
        if value is None:
            return None

        if hasattr(value, "item"):
            value = value.item()

        if isinstance(value, float):
            if value != value:
                return None

        return value
    except Exception:
        return None


def format_krw_amount(value):
    """
    원 단위 금액을 사람이 읽기 쉬운 형태로 변환합니다.
    """
    value = safe_float(value)

    if value is None:
        return "확인 필요"

    abs_value = abs(value)

    if abs_value >= 100_000_000_000:
        return f"{value / 100_000_000_000:.2f}조원"

    if abs_value >= 100_000_000:
        return f"{value / 100_000_000:.1f}억원"

    return f"{value:,.0f}원"


def dataframe_to_records(df, max_rows=20):
    """
    pykrx DataFrame을 JSON에 넣기 쉬운 records 형태로 변환합니다.
    """
    if df is None or df.empty:
        return []

    records = []

    for idx, row in df.head(max_rows).iterrows():
        item = {"index": str(idx)}

        for col in df.columns:
            value = normalize_number(row[col])
            item[str(col)] = value

        records.append(item)

    return records


def get_latest_krx_trading_date(max_lookback_days=10):
    """
    pykrx에서 조회 가능한 가장 최근 거래일을 찾습니다.

    개선점:
    - pykrx 내부 반복 에러 로그를 숨깁니다.
    - get_nearest_business_day_in_a_week를 우선 사용합니다.
    - 실패 시 최근 날짜를 몇 개만 조용히 확인합니다.
    """
    if krx_stock is None:
        return None

    today = yyyymmdd_kst(days_ago=0)

    # 1차: pykrx의 최근 영업일 함수 사용
    try:
        nearest_day = safe_pykrx_call(
            krx_stock.get_nearest_business_day_in_a_week,
            today
        )

        if nearest_day:
            return str(nearest_day).replace("-", "")

    except Exception:
        pass

    # 2차: 최근 날짜를 조용히 확인
    for days_ago in range(0, min(max_lookback_days, 5) + 1):
        date_text = yyyymmdd_kst(days_ago=days_ago)
        df = safe_pykrx_call(
            krx_stock.get_market_ohlcv_by_ticker,
            date_text,
            market="KOSPI"
        )

        if is_valid_ohlcv_dataframe(df):
            return date_text

    return None


def get_krx_ticker_name(ticker):
    if krx_stock is None:
        return ticker

    name = safe_pykrx_call(krx_stock.get_market_ticker_name, ticker)

    if name:
        return name

    return ticker


def fetch_krx_market_ohlcv_by_ticker(date_text, market):
    """
    특정 거래일의 KOSPI/KOSDAQ 종목별 OHLCV를 가져옵니다.

    실패 시 예외를 던지지 않고 None을 반환합니다.
    """
    if krx_stock is None:
        return None

    df = safe_pykrx_call(
        krx_stock.get_market_ohlcv_by_ticker,
        date_text,
        market=market
    )

    if not is_valid_ohlcv_dataframe(df):
        print(f"KRX 종목별 OHLCV 사용 불가: {market} {date_text}")
        return None

    return df


def fetch_krx_top_trading_value(date_text, market, limit=10):
    """
    거래대금 상위 종목을 가져옵니다.
    """
    df = fetch_krx_market_ohlcv_by_ticker(date_text, market)

    if df is None or df.empty or "거래대금" not in df.columns:
        return []

    sorted_df = df.sort_values("거래대금", ascending=False).head(limit)

    result = []

    for ticker, row in sorted_df.iterrows():
        result.append(
            {
                "ticker": str(ticker),
                "name": get_krx_ticker_name(str(ticker)),
                "close": normalize_number(row.get("종가")),
                "change": normalize_number(row.get("대비")),
                "change_pct": normalize_number(row.get("등락률")),
                "volume": normalize_number(row.get("거래량")),
                "trading_value": normalize_number(row.get("거래대금")),
                "trading_value_text": format_krw_amount(row.get("거래대금")),
            }
        )

    return result


def fetch_krx_top_gainers(date_text, market, limit=10):
    """
    등락률 상위 종목을 가져옵니다.
    """
    df = fetch_krx_market_ohlcv_by_ticker(date_text, market)

    if df is None or df.empty or "등락률" not in df.columns:
        return []

    # 거래대금이 너무 작은 종목을 줄이기 위해 거래대금 0 또는 결측은 제외
    if "거래대금" in df.columns:
        df = df[df["거래대금"] > 0]

    sorted_df = df.sort_values("등락률", ascending=False).head(limit)

    result = []

    for ticker, row in sorted_df.iterrows():
        result.append(
            {
                "ticker": str(ticker),
                "name": get_krx_ticker_name(str(ticker)),
                "close": normalize_number(row.get("종가")),
                "change": normalize_number(row.get("대비")),
                "change_pct": normalize_number(row.get("등락률")),
                "volume": normalize_number(row.get("거래량")),
                "trading_value": normalize_number(row.get("거래대금")),
                "trading_value_text": format_krw_amount(row.get("거래대금")),
            }
        )

    return result


def fetch_krx_investor_flow(date_text, market):
    """
    투자자별 거래대금/순매수 데이터를 가져옵니다.
    market 예: KOSPI, KOSDAQ

    실패 시 예외를 던지지 않고 확인 필요로 반환합니다.
    """
    if krx_stock is None:
        return {
            "status": "확인 필요",
            "reason": "pykrx 패키지가 설치되어 있지 않습니다.",
            "rows": [],
        }

    df = safe_pykrx_call(
        krx_stock.get_market_trading_value_by_investor,
        date_text,
        date_text,
        market
    )

    try:
        if df is None or df.empty:
            return {
                "status": "확인 필요",
                "reason": "투자자별 수급 데이터가 비어 있거나 조회 실패했습니다.",
                "rows": [],
            }

        return {
            "status": "ok",
            "rows": dataframe_to_records(df, max_rows=30),
        }

    except Exception as error:
        return {
            "status": "확인 필요",
            "reason": str(error),
            "rows": [],
        }


def find_investor_row(rows, keywords):
    """
    pykrx 투자자별 수급 결과에서 개인/외국인/기관 행을 찾습니다.
    """
    for row in rows:
        label = row.get("index", "")

        for keyword in keywords:
            if keyword in label:
                return row

    return None


def extract_net_buy_value(row):
    """
    투자자별 수급 row에서 순매수 금액 컬럼을 찾습니다.
    pykrx 버전에 따라 컬럼명이 다를 수 있어 '순매수'가 포함된 첫 컬럼을 사용합니다.
    """
    if not row:
        return None

    preferred_columns = ["순매수", "순매수거래대금"]

    for col in preferred_columns:
        if col in row:
            return row.get(col)

    for col, value in row.items():
        if "순매수" in col:
            return value

    return None


def build_single_investor_flow_text(krx_data, investor_name, keywords):
    """
    KOSPI/KOSDAQ 투자자별 순매수 요약 문장을 만듭니다.
    """
    investor_flows = krx_data.get("investor_flows", {})
    parts = []

    for market in ["KOSPI", "KOSDAQ"]:
        market_data = investor_flows.get(market, {})
        rows = market_data.get("rows", [])

        row = find_investor_row(rows, keywords)
        net_value = extract_net_buy_value(row)

        if net_value is None:
            parts.append(f"{market} 확인 필요")
        else:
            parts.append(f"{market} {format_krw_amount(net_value)}")

    if not parts:
        return f"{investor_name}: 확인 필요"

    return f"{investor_name}: " + " / ".join(parts)


def format_top_stock_list(items, limit=5):
    if not items:
        return "확인 필요"

    parts = []

    for item in items[:limit]:
        name = item.get("name") or item.get("ticker")
        change_pct = item.get("change_pct")
        trading_value_text = item.get("trading_value_text", "")

        if change_pct is None:
            parts.append(f"{name}({trading_value_text})")
        else:
            parts.append(f"{name}({change_pct:+.2f}%, {trading_value_text})")

    return ", ".join(parts)



# =========================
# 업종/테마 자동 분류
# =========================

STOCK_THEME_MAP = {
    # 반도체·AI
    "005930": "반도체·AI",
    "000660": "반도체·AI",
    "042700": "반도체·AI",
    "009150": "반도체·AI",
    "058470": "반도체·AI",
    "036930": "반도체·AI",
    "000990": "반도체·AI",
    "039030": "반도체·AI",
    "108320": "반도체·AI",
    "240810": "반도체·AI",
    "319660": "반도체·AI",
    "214450": "반도체·AI",

    # 2차전지
    "373220": "2차전지",
    "051910": "2차전지",
    "006400": "2차전지",
    "096770": "2차전지",
    "247540": "2차전지",
    "003670": "2차전지",
    "086520": "2차전지",
    "066970": "2차전지",
    "011790": "2차전지",

    # 자동차
    "005380": "자동차",
    "000270": "자동차",
    "012330": "자동차",
    "161390": "자동차",
    "018880": "자동차",

    # 금융
    "105560": "금융",
    "055550": "금융",
    "086790": "금융",
    "032830": "금융",
    "000810": "금융",
    "138930": "금융",
    "024110": "금융",
    "316140": "금융",

    # 조선·기계·산업재
    "010140": "조선·기계·산업재",
    "329180": "조선·기계·산업재",
    "042660": "조선·기계·산업재",
    "009540": "조선·기계·산업재",
    "034020": "조선·기계·산업재",
    "267260": "조선·기계·산업재",
    "064350": "조선·기계·산업재",
    "042670": "조선·기계·산업재",

    # 방산·우주항공
    "012450": "방산·우주항공",
    "047810": "방산·우주항공",
    "064960": "방산·우주항공",
    "079550": "방산·우주항공",
    "272210": "방산·우주항공",

    # 바이오
    "068270": "바이오",
    "207940": "바이오",
    "128940": "바이오",
    "326030": "바이오",
    "302440": "바이오",
    "145020": "바이오",
    "196170": "바이오",

    # 인터넷·게임
    "035420": "인터넷·게임",
    "035720": "인터넷·게임",
    "036570": "인터넷·게임",
    "251270": "인터넷·게임",
    "259960": "인터넷·게임",
    "112040": "인터넷·게임",

    # 항공·운송
    "003490": "항공·운송",
    "180640": "항공·운송",
    "000120": "항공·운송",
    "011200": "항공·운송",
    "086280": "항공·운송",
    "028670": "항공·운송",

    # 정유·에너지
    "010950": "정유·에너지",
    "096770": "정유·에너지",
    "267250": "정유·에너지",
    "015760": "정유·에너지",
    "034730": "정유·에너지",
    "017670": "정유·에너지",

    # 화학·소재
    "051910": "화학·소재",
    "011170": "화학·소재",
    "009830": "화학·소재",
    "010130": "화학·소재",
    "004020": "화학·소재",

    # 소비재·화장품
    "090430": "소비재·화장품",
    "051900": "소비재·화장품",
    "018260": "소비재·화장품",
    "097950": "소비재·화장품",
    "271560": "소비재·화장품",

    # 건설·부동산
    "000720": "건설·부동산",
    "006360": "건설·부동산",
    "047040": "건설·부동산",
    "028050": "건설·부동산",
}

THEME_KEYWORD_RULES = [
    ("반도체·AI", ["반도체", "하이닉스", "삼성전자", "칩", "테크", "HBM", "AI", "실리콘", "테스", "리노", "원익", "한미반도체"]),
    ("2차전지", ["에코프로", "엘앤에프", "포스코퓨처", "배터리", "2차전지", "전지", "양극재", "음극재", "전해액"]),
    ("자동차", ["현대차", "기아", "모비스", "만도", "자동차", "타이어"]),
    ("금융", ["금융", "은행", "보험", "증권", "카드", "지주"]),
    ("조선·기계·산업재", ["조선", "중공업", "엔진", "기계", "로보틱스", "두산", "현대로템", "산업"]),
    ("방산·우주항공", ["방산", "항공", "우주", "한화에어로", "한국항공", "LIG", "풍산"]),
    ("바이오", ["바이오", "제약", "셀트리온", "삼성바이오", "헬스", "메디", "약품"]),
    ("인터넷·게임", ["NAVER", "카카오", "게임", "엔씨", "크래프톤", "넷마블", "인터넷"]),
    ("항공·운송", ["항공", "대한항공", "운송", "해운", "HMM", "글로비스", "CJ대한통운"]),
    ("정유·에너지", ["정유", "에너지", "S-Oil", "SK이노", "가스", "전력", "한국전력", "석유"]),
    ("화학·소재", ["화학", "소재", "금속", "철강", "포스코", "고려아연", "현대제철"]),
    ("소비재·화장품", ["화장품", "아모레", "LG생활", "식품", "오리온", "CJ제일제당", "농심"]),
    ("건설·부동산", ["건설", "현대건설", "대우건설", "DL이앤씨", "부동산"]),
]


def classify_stock_theme(ticker, name):
    """
    종목코드와 종목명으로 업종/테마를 자동 분류합니다.
    1차: 주요 종목 코드 매핑
    2차: 종목명 키워드 분류
    """
    ticker = str(ticker).zfill(6)
    name = name or ""

    if ticker in STOCK_THEME_MAP:
        return STOCK_THEME_MAP[ticker]

    upper_name = name.upper()

    for theme, keywords in THEME_KEYWORD_RULES:
        for keyword in keywords:
            if keyword.upper() in upper_name:
                return theme

    return "기타"


def enrich_stocks_with_theme(items):
    result = []

    for item in items or []:
        enriched = dict(item)
        enriched["theme"] = classify_stock_theme(
            enriched.get("ticker", ""),
            enriched.get("name", "")
        )
        result.append(enriched)

    return result


def aggregate_theme_trading_value(items):
    """
    거래대금 상위 종목을 업종/테마별로 합산합니다.
    """
    theme_map = {}

    for item in items or []:
        theme = item.get("theme") or classify_stock_theme(item.get("ticker", ""), item.get("name", ""))
        trading_value = safe_float(item.get("trading_value")) or 0
        change_pct = safe_float(item.get("change_pct"))

        if theme not in theme_map:
            theme_map[theme] = {
                "theme": theme,
                "stock_count": 0,
                "total_trading_value": 0,
                "total_change_pct": 0,
                "change_pct_count": 0,
                "top_stocks": [],
            }

        theme_map[theme]["stock_count"] += 1
        theme_map[theme]["total_trading_value"] += trading_value

        if change_pct is not None:
            theme_map[theme]["total_change_pct"] += change_pct
            theme_map[theme]["change_pct_count"] += 1

        theme_map[theme]["top_stocks"].append(
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "change_pct": item.get("change_pct"),
                "trading_value": item.get("trading_value"),
                "trading_value_text": item.get("trading_value_text"),
            }
        )

    result = []

    for theme_data in theme_map.values():
        count = theme_data["change_pct_count"]

        if count > 0:
            avg_change_pct = theme_data["total_change_pct"] / count
        else:
            avg_change_pct = None

        theme_data["avg_change_pct"] = avg_change_pct
        theme_data["total_trading_value_text"] = format_krw_amount(theme_data["total_trading_value"])

        theme_data["top_stocks"] = sorted(
            theme_data["top_stocks"],
            key=lambda x: safe_float(x.get("trading_value")) or 0,
            reverse=True,
        )[:5]

        result.append(theme_data)

    result.sort(
        key=lambda x: safe_float(x.get("total_trading_value")) or 0,
        reverse=True,
    )

    return result


def format_theme_summary(theme_items, limit=6):
    if not theme_items:
        return "확인 필요"

    parts = []

    for item in theme_items[:limit]:
        theme = item.get("theme")
        total_text = item.get("total_trading_value_text")
        avg_change_pct = item.get("avg_change_pct")
        top_stocks = item.get("top_stocks", [])

        top_stock_names = ", ".join([s.get("name", "") for s in top_stocks[:3] if s.get("name")])

        if avg_change_pct is None:
            parts.append(f"{theme}: {total_text} / 대표: {top_stock_names}")
        else:
            parts.append(f"{theme}: {total_text}, 평균 {avg_change_pct:+.2f}% / 대표: {top_stock_names}")

    return " | ".join(parts)


# =========================
# KOSPI200 선물 / 외국인 선물 수급
# =========================

def fetch_kospi200_futures_data():
    """
    KOSPI200 선물 데이터를 무료 공개 소스 후보로 조회합니다.

    주의:
    - Yahoo Finance의 한국 파생상품 심볼은 환경에 따라 조회가 안 될 수 있습니다.
    - 실패하면 KOSPI200 지수 대체 데이터와 '확인 필요'를 함께 제공합니다.
    """
    candidates = [
        ("KR200=F", "KOSPI200 Futures 후보 1"),
        ("K200=F", "KOSPI200 Futures 후보 2"),
        ("KS200=F", "KOSPI200 Futures 후보 3"),
    ]

    for symbol, name in candidates:
        quote = fetch_yfinance_quote(symbol, name)

        if quote.get("status") == "ok":
            quote["data_note"] = "Yahoo Finance 후보 심볼 기준입니다. 실제 KRX 선물과 차이가 있을 수 있어 확인 필요합니다."
            return {
                "status": "ok",
                "source": "Yahoo Finance candidate symbol",
                "quote": quote,
                "summary": quote_to_text(quote),
            }

    return {
        "status": "확인 필요",
        "source": "not_available",
        "summary": "확인 필요 - 무료 공개 데이터 후보에서 KOSPI200 선물 조회 실패",
        "data_note": "현재 버전은 KOSPI200 지수 대체 데이터와 함께 확인 필요로 표시합니다.",
    }


def fetch_foreign_futures_flow_data(date_text):
    """
    외국인 선물 수급 데이터 수집 함수입니다.

    현재 안정적인 무료 공개 Python 라이브러리 연결이 제한되어 있어
    자동 수집 실패 시 명확히 '확인 필요'로 표시합니다.

    이후 KRX 파생상품 전용 엔드포인트가 안정적으로 확인되면 이 함수만 교체하면 됩니다.
    """
    return {
        "status": "확인 필요",
        "latest_trading_date": date_text,
        "summary": "확인 필요 - 외국인 선물 수급은 KRX 파생상품 전용 데이터 추가 연결 필요",
        "data_note": "현재 버전은 외국인 현물 수급과 KOSPI200 지수/선물 후보 데이터를 함께 참고합니다.",
    }



def fetch_krx_market_data():
    """
    pykrx 기반 한국시장 전용 데이터입니다.

    개선점:
    - pykrx/KRX 조회 실패 시 반복 오류를 길게 출력하지 않습니다.
    - 실패해도 전체 보고서 생성은 계속 진행됩니다.
    - KRX 데이터 상태를 data_packet에 명확히 표시합니다.
    """
    print("KRX/pykrx 한국시장 데이터 수집 시작")

    kospi200_futures = fetch_kospi200_futures_data()

    if krx_stock is None:
        print("KRX/pykrx 사용 불가: pykrx 패키지가 설치되어 있지 않습니다.")

        return {
            "status": "확인 필요",
            "reason": "pykrx 패키지가 설치되어 있지 않습니다.",
            "latest_trading_date": None,
            "investor_flows": {},
            "top_trading_value": {},
            "top_gainers": {},
            "theme_trading_value": [],
            "theme_trading_value_summary": "확인 필요 - pykrx 미설치",
            "kospi200_futures": kospi200_futures,
            "foreign_futures_flow": fetch_foreign_futures_flow_data(None),
        }

    latest_date = get_latest_krx_trading_date(max_lookback_days=10)

    if not latest_date:
        print("KRX/pykrx 수집 실패: 최근 거래일 확인 불가. yfinance 데이터만 사용합니다.")

        return {
            "status": "확인 필요",
            "reason": "KRX 최근 거래일 확인 실패 또는 GitHub Actions 환경에서 KRX 응답 사용 불가",
            "latest_trading_date": None,
            "investor_flows": {},
            "top_trading_value": {},
            "top_gainers": {},
            "theme_trading_value": [],
            "theme_trading_value_summary": "확인 필요 - KRX 데이터 수집 실패",
            "kospi200_futures": kospi200_futures,
            "foreign_futures_flow": fetch_foreign_futures_flow_data(None),
        }

    investor_flows = {
        "KOSPI": fetch_krx_investor_flow(latest_date, "KOSPI"),
        "KOSDAQ": fetch_krx_investor_flow(latest_date, "KOSDAQ"),
    }

    top_trading_value = {
        "KOSPI": enrich_stocks_with_theme(
            fetch_krx_top_trading_value(latest_date, "KOSPI", limit=100)
        ),
        "KOSDAQ": enrich_stocks_with_theme(
            fetch_krx_top_trading_value(latest_date, "KOSDAQ", limit=100)
        ),
    }

    top_gainers = {
        "KOSPI": enrich_stocks_with_theme(
            fetch_krx_top_gainers(latest_date, "KOSPI", limit=30)
        ),
        "KOSDAQ": enrich_stocks_with_theme(
            fetch_krx_top_gainers(latest_date, "KOSDAQ", limit=30)
        ),
    }

    all_top_value_items = top_trading_value["KOSPI"] + top_trading_value["KOSDAQ"]
    theme_trading_value = aggregate_theme_trading_value(all_top_value_items)

    foreign_futures_flow = fetch_foreign_futures_flow_data(latest_date)

    # KRX가 응답했지만 실질 데이터가 없는 경우도 실패로 처리
    has_useful_krx_data = (
        bool(top_trading_value["KOSPI"])
        or bool(top_trading_value["KOSDAQ"])
        or investor_flows["KOSPI"].get("status") == "ok"
        or investor_flows["KOSDAQ"].get("status") == "ok"
    )

    if not has_useful_krx_data:
        print("KRX/pykrx 수집 실패: 조회 가능한 실질 데이터가 없습니다. yfinance 데이터만 사용합니다.")

        return {
            "status": "확인 필요",
            "reason": "KRX 응답은 있었지만 수급/거래대금 데이터 조회 실패",
            "latest_trading_date": latest_date,
            "investor_flows": investor_flows,
            "top_trading_value": top_trading_value,
            "top_gainers": top_gainers,
            "theme_trading_value": [],
            "theme_trading_value_summary": "확인 필요 - KRX 수급/거래대금 데이터 조회 실패",
            "kospi200_futures": kospi200_futures,
            "foreign_futures_flow": foreign_futures_flow,
        }

    krx_data = {
        "status": "ok",
        "data_source": "KRX via pykrx + yfinance candidate futures",
        "latest_trading_date": latest_date,
        "data_delay_note": "KRX/pykrx 데이터는 장중 실시간 데이터가 아닐 수 있으며, 최근 조회 가능 거래일 기준입니다.",
        "investor_flows": investor_flows,
        "top_trading_value": top_trading_value,
        "top_gainers": top_gainers,
        "theme_trading_value": theme_trading_value,
        "theme_trading_value_summary": format_theme_summary(theme_trading_value, limit=8),
        "kospi200_futures": kospi200_futures,
        "foreign_futures_flow": foreign_futures_flow,
    }

    print(f"KRX/pykrx 한국시장 데이터 수집 완료: 기준일 {latest_date}")
    print(f"업종/테마 거래대금 집계 완료: {len(theme_trading_value)}개 테마")
    print(f"KOSPI200 선물 상태: {kospi200_futures.get('status')}")
    print(f"외국인 선물 수급 상태: {foreign_futures_flow.get('status')}")

    return krx_data



def merge_krx_data_into_market_data(market_data, krx_data):
    """
    yfinance 기반 market_data에 pykrx 한국시장 데이터를 보강합니다.
    """
    market_data["krx_data"] = krx_data

    # KOSPI200 선물 후보 데이터는 KRX 상태와 무관하게 반영합니다.
    kospi200_futures = krx_data.get("kospi200_futures", {})
    market_data["kospi200_futures_data"] = kospi200_futures
    market_data["kospi200_futures"] = kospi200_futures.get(
        "summary",
        "확인 필요 - KOSPI200 선물 데이터 없음"
    )

    foreign_futures_flow = krx_data.get("foreign_futures_flow", {})
    market_data["foreign_futures_flow_data"] = foreign_futures_flow
    market_data["foreign_futures_flow"] = foreign_futures_flow.get(
        "summary",
        "확인 필요 - 외국인 선물 수급 데이터 없음"
    )

    if krx_data.get("status") != "ok":
        market_data["krx_data_status"] = krx_data.get("reason", "확인 필요")
        market_data["foreign_spot_flow"] = "확인 필요 - KRX 데이터 수집 실패"
        market_data["institution_flow"] = "확인 필요 - KRX 데이터 수집 실패"
        market_data["retail_flow"] = "확인 필요 - KRX 데이터 수집 실패"
        market_data["leading_sectors_by_volume"] = krx_data.get(
            "theme_trading_value_summary",
            "확인 필요 - KRX 데이터 수집 실패"
        )
        market_data["leading_stocks_summary"] = "확인 필요 - KRX 거래대금 데이터 수집 실패"
        market_data["top_gainers_summary"] = "확인 필요 - KRX 등락률 데이터 수집 실패"
        return market_data

    market_data["krx_data_status"] = "ok"
    market_data["krx_latest_trading_date"] = krx_data.get("latest_trading_date")

    market_data["foreign_spot_flow"] = build_single_investor_flow_text(
        krx_data,
        "외국인 현물 수급",
        ["외국인", "외국인합계"]
    )

    market_data["institution_flow"] = build_single_investor_flow_text(
        krx_data,
        "기관 수급",
        ["기관", "기관합계", "금융투자", "투신", "연기금"]
    )

    market_data["retail_flow"] = build_single_investor_flow_text(
        krx_data,
        "개인 수급",
        ["개인"]
    )

    kospi_top_value = krx_data.get("top_trading_value", {}).get("KOSPI", [])
    kosdaq_top_value = krx_data.get("top_trading_value", {}).get("KOSDAQ", [])
    kospi_top_gainers = krx_data.get("top_gainers", {}).get("KOSPI", [])
    kosdaq_top_gainers = krx_data.get("top_gainers", {}).get("KOSDAQ", [])

    market_data["leading_stocks_by_trading_value"] = {
        "KOSPI": kospi_top_value[:20],
        "KOSDAQ": kosdaq_top_value[:20],
    }

    market_data["top_gainers"] = {
        "KOSPI": kospi_top_gainers[:20],
        "KOSDAQ": kosdaq_top_gainers[:20],
    }

    market_data["theme_trading_value"] = krx_data.get("theme_trading_value", [])
    market_data["theme_trading_value_summary"] = krx_data.get(
        "theme_trading_value_summary",
        "확인 필요"
    )

    market_data["leading_sectors_by_volume"] = (
        "업종/테마 자동 분류 기준 거래대금 상위 - "
        f"{market_data['theme_trading_value_summary']}"
    )

    market_data["leading_stocks_summary"] = (
        "거래대금 상위 종목 - "
        f"KOSPI: {format_top_stock_list(kospi_top_value, limit=5)} / "
        f"KOSDAQ: {format_top_stock_list(kosdaq_top_value, limit=5)}"
    )

    market_data["top_gainers_summary"] = (
        "등락률 상위 종목 - "
        f"KOSPI: {format_top_stock_list(kospi_top_gainers, limit=5)} / "
        f"KOSDAQ: {format_top_stock_list(kosdaq_top_gainers, limit=5)}"
    )

    return market_data




def fetch_yfinance_market_data(report_mode):
    """
    한국증시 장전·장중 자동화에 필요한 실제 시장 데이터를 1차로 연결합니다.

    현재 연결 데이터:
    - KOSPI, KOSDAQ
    - KOSPI200 지수 대체 데이터
    - 삼성전자, SK하이닉스
    - 원/달러 환율
    - 미국 주요 지수와 선물
    - 달러 인덱스, 미국 10년물 금리
    - WTI, Brent, 금
    - 주요 빅테크
    """
    print("Yahoo Finance/yfinance 시장 데이터 수집 시작")

    korea_indices = {
        "kospi": fetch_yfinance_quote("^KS11", "KOSPI"),
        "kosdaq": fetch_yfinance_quote("^KQ11", "KOSDAQ"),
        "kospi200": fetch_yfinance_quote("^KS200", "KOSPI200"),
    }

    korea_key_stocks = {
        "samsung_electronics": fetch_yfinance_quote("005930.KS", "삼성전자"),
        "sk_hynix": fetch_yfinance_quote("000660.KS", "SK하이닉스"),
    }

    fx_rates = {
        "usd_krw": first_ok_quote([
            ("KRW=X", "원/달러 환율"),
            ("USDKRW=X", "원/달러 환율"),
        ]),
    }

    us_indices = {
        "sp500": fetch_yfinance_quote("^GSPC", "S&P 500"),
        "nasdaq": fetch_yfinance_quote("^IXIC", "Nasdaq"),
        "dow": fetch_yfinance_quote("^DJI", "Dow"),
        "russell2000": fetch_yfinance_quote("^RUT", "Russell 2000"),
        "sox": fetch_yfinance_quote("^SOX", "필라델피아 반도체지수"),
    }

    us_futures = {
        "sp500_futures": fetch_yfinance_quote("ES=F", "S&P 500 Futures"),
        "nasdaq_futures": fetch_yfinance_quote("NQ=F", "Nasdaq Futures"),
        "dow_futures": fetch_yfinance_quote("YM=F", "Dow Futures"),
        "russell2000_futures": fetch_yfinance_quote("RTY=F", "Russell 2000 Futures"),
    }

    global_indicators = {
        "us_10y_yield": fetch_yfinance_quote("^TNX", "미국 10년물 국채금리"),
        "dollar_index": fetch_yfinance_quote("DX-Y.NYB", "달러 인덱스"),
    }

    commodities = {
        "wti": fetch_yfinance_quote("CL=F", "WTI 원유"),
        "brent": fetch_yfinance_quote("BZ=F", "Brent 원유"),
        "gold": fetch_yfinance_quote("GC=F", "금"),
    }

    big_tech = {
        "nvidia": fetch_yfinance_quote("NVDA", "NVIDIA"),
        "apple": fetch_yfinance_quote("AAPL", "Apple"),
        "microsoft": fetch_yfinance_quote("MSFT", "Microsoft"),
        "tesla": fetch_yfinance_quote("TSLA", "Tesla"),
    }

    market_data = {
        "data_source": "Yahoo Finance via yfinance",
        "data_delay_note": "무료 공개 데이터 기반이므로 지연 시세일 수 있습니다.",
        "report_mode": report_mode,

        "korea_indices": korea_indices,
        "korea_key_stocks": korea_key_stocks,
        "fx_rates": fx_rates,
        "us_indices": us_indices,
        "us_futures_quotes": us_futures,
        "global_indicators": global_indicators,
        "commodities": commodities,
        "big_tech_quotes": big_tech,

        # 프롬프트가 바로 읽기 쉬운 요약 필드
        "kospi_current": quote_to_text(korea_indices["kospi"]),
        "kosdaq_current": quote_to_text(korea_indices["kosdaq"]),
        "kospi200_index": quote_to_text(korea_indices["kospi200"]),
        "kospi200_futures": "확인 필요 - 현재 1차 버전은 KOSPI200 지수 대체 데이터만 제공",
        "samsung_electronics": quote_to_text(korea_key_stocks["samsung_electronics"]),
        "sk_hynix": quote_to_text(korea_key_stocks["sk_hynix"]),
        "usd_krw": quote_to_text(fx_rates["usd_krw"]),
        "usd_krw_or_ndf": quote_to_text(fx_rates["usd_krw"]),

        "sp500": quote_to_text(us_indices["sp500"]),
        "nasdaq": quote_to_text(us_indices["nasdaq"]),
        "dow": quote_to_text(us_indices["dow"]),
        "russell2000": quote_to_text(us_indices["russell2000"]),
        "semiconductor_index": quote_to_text(us_indices["sox"]),
        "us_futures": {
            "sp500_futures": quote_to_text(us_futures["sp500_futures"]),
            "nasdaq_futures": quote_to_text(us_futures["nasdaq_futures"]),
            "dow_futures": quote_to_text(us_futures["dow_futures"]),
            "russell2000_futures": quote_to_text(us_futures["russell2000_futures"]),
        },
        "us_10y_yield": quote_to_text(global_indicators["us_10y_yield"]),
        "dollar_index": quote_to_text(global_indicators["dollar_index"]),
        "wti": quote_to_text(commodities["wti"]),
        "brent": quote_to_text(commodities["brent"]),
        "wti_brent": {
            "wti": quote_to_text(commodities["wti"]),
            "brent": quote_to_text(commodities["brent"]),
        },
        "gold": quote_to_text(commodities["gold"]),
        "big_tech": {
            "nvidia": quote_to_text(big_tech["nvidia"]),
            "apple": quote_to_text(big_tech["apple"]),
            "microsoft": quote_to_text(big_tech["microsoft"]),
            "tesla": quote_to_text(big_tech["tesla"]),
        },

        # 아직 미연결 데이터
        "foreign_spot_flow": "확인 필요 - 외국인 현물 수급은 2차 연결 예정",
        "foreign_futures_flow": "확인 필요 - 외국인 선물 수급은 2차 연결 예정",
        "institution_flow": "확인 필요 - 기관 수급은 2차 연결 예정",
        "retail_flow": "확인 필요 - 개인 수급은 2차 연결 예정",
        "leading_sectors_by_volume": "확인 필요 - 거래대금 상위 업종은 2차 연결 예정",
        "asia_market_preopen": "확인 필요",
        "domestic_premarket_news": "뉴스/공시 데이터 기반으로 확인",
        "us_sector_flow": "확인 필요 - 미국 업종별 세부 흐름은 2차 연결 예정",
    }

    krx_data = fetch_krx_market_data()
    market_data = merge_krx_data_into_market_data(market_data, krx_data)

    ok_count = 0
    check_count = 0

    for group_name in [
        "korea_indices",
        "korea_key_stocks",
        "fx_rates",
        "us_indices",
        "us_futures_quotes",
        "global_indicators",
        "commodities",
        "big_tech_quotes",
    ]:
        for item in market_data[group_name].values():
            if isinstance(item, dict) and item.get("status") == "ok":
                ok_count += 1
            else:
                check_count += 1

    market_data["fetch_summary"] = {
        "success_count": ok_count,
        "check_needed_count": check_count,
    }

    print(f"Yahoo Finance/yfinance 시장 데이터 수집 완료: 성공 {ok_count}개, 확인 필요 {check_count}개")

    return market_data


def build_market_data_status(report_mode):
    return fetch_yfinance_market_data(report_mode)


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
            "KOSPI/KOSDAQ, 삼성전자, SK하이닉스, 원/달러 환율, 미국 지수·선물, "
            "금리·달러·유가·금 일부 데이터는 yfinance 기반으로 자동 수집됩니다. "
            "한국시장 투자자별 수급, 거래대금 상위 종목, 등락률 상위 종목은 pykrx 기반으로 자동 수집됩니다. "
            "거래대금 상위 종목은 업종/테마 자동 분류 로직으로 집계됩니다. "
            "KOSPI200 선물은 무료 공개 후보 심볼로 조회하며 실패 시 확인 필요로 표시합니다. "
            "외국인 선물 수급은 아직 안정적인 공개 라이브러리 연결이 제한되어 확인 필요로 표시할 수 있습니다. "
            "무료 공개/스크래핑 데이터라 지연되거나 일부 항목이 누락될 수 있습니다. "
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
