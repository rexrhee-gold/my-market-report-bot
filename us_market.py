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

try:
    import yfinance as yf
except Exception:
    yf = None


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

EMAIL_TO = os.getenv("EMAIL_TO")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

US_PROMPT_PATH = os.getenv("US_PROMPT_PATH", "prompts/us_market_v4.md")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 시간 함수
# =========================

def now_kst():
    return datetime.now(KST)


def now_utc():
    return datetime.now(UTC)


def since_utc(hours=24):
    return now_utc() - timedelta(hours=hours)


def alpha_time_from(hours=72):
    target_time = now_utc() - timedelta(hours=hours)
    return target_time.strftime("%Y%m%dT%H%M")


# =========================
# 기본 유틸
# =========================

def safe_float(value):
    try:
        if value is None:
            return None
        if hasattr(value, "item"):
            value = value.item()
        value = float(value)
        if value != value:
            return None
        return value
    except Exception:
        return None


def format_number(value, decimals=2):
    value = safe_float(value)
    if value is None:
        return "확인 필요"
    return f"{value:,.{decimals}f}"


def calculate_change(last_price, previous_close):
    last_price = safe_float(last_price)
    previous_close = safe_float(previous_close)

    if last_price is None or previous_close is None or previous_close == 0:
        return None, None

    change = last_price - previous_close
    change_pct = (change / previous_close) * 100
    return change, change_pct


def fast_info_get(fast_info, key):
    try:
        return fast_info.get(key)
    except Exception:
        try:
            return getattr(fast_info, key)
        except Exception:
            return None


def quote_to_text(quote):
    if not quote or quote.get("status") != "ok":
        return "확인 필요"

    last_price = quote.get("last_price")
    change = quote.get("change")
    change_pct = quote.get("change_pct")

    if change is None or change_pct is None:
        return f"{format_number(last_price)}"

    return f"{format_number(last_price)} ({change:+.2f}, {change_pct:+.2f}%)"


def dedupe_items(items, key_fields):
    seen = set()
    result = []

    for item in items:
        raw = "|".join(str(item.get(field, "")) for field in key_fields)
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


# =========================
# yfinance 시세 수집
# =========================

def fetch_yfinance_quote(symbol, name):
    empty_result = {
        "symbol": symbol,
        "name": name,
        "status": "확인 필요",
        "last_price": None,
        "previous_close": None,
        "change": None,
        "change_pct": None,
        "currency": None,
        "volume": None,
        "market_cap": None,
        "source": "yfinance",
    }

    if yf is None:
        empty_result["error"] = "yfinance 패키지가 설치되어 있지 않습니다."
        return empty_result

    try:
        ticker = yf.Ticker(symbol)

        last_price = None
        previous_close = None
        currency = None
        volume = None
        market_cap = None

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
            market_cap = safe_float(
                fast_info_get(fast_info, "market_cap")
                or fast_info_get(fast_info, "marketCap")
            )
        except Exception:
            pass

        try:
            intraday = ticker.history(period="1d", interval="5m", prepost=True)
            if intraday is not None and not intraday.empty:
                close_series = intraday["Close"].dropna()
                volume_series = intraday["Volume"].dropna() if "Volume" in intraday.columns else None

                if not close_series.empty:
                    last_price = safe_float(close_series.iloc[-1])

                if volume_series is not None and not volume_series.empty:
                    volume = safe_float(volume_series.sum())
        except Exception:
            pass

        try:
            daily = ticker.history(period="7d", interval="1d")
            if daily is not None and not daily.empty:
                closes = daily["Close"].dropna()

                if last_price is None and len(closes) >= 1:
                    last_price = safe_float(closes.iloc[-1])

                if previous_close is None and len(closes) >= 2:
                    previous_close = safe_float(closes.iloc[-2])

                if volume is None and "Volume" in daily.columns:
                    volume_values = daily["Volume"].dropna()
                    if not volume_values.empty:
                        volume = safe_float(volume_values.iloc[-1])
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
            "volume": volume,
            "market_cap": market_cap,
            "source": "yfinance",
        }

    except Exception as error:
        empty_result["error"] = str(error)
        return empty_result


def fetch_quote_group(symbol_map):
    result = {}
    for key, info in symbol_map.items():
        result[key] = fetch_yfinance_quote(info["symbol"], info["name"])
    return result


def summarize_quote_group(quotes):
    result = {}
    for key, quote in quotes.items():
        result[key] = quote_to_text(quote)
    return result


def get_top_movers(quotes, limit=5, direction="up"):
    valid = []
    for key, quote in quotes.items():
        change_pct = quote.get("change_pct")
        if quote.get("status") == "ok" and change_pct is not None:
            valid.append(quote)

    reverse = direction == "up"
    valid.sort(key=lambda item: item.get("change_pct", 0), reverse=reverse)

    return valid[:limit]


def build_market_data():
    print("미국시장 yfinance 데이터 수집 시작")

    indices_symbols = {
        "sp500": {"symbol": "^GSPC", "name": "S&P 500"},
        "nasdaq": {"symbol": "^IXIC", "name": "Nasdaq Composite"},
        "dow": {"symbol": "^DJI", "name": "Dow Jones"},
        "russell2000": {"symbol": "^RUT", "name": "Russell 2000"},
        "sox": {"symbol": "^SOX", "name": "Philadelphia Semiconductor Index"},
    }

    futures_symbols = {
        "sp500_futures": {"symbol": "ES=F", "name": "S&P 500 Futures"},
        "nasdaq_futures": {"symbol": "NQ=F", "name": "Nasdaq Futures"},
        "dow_futures": {"symbol": "YM=F", "name": "Dow Futures"},
        "russell2000_futures": {"symbol": "RTY=F", "name": "Russell 2000 Futures"},
    }

    m7_symbols = {
        "aapl": {"symbol": "AAPL", "name": "Apple"},
        "msft": {"symbol": "MSFT", "name": "Microsoft"},
        "nvda": {"symbol": "NVDA", "name": "NVIDIA"},
        "amzn": {"symbol": "AMZN", "name": "Amazon"},
        "googl": {"symbol": "GOOGL", "name": "Alphabet"},
        "meta": {"symbol": "META", "name": "Meta"},
        "tsla": {"symbol": "TSLA", "name": "Tesla"},
    }

    semiconductor_symbols = {
        "nvda": {"symbol": "NVDA", "name": "NVIDIA"},
        "amd": {"symbol": "AMD", "name": "AMD"},
        "avgo": {"symbol": "AVGO", "name": "Broadcom"},
        "tsm": {"symbol": "TSM", "name": "TSMC"},
        "arm": {"symbol": "ARM", "name": "ARM"},
        "mu": {"symbol": "MU", "name": "Micron"},
        "mrvl": {"symbol": "MRVL", "name": "Marvell"},
        "asml": {"symbol": "ASML", "name": "ASML"},
    }

    sector_etf_symbols = {
        "spy": {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
        "qqq": {"symbol": "QQQ", "name": "Invesco QQQ"},
        "iwm": {"symbol": "IWM", "name": "iShares Russell 2000 ETF"},
        "smh": {"symbol": "SMH", "name": "VanEck Semiconductor ETF"},
        "xlk": {"symbol": "XLK", "name": "Technology Select Sector SPDR"},
        "xlf": {"symbol": "XLF", "name": "Financial Select Sector SPDR"},
        "xle": {"symbol": "XLE", "name": "Energy Select Sector SPDR"},
        "xly": {"symbol": "XLY", "name": "Consumer Discretionary SPDR"},
        "xlp": {"symbol": "XLP", "name": "Consumer Staples SPDR"},
        "xlv": {"symbol": "XLV", "name": "Health Care SPDR"},
        "xli": {"symbol": "XLI", "name": "Industrial SPDR"},
        "xlu": {"symbol": "XLU", "name": "Utilities SPDR"},
        "xlp": {"symbol": "XLP", "name": "Consumer Staples SPDR"},
        "tlt": {"symbol": "TLT", "name": "20+ Year Treasury Bond ETF"},
        "hyg": {"symbol": "HYG", "name": "High Yield Corporate Bond ETF"},
        "lqd": {"symbol": "LQD", "name": "Investment Grade Corporate Bond ETF"},
    }

    macro_symbols = {
        "us_10y_yield": {"symbol": "^TNX", "name": "US 10Y Treasury Yield"},
        "us_30y_yield": {"symbol": "^TYX", "name": "US 30Y Treasury Yield"},
        "dollar_index": {"symbol": "DX-Y.NYB", "name": "US Dollar Index"},
        "gold": {"symbol": "GC=F", "name": "Gold Futures"},
        "wti": {"symbol": "CL=F", "name": "WTI Crude Oil Futures"},
        "brent": {"symbol": "BZ=F", "name": "Brent Crude Oil Futures"},
    }

    volatility_symbols = {
        "vix": {"symbol": "^VIX", "name": "VIX"},
        "vvix": {"symbol": "^VVIX", "name": "VVIX"},
        "skew": {"symbol": "^SKEW", "name": "CBOE SKEW"},
    }

    watchlist_symbols = load_us_watchlist()

    data = {
        "data_source": "Yahoo Finance via yfinance",
        "data_delay_note": "무료 공개 데이터 기반이므로 지연 시세일 수 있습니다.",
        "indices": fetch_quote_group(indices_symbols),
        "futures": fetch_quote_group(futures_symbols),
        "m7": fetch_quote_group(m7_symbols),
        "semiconductors": fetch_quote_group(semiconductor_symbols),
        "sector_etfs": fetch_quote_group(sector_etf_symbols),
        "macro": fetch_quote_group(macro_symbols),
        "volatility": fetch_quote_group(volatility_symbols),
        "custom_watchlist": fetch_quote_group(watchlist_symbols) if watchlist_symbols else {},
    }

    # 프롬프트가 바로 읽기 쉬운 요약 필드
    data["indices_summary"] = summarize_quote_group(data["indices"])
    data["futures_summary"] = summarize_quote_group(data["futures"])
    data["m7_summary"] = summarize_quote_group(data["m7"])
    data["semiconductors_summary"] = summarize_quote_group(data["semiconductors"])
    data["sector_etfs_summary"] = summarize_quote_group(data["sector_etfs"])
    data["macro_summary"] = summarize_quote_group(data["macro"])
    data["volatility_summary"] = summarize_quote_group(data["volatility"])

    all_equity_quotes = {}
    all_equity_quotes.update(data["m7"])
    all_equity_quotes.update(data["semiconductors"])
    all_equity_quotes.update(data["sector_etfs"])
    all_equity_quotes.update(data["custom_watchlist"])

    data["top_gainers_sample"] = get_top_movers(all_equity_quotes, limit=8, direction="up")
    data["top_losers_sample"] = get_top_movers(all_equity_quotes, limit=8, direction="down")

    # 아직 직접 수집하지 않는 고급 데이터는 명시적으로 확인 필요 처리
    data["breadth_data"] = {
        "advance_decline": "확인 필요",
        "advance_ratio": "확인 필요",
        "new_highs": "확인 필요",
        "new_lows": "확인 필요",
        "equal_weight_relative_strength": "확인 필요",
    }

    data["options_data"] = {
        "put_call_ratio": "확인 필요",
        "max_pain": "확인 필요",
        "gamma_exposure": "확인 필요",
        "dealer_gamma": "확인 필요",
        "zero_dte": "확인 필요",
        "note": "옵션 전문 데이터 소스가 연결되지 않았으므로 추정 금지",
    }

    data["etf_flow_data"] = {
        "fund_flow": "확인 필요",
        "note": "ETF 자금 유입/유출 전문 데이터가 연결되지 않았으므로 가격·거래량 기반 대체 해석만 가능",
    }

    ok_count = 0
    check_count = 0

    for group_name in [
        "indices",
        "futures",
        "m7",
        "semiconductors",
        "sector_etfs",
        "macro",
        "volatility",
        "custom_watchlist",
    ]:
        for quote in data[group_name].values():
            if quote.get("status") == "ok":
                ok_count += 1
            else:
                check_count += 1

    data["fetch_summary"] = {
        "success_count": ok_count,
        "check_needed_count": check_count,
    }

    print(f"미국시장 yfinance 데이터 수집 완료: 성공 {ok_count}개, 확인 필요 {check_count}개")
    return data


# =========================
# 관심종목
# =========================

def load_us_watchlist():
    """
    us_watchlist.txt 파일이 있으면 미국 관심 종목을 읽습니다.
    형식 예:
    NVDA,NVIDIA
    AAPL,Apple
    SMH,VanEck Semiconductor ETF
    """
    path = "us_watchlist.txt"

    if not os.path.exists(path):
        print("us_watchlist.txt 파일이 없습니다. 미국 관심 종목 추가 수집은 건너뜁니다.")
        return {}

    result = {}

    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    for line in lines:
        raw = line.strip()

        if not raw or raw.startswith("#"):
            continue

        if "," in raw:
            symbol, name = raw.split(",", 1)
            symbol = symbol.strip().upper()
            name = name.strip()
        else:
            symbol = raw.strip().upper()
            name = symbol

        if not symbol:
            continue

        result[symbol.lower().replace("-", "_").replace(".", "_")] = {
            "symbol": symbol,
            "name": name,
        }

    print(f"미국 관심 종목 {len(result)}개 로드 완료")
    return result


# =========================
# 뉴스 수집
# =========================

def fetch_newsapi():
    if not NEWSAPI_KEY:
        print("NEWSAPI_KEY가 없습니다. NewsAPI 수집을 건너뜁니다.")
        return []

    query = (
        "US stock market OR Wall Street OR S&P 500 OR Nasdaq OR Dow OR Russell 2000 OR "
        "Federal Reserve OR inflation OR Treasury yields OR dollar index OR oil OR "
        "Nvidia OR AI OR semiconductor OR earnings OR options market"
    )

    url = "https://newsapi.org/v2/everything"

    params = {
        "q": query,
        "from": since_utc(36).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 70,
        "apiKey": NEWSAPI_KEY,
    }

    print("NewsAPI 미국시장 뉴스 수집 시작")
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

    print(f"NewsAPI 미국시장 뉴스 {len(result)}개 수집 완료")
    return result


def fetch_alpha_vantage_news():
    if not ALPHAVANTAGE_KEY:
        print("ALPHAVANTAGE_KEY가 없습니다. Alpha Vantage 뉴스 수집을 건너뜁니다.")
        return []

    url = "https://www.alphavantage.co/query"

    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "financial_markets,economy_monetary,economy_macro,technology,earnings",
        "time_from": alpha_time_from(72),
        "sort": "LATEST",
        "limit": 70,
        "apikey": ALPHAVANTAGE_KEY,
    }

    print("Alpha Vantage 미국시장 뉴스 수집 시작")
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

        for ticker_item in item.get("ticker_sentiment", [])[:15]:
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

    print(f"Alpha Vantage 미국시장 뉴스 {len(result)}개 수집 완료")
    return result


# =========================
# 데이터 패킷
# =========================

def build_data_quality_summary(market_data, newsapi_articles, alpha_articles):
    quote_success = market_data.get("fetch_summary", {}).get("success_count", 0)
    quote_check = market_data.get("fetch_summary", {}).get("check_needed_count", 0)

    if quote_success >= 30 and quote_check <= 5:
        price_status = "제공"
    elif quote_success >= 15:
        price_status = "일부 제공"
    else:
        price_status = "부족"

    news_count = len(newsapi_articles) + len(alpha_articles)

    if news_count >= 30:
        news_status = "제공"
    elif news_count >= 10:
        news_status = "일부 제공"
    else:
        news_status = "부족"

    return {
        "price_and_index_data": price_status,
        "rates_fx_commodities_data": "일부 제공",
        "breadth_data": "부족",
        "options_volatility_data": "일부 제공 - VIX 계열만 제공, Put/Call·GEX는 미제공",
        "etf_flow_data": "부족 - ETF 가격·거래량만 제공",
        "news_event_data": news_status,
        "overall_confidence": "보통" if price_status != "부족" and news_status != "부족" else "낮음",
        "missing_items": [
            "Advance/Decline",
            "신고가/신저가",
            "Put/Call Ratio",
            "Max Pain",
            "Gamma Exposure",
            "ETF Fund Flow",
            "CPI/PPI/PCE 최신 공식 수치 자동 연결",
            "경제·실적 일정 자동 연결",
        ],
    }


def build_data_packet():
    market_data = build_market_data()
    newsapi_articles = fetch_newsapi()
    alpha_articles = fetch_alpha_vantage_news()

    articles = dedupe_items(newsapi_articles + alpha_articles, ["title", "url"])

    data_quality = build_data_quality_summary(
        market_data=market_data,
        newsapi_articles=newsapi_articles,
        alpha_articles=alpha_articles,
    )

    data_packet = {
        "system_name": "미국 증시 분석 자동화 시스템",
        "report_name": "미국 증시 데일리 리포트 v4.0",
        "generated_at_kst": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
        "generated_at_utc": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "market_data": market_data,
        "data_quality": data_quality,
        "article_count": len(articles),
        "newsapi_article_count": len(newsapi_articles),
        "alpha_vantage_article_count": len(alpha_articles),
        "articles": articles[:120],
        "instruction": (
            "제공된 DATA에 없는 숫자와 고급 옵션/수급 데이터는 임의 생성하지 말고 확인 필요로 표시하세요. "
            "미국 증시 기관투자자급 데일리 리포트를 한국어로 작성하세요."
        ),
    }

    print(f"NewsAPI 기사 수: {len(newsapi_articles)}개")
    print(f"Alpha Vantage 기사 수: {len(alpha_articles)}개")
    print(f"최종 기사 수: {len(articles)}개")
    print(f"데이터 신뢰도: {data_quality.get('overall_confidence')}")

    return data_packet


# =========================
# OpenAI 보고서 생성
# =========================

def load_prompt():
    if not os.path.exists(US_PROMPT_PATH):
        raise FileNotFoundError(
            f"{US_PROMPT_PATH} 파일이 없습니다. "
            "GitHub 저장소의 prompts 폴더에 us_market_v4.md 파일을 추가해 주세요."
        )

    with open(US_PROMPT_PATH, "r", encoding="utf-8") as file:
        return file.read()


def generate_report(data_packet):
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 없습니다.")

    prompt = load_prompt()

    user_input = f"""
오늘 날짜: {now_kst().strftime('%Y-%m-%d %H:%M KST')}

아래 DATA는 현재 자동 수집 가능한 미국시장 지수, 종목, ETF, 금리, 달러, 원자재, 변동성, 뉴스 데이터입니다.

중요:
- DATA에 없는 수치, 옵션 데이터, 시장 폭 데이터, ETF Flow 데이터는 임의로 만들지 마세요.
- 불명확한 항목은 반드시 "확인 필요"라고 표시하세요.
- 투자 권유가 아니라 참고용 분석 자료로 작성하세요.
- Top Pick이 아니라 조건부 관찰 후보로 작성하세요.
- 한국어 Markdown 형식으로 작성하세요.

DATA:
{json.dumps(data_packet, ensure_ascii=False, indent=2)}
"""

    print("OpenAI 미국증시 보고서 생성 시작")

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=prompt,
        input=user_input,
        max_output_tokens=7000,
    )

    report = response.output_text

    if not report or not report.strip():
        raise ValueError("OpenAI 응답이 비어 있습니다.")

    print("OpenAI 미국증시 보고서 생성 완료")
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

    subject = f"[자동] 미국 증시 데일리 리포트 - {now_kst().strftime('%Y-%m-%d %H:%M KST')}"

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

def extract_first_lines(report, max_lines=18):
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


def build_slack_summary(report, data_packet, notion_url=None):
    lines = extract_first_lines(report, max_lines=18)

    market_data = data_packet.get("market_data", {})
    fetch_summary = market_data.get("fetch_summary", {})
    data_quality = data_packet.get("data_quality", {})

    message = "🇺🇸 *미국 증시 데일리 리포트 생성 완료*\n\n"

    message += "*요약 미리보기*\n"
    for line in lines:
        cleaned = line.replace("#", "").replace("**", "").strip()
        if cleaned:
            message += f"• {cleaned}\n"

    message += "\n*수집 데이터*\n"
    message += f"• 시세 데이터 성공: {fetch_summary.get('success_count', 0)}개\n"
    message += f"• 시세 확인 필요: {fetch_summary.get('check_needed_count', 0)}개\n"
    message += f"• NewsAPI: {data_packet.get('newsapi_article_count', 0)}개\n"
    message += f"• Alpha Vantage: {data_packet.get('alpha_vantage_article_count', 0)}개\n"
    message += f"• 최종 뉴스 기사: {data_packet.get('article_count', 0)}개\n"
    message += f"• 전체 판단 신뢰도: {data_quality.get('overall_confidence', '확인 필요')}\n"

    message += "\n*전체 보고서*\n"

    if notion_url:
        message += f"• Notion에서 보기: {notion_url}\n"
    else:
        message += "• Notion 링크 없음. 이메일 또는 Notion 페이지를 직접 확인하세요.\n"

    message += "\n_이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다._"

    return truncate_slack_message(message, max_chars=9000)


def send_slack(report, data_packet, notion_url=None):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 없어서 Slack 발송을 건너뜁니다.")
        return

    slack_text = build_slack_summary(
        report=report,
        data_packet=data_packet,
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


def send_notion(report, data_packet):
    if not NOTION_TOKEN:
        print("NOTION_TOKEN이 없어서 Notion 저장을 건너뜁니다.")
        return None

    if not NOTION_PARENT_PAGE_ID:
        print("NOTION_PARENT_PAGE_ID가 없어서 Notion 저장을 건너뜁니다.")
        return None

    created_at = now_kst()
    title = f"미국 증시 데일리 리포트 - {created_at.strftime('%Y-%m-%d %H:%M KST')}"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    market_data = data_packet.get("market_data", {})
    fetch_summary = market_data.get("fetch_summary", {})
    data_quality = data_packet.get("data_quality", {})

    intro_blocks = [
        make_text_block("heading_1", title),
        make_text_block("paragraph", f"생성 시각: {created_at.strftime('%Y-%m-%d %H:%M:%S KST')}"),
        make_text_block("paragraph", "미국 증시 분석 자동화 시스템에서 생성한 보고서입니다."),
        make_text_block("paragraph", "주의: 이 보고서는 투자 권유가 아니라 참고용 분석 자료입니다."),
        make_divider_block(),
        make_text_block("heading_2", "수집 데이터 요약"),
        make_text_block("bulleted_list_item", f"시세 데이터 성공: {fetch_summary.get('success_count', 0)}개"),
        make_text_block("bulleted_list_item", f"시세 확인 필요: {fetch_summary.get('check_needed_count', 0)}개"),
        make_text_block("bulleted_list_item", f"NewsAPI 기사: {data_packet.get('newsapi_article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"Alpha Vantage 기사: {data_packet.get('alpha_vantage_article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"최종 뉴스 기사: {data_packet.get('article_count', 0)}개"),
        make_text_block("bulleted_list_item", f"전체 판단 신뢰도: {data_quality.get('overall_confidence', '확인 필요')}"),
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
    print("미국 증시 분석 자동화 시스템 시작")

    data_packet = build_data_packet()

    report = generate_report(data_packet)

    if not report:
        raise ValueError("OpenAI 보고서 생성 결과가 비어 있습니다.")

    send_email(report)

    notion_url = send_notion(report, data_packet)

    send_slack(
        report=report,
        data_packet=data_packet,
        notion_url=notion_url,
    )

    print("미국 증시 분석 자동화 시스템 완료")


if __name__ == "__main__":
    main()
