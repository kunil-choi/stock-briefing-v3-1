# collectors/market_collector.py
"""
시장 지표 수집기

수정 이력:
- FIX-MKT-1  : 반환 딕셔너리 키를 html_generator._INDICATOR_DEFS와 일치하도록 통일
- FIX-MKT-2  : FinanceDataReader 의존 제거, yfinance 우선 / 네이버 폴백
- FIX-MKT-3  : collect_market_overview() 함수 내 들여쓰기 버그 수정
- FIX-MKT-4  : KOSPI/KOSDAQ도 yfinance 우선으로 변경
- FIX-MKT-12 : _fetch_yf period→start/end 명시 + 수집 날짜 로그 출력 (날짜 오류 추적용)
- FIX-MKT-5  : 장 시작 전(09:00 KST 이전)에는 전일 종가 + "전일종가" 라벨 표시
- FIX-MKT-6  : _is_premarket() 주말(토·일) 처리 추가
- FIX-MKT-7  : 나스닥/S&P500/다우존스/달러원은 is_premarket=False 고정
"""

import re
import math
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _is_premarket() -> bool:
    """현재 시각이 장 시작(09:00 KST) 이전인지 확인. 토·일은 항상 True."""
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return True
    return now.hour < 9


def _make_indicator(value, change_pct, direction: str = "",
                    is_premarket: bool = False) -> dict:
    try:
        pct_num = float(change_pct) if change_pct is not None else 0.0
    except (TypeError, ValueError):
        pct_num = 0.0
    if not direction:
        direction = "up" if pct_num > 0 else "down" if pct_num < 0 else "flat"
    return {
        "value":        value,
        "change_pct":   pct_num,
        "direction":    direction,
        "is_premarket": is_premarket,
    }


def _pct(current, previous) -> float:
    try:
        c, p = float(current), float(previous)
        if p == 0:
            return 0.0
        return round((c - p) / p * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ── yfinance 기반 조회 ────────────────────────────────────────────────────────

def _expected_last_trading_date(ref: datetime) -> str:
    """
    ref 시각(KST) 기준으로 "가장 최근에 마감했어야 할 KRX 거래일"을 반환.
    - 09:00 이전 → 어제부터 역산
    - 09:00 이후 → 오늘부터 역산
    - 토·일이면 그 이전 평일(금요일 등)까지 거슬러 올라감
    반환 형식: "YYYY-MM-DD"
    """
    candidate = ref.date() if ref.hour >= 9 else (ref - timedelta(days=1)).date()
    # 공휴일은 별도 처리하지 않고 토·일만 건너뜀
    while candidate.weekday() >= 5:   # 5=토, 6=일
        candidate -= timedelta(days=1)
    return candidate.strftime("%Y-%m-%d")


def _fetch_yf(ticker: str, is_krx: bool = False):
    """
    FIX-MKT-11: yfinance가 당일 미개장(특히 미국장 개장 전) 구간에
    NaN 종가를 포함한 placeholder 행을 반환하는 경우가 있음.
    NaN을 그대로 통과시키면 화면에 "nan"으로 표출되므로,
    NaN인 경우 수집 실패(None, None)로 명확히 처리한다.

    FIX-MKT-12: period="5d" 대신 start/end 날짜를 명시적으로 지정하여
    yfinance가 잘못된 날짜를 반환하는 버그를 방지한다.
    수집된 날짜를 로그로 출력해 오류 추적이 가능하도록 한다.

    is_krx=True: KRX 지수(KOSPI/KOSDAQ)용. Yahoo의 3rd-party 피드 지연으로
    최신 봉 날짜가 _expected_last_trading_date와 다르면 실패 처리 → 네이버 폴백.
    """
    if not _YF_AVAILABLE:
        return None, None
    try:
        # 오늘 KST 날짜 기준으로 최근 10 캘린더일 범위를 명시 지정
        # (주말/공휴일 고려해 여유 있게 10일 요청)
        now_kst    = datetime.now(KST)
        end_date   = (now_kst + timedelta(days=1)).strftime("%Y-%m-%d")   # 내일 (exclusive)
        start_date = (now_kst - timedelta(days=10)).strftime("%Y-%m-%d")  # 10일 전

        tk   = yf.Ticker(ticker)
        hist = tk.history(start=start_date, end=end_date)
        if hist.empty or len(hist) < 2:
            print(f"  [yfinance] {ticker} 데이터 부족 (행수={len(hist)})")
            return None, None

        # 주말 행 제외: 날짜 문자열(YYYY-MM-DD) 기준으로 weekday 판단 (timezone 무관)
        import pandas as pd
        valid_hist = hist[hist.index.map(lambda x: pd.Timestamp(str(x)[:10]).weekday() < 5)]
        if len(valid_hist) < 2:
            print(f"  [yfinance] {ticker} 유효 거래일 데이터 부족 (행수={len(valid_hist)})")
            return None, None

        # 날짜 인덱스를 문자열로 변환해 로그 출력 (날짜 오류 추적용)
        try:
            dates = [str(d)[:10] for d in valid_hist.index]
            latest_date_str = dates[-1]
            print(f"  [yfinance] {ticker} 수집 날짜: {dates[-2]} → {latest_date_str}")
        except Exception:
            latest_date_str = ""

        # KRX 지수: 최신 봉이 기대 거래일과 정확히 일치해야 함
        # 하루라도 오래됐으면 Yahoo 피드 지연으로 판단 → 실패 처리
        if is_krx and latest_date_str:
            expected = _expected_last_trading_date(now_kst)
            if latest_date_str < expected:
                print(f"  [yfinance] {ticker} KRX 피드 지연 "
                      f"(최신봉={latest_date_str}, 기대={expected}) → 네이버 폴백")
                return None, None

        close_prev = float(valid_hist["Close"].iloc[-2])
        close_now  = float(valid_hist["Close"].iloc[-1])
        if math.isnan(close_prev) or math.isnan(close_now):
            print(f"  [yfinance] {ticker} NaN 감지 → 수집 실패 처리")
            return None, None
        return close_now, _pct(close_now, close_prev)
    except Exception as e:
        print(f"  [yfinance] {ticker} 조회 실패: {e}")
        return None, None


# ── 네이버 금융 폴백 ──────────────────────────────────────────────────────────

def _fetch_naver_index(symbol: str):
    if not _REQUESTS_AVAILABLE:
        return None, None
    url = f"https://finance.naver.com/sise/sise_index.naver?code={symbol}"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        text = resp.text

        m_val = re.search(r'id="now_value"[^>]*>([\d,.]+)', text)
        m_pct = re.search(r'id="change_percent"[^>]*>([\d.]+)', text)
        m_dir = re.search(r'class="(up\d*|down\d*|dn\d*|no\d*)"', text)

        if not m_val:
            return None, None
        value     = float(m_val.group(1).replace(",", ""))
        pct       = float(m_pct.group(1)) if m_pct else 0.0
        raw_dir   = (m_dir.group(1) if m_dir else "").lower()
        direction = ("up"   if "up"   in raw_dir else
                     "down" if ("down" in raw_dir or "dn" in raw_dir) else
                     "flat")
        if direction == "down":
            pct = -abs(pct)
        return value, pct
    except Exception as e:
        print(f"  [Naver] {symbol} 조회 실패: {e}")
        return None, None


def _fetch_naver_index_day_over_day(symbol: str):
    """
    네이버 차트 API에서 일봉 데이터를 2개 가져와 전일 대비 등락률을 직접 계산.
    _fetch_naver_index()의 페이지 파싱보다 안정적인 대안.
    반환: (현재가, 등락률%) 또는 (None, None)
    """
    if not _REQUESTS_AVAILABLE:
        return None, None
    url = (
        f"https://fchart.stock.naver.com/sise.nhn"
        f"?symbol={symbol}&timeframe=day&count=5&requestType=0"
    )
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
        resp.raise_for_status()
        # XML: <item data="20240101|open|high|low|close|volume"/>
        items = re.findall(r'<item data="([^"]+)"', resp.text)
        if len(items) < 2:
            print(f"  [Naver차트] {symbol} 데이터 부족 ({len(items)}개)")
            return None, None
        prev_close = float(items[-2].split("|")[4])
        now_close  = float(items[-1].split("|")[4])
        if prev_close == 0:
            return None, None
        pct = round((now_close - prev_close) / prev_close * 100, 2)
        print(f"  [Naver차트] {symbol}: {prev_close} → {now_close} ({pct:+.2f}%)")
        return now_close, pct
    except Exception as e:
        print(f"  [Naver차트] {symbol} 조회 실패: {e}")
        return None, None


def _fetch_naver_forex():
    if not _REQUESTS_AVAILABLE:
        return None, None
    url = "https://finance.naver.com/marketindex/"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        text = resp.text

        m_val = re.search(
            r'USD.*?<span[^>]+class="[^"]*value[^"]*"[^>]*>([\d,.]+)</span>',
            text, re.DOTALL
        )
        if not m_val:
            m_val = re.search(r'\b(1[,.]?\d{3}[.,]\d{2})\b', text)
        if not m_val:
            return None, None

        value = float(m_val.group(1).replace(",", ""))
        if not (900 < value < 2000):
            return None, None

        m_pct = re.search(
            r'USD.*?<span[^>]+class="[^"]*rate[^"]*"[^>]*>.*?([\d.]+)%',
            text, re.DOTALL
        )
        pct = float(m_pct.group(1)) if m_pct else 0.0

        m_dir   = re.search(r'USD.*?class="(up\d*|dn\d*|down\d*)"', text, re.DOTALL)
        raw_dir = (m_dir.group(1) if m_dir else "").lower()
        if "dn" in raw_dir or "down" in raw_dir:
            pct = -abs(pct)

        return value, pct
    except Exception as e:
        print(f"  [Naver] USD/KRW 조회 실패: {e}")
        return None, None


# ── 공개 API ──────────────────────────────────────────────────────────────────

def collect_market_overview() -> dict:
    """
    시장 지표 수집.
    FIX-MKT-10: 모든 지표를 항상 result에 포함 (수집 실패 시 value=None).
    """
    print("\n[시장수집] 지표 수집 시작...")
    result    = {}
    premarket = _is_premarket()

    if premarket:
        print("  [장전/주말] 전일 종가 기준으로 표시")

    def _set(key: str, label: str, val, pct, is_pre: bool = False) -> None:
        """FIX-MKT-10: 성공/실패 모두 result[key]를 채운다 (실패 시 value=None)."""
        if val is not None:
            result[key] = _make_indicator(val, pct, is_premarket=is_pre)
            suffix = " [전일종가]" if is_pre else ""
            print(f"  {label}: {val:,.2f} ({pct:+.2f}%){suffix}")
        else:
            result[key] = _make_indicator(None, 0.0, direction="flat", is_premarket=is_pre)
            print(f"  {label}: 데이터 없음")

    # ── KOSPI ─────────────────────────────────────────────────────────────────
    # is_krx=True: 피드 지연(날짜 오래됨) 감지 시 None 반환 → 네이버 폴백
    val, pct = _fetch_yf("^KS11", is_krx=True)
    if val is None:
        print("  [KOSPI] yfinance 실패/지연 → 네이버 차트 폴백")
        val, pct = _fetch_naver_index_day_over_day("KOSPI")
    if val is None:
        val, pct = _fetch_naver_index("KOSPI")
    _set("kospi", "KOSPI", val, pct, premarket)

    # ── KOSDAQ ────────────────────────────────────────────────────────────────
    val, pct = _fetch_yf("^KQ11", is_krx=True)
    if val is None:
        print("  [KOSDAQ] yfinance 실패/지연 → 네이버 차트 폴백")
        val, pct = _fetch_naver_index_day_over_day("KOSDAQ")
    if val is None:
        val, pct = _fetch_naver_index("KOSDAQ")
    _set("kosdaq", "KOSDAQ", val, pct, premarket)

    # ── NASDAQ ────────────────────────────────────────────────────────────────
    val, pct = _fetch_yf("^IXIC")
    _set("nasdaq", "NASDAQ", val, pct, False)

    # ── S&P 500 ───────────────────────────────────────────────────────────────
    val, pct = _fetch_yf("^GSPC")
    _set("sp500", "S&P500", val, pct, False)

    # ── 다우존스 ──────────────────────────────────────────────────────────────
    val, pct = _fetch_yf("^DJI")
    _set("dow", "DOW", val, pct, False)

    # ── USD/KRW ───────────────────────────────────────────────────────────────
    val, pct = _fetch_yf("KRW=X")
    if val is None:
        val, pct = _fetch_naver_forex()
    _set("usd_krw", "USD/KRW", val, pct, False)

    available = sum(1 for v in result.values() if v.get("value") is not None)
    print(f"[시장수집] 완료 (전체 {len(result)}개 지표 / 실제 수집 {available}개)")

    return result

