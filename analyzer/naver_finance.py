# analyzer/naver_finance.py
# FIX-PRICE-1: HTML 파싱 → Naver JSON API 우선, sise_day 폴백
# FIX-PRICE-2: 주가 단위 오류 방지 (원 단위 정수 반환)
# FIX-SISE-1 : sise_day 정규식 그룹 인덱스 오류 수정
#              (m[2]전일비 스킵 → m[3]시가 올바르게 매핑)
# FIX-PRICE-5: 한국 주식시장 프리마켓 없음 반영
#              09:00 이전 → Naver API 반환값 = 전일 종가
#              price_label 결정은 ai_analyzer(호출부)에서 담당
#              이 함수는 가격 값만 정확하게 반환
# FIX-PRICE-6: API closePrice=0 또는 누락 시 추가 키 탐색 강화
#              prevClosePrice, stockEndPrice 순으로 폴백
#              prevClosePrice 폴백 시 change/change_pct는 0으로 강제
#              (의미 혼동 방지 — 어제 종가에 오늘 등락률 붙이지 않음)
# FIX-API-2  : Naver Stock API 응답 구조 변화 대응
#              stockPrice 중첩 객체 내 키도 탐색
# KRX-LOGIN-WALL-1: KRX가 2025-12-27부로 로그인 필수(회원제)로 전환돼
#              data.krx.co.kr 직접 조회가 막힌 것을 대체하기 위해
#              fetch_naver_stock_list()/fetch_naver_full_stock_map() 추가
#              (전체 상장 종목명→코드 매핑을 네이버 시가총액 페이지에서 조회)
# FIX-SISE-4 : 이 파이프라인은 항상 장 시작(09:00 KST) 전에 실행되는데,
#              네이버 sise_day가 가끔 "오늘(아직 미체결)" 행을 전일 종가와
#              동일한 값으로 미리 얹어 최상단(daily[0])에 내려준다. 이 행이
#              그대로 daily[0]으로 잡히면 "오늘(미확정, 전일과 동일값)" vs
#              "전일 종가"를 비교하게 되어 등락률이 0.00%로 고정된다
#              (2026-08-11/08-12 실행에서 관심종목 10개 전부 등락률이
#              0.00%로 노출된 사고 — 대형·고변동성 종목까지 전부 동시에
#              정확히 0.00%인 건 실제 시황일 수 없어 이 파싱 문제로 진단됨).
#              최상단 행의 날짜가 오늘(KST)이면 그 행을 버리고 다음 행부터
#              쓰도록 fetch_naver_daily_prices()에 가드를 추가했다. 그래도
#              0.00%가 나오는 경우를 대비해 원인 추적용 raw 행 로그도 남긴다.

import re
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}


def _get(url: str, timeout: int = 10) -> str:
    """공통 HTTP GET 헬퍼. 실패 시 빈 문자열 반환."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[naver_finance] GET 실패 {url}: {e}")
        return ""


def _get_euckr(url: str, timeout: int = 10) -> str:
    """공통 HTTP GET 헬퍼(EUC-KR 디코딩). 네이버 금융의 구버전 페이지들은
    EUC-KR 인코딩을 쓰므로, UTF-8로 디코딩하면 한글이 깨진다."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("euc-kr", errors="ignore")
    except Exception as e:
        print(f"[naver_finance] GET 실패 {url}: {e}")
        return ""


def _get_json(url: str, timeout: int = 10):
    """JSON GET 헬퍼. 실패 시 None 반환."""
    raw = _get(url, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_int(value) -> int:
    """
    콤마·공백·부호 문자를 제거하고 정수로 변환.
    변환 실패 시 0 반환.
    """
    try:
        return int(
            str(value).replace(",", "").replace(" ", "").replace("+", "")
        )
    except (ValueError, TypeError):
        return 0


def _parse_float(value) -> float:
    """
    콤마·공백·%·부호 문자를 제거하고 float으로 변환.
    변환 실패 시 0.0 반환.
    """
    try:
        return float(
            str(value).replace("%", "").replace("+", "")
                      .replace(",", "").replace(" ", "")
        )
    except (ValueError, TypeError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 종목 코드 조회
# ─────────────────────────────────────────────────────────────────────────────

def search_code_by_autocomplete(stock_name: str) -> dict:
    """자동완성 API로 종목명 → 코드 변환. 실패 시 None 반환."""
    enc = urllib.parse.quote(stock_name)
    url = (
        f"https://ac.finance.naver.com/ac?"
        f"q={enc}&q_enc=UTF-8&st=111&sug=all&frm=stock"
    )
    raw = _get(url)
    try:
        data  = json.loads(raw)
        items = data.get("items", [[]])[0]
        for item in items:
            # item 형식: [name, code, ...]
            if len(item) >= 2:
                code = str(item[1])
                if re.match(r"^\d{6}$", code):
                    return {"name": item[0], "code": code}
    except Exception:
        pass
    return None


def verify_stock_via_naver(stock_name: str) -> dict:
    result = search_code_by_autocomplete(stock_name)
    if result:
        return {"verified": True, "code": result["code"], "name": result["name"]}
    return {"verified": False, "code": "", "name": stock_name}


# ─────────────────────────────────────────────────────────────────────────────
# 전체 종목 목록 조회 (KRX 로그인 벽 우회)
# ─────────────────────────────────────────────────────────────────────────────
# KRX-LOGIN-WALL-1: KRX 정보데이터시스템이 2025-12-27부로 로그인 필수(회원제)로
# 전환되면서, 익명 요청으로 전체 상장 종목 목록을 가져오던 기존 방식
# (data.krx.co.kr/comm/bldAttendant/getJsonData.cmd)이 막혔다 — 로그인 세션이
# 없으면 JSON 대신 오류 페이지가 와서 "Expecting value" 파싱 실패로 이어졌다.
# pykrx 등 기존에 이 엔드포인트에 의존하던 라이브러리들도 동일한 문제를 겪고
# 있어(관련 이슈 확인), KRX 도메인 자체를 계속 써서는 근본 해결이 안 된다.
# 같은 종목명→코드 매핑을 KRX가 아닌 네이버 금융 "시가총액" 페이지(로그인
# 불필요, 이 모듈이 가격 조회에도 이미 쓰고 있는 소스)에서 페이지네이션으로
# 긁어와 대체한다.

_MARKET_CAP_URL    = "https://finance.naver.com/sise/sise_market_sum.naver"
_MARKET_CAP_ROW_RE = re.compile(
    r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>'
)
_MAX_MARKET_CAP_PAGES = 60  # 안전장치: 페이지당 최대 50종목 × 60페이지 = 3,000종목까지


def fetch_naver_stock_list(sosok: int) -> dict:
    """
    네이버 금융 시가총액 페이지를 페이지네이션으로 순회해 종목명→코드
    매핑을 만든다. sosok=0: 코스피, sosok=1: 코스닥.

    새로 들어오는 종목이 없는 페이지를 만나면(=마지막 페이지를 지났음)
    중단한다 — 페이지네이션 위젯을 별도로 파싱하지 않아도 되게 하는
    단순하고 견고한 종료 조건. 안전장치로 _MAX_MARKET_CAP_PAGES를
    넘지 않는다. 실패해도 예외를 던지지 않고 지금까지 모은 값(빈 dict
    포함)을 반환한다 — 호출부(ai_analyzer.load_stock_names)가 fallback
    처리를 담당.
    """
    stock_map = {}
    page = 1
    while page <= _MAX_MARKET_CAP_PAGES:
        url  = f"{_MARKET_CAP_URL}?sosok={sosok}&page={page}"
        html = _get_euckr(url)
        if not html:
            break

        matches = _MARKET_CAP_ROW_RE.findall(html)
        if not matches:
            break

        new_count = 0
        for code, name in matches:
            name = name.strip()
            if name and name not in stock_map:
                stock_map[name] = code
                new_count += 1

        if new_count == 0:
            break
        page += 1

    return stock_map


def fetch_naver_full_stock_map() -> dict:
    """코스피 + 코스닥 전체 종목명→코드 매핑. 한쪽이 실패해도 나머지는 반환."""
    kospi  = fetch_naver_stock_list(0)
    kosdaq = fetch_naver_stock_list(1)
    print(f"[naver_finance] 전체 종목 목록: 코스피 {len(kospi)}개 + 코스닥 {len(kosdaq)}개")
    return {**kospi, **kosdaq}


# ─────────────────────────────────────────────────────────────────────────────
# API 응답 파싱 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _extract_price_from_api(data: dict) -> tuple:
    """
    FIX-PRICE-6 / FIX-API-2:
    Naver Stock API JSON에서 price, change, change_pct를 추출한다.

    탐색 순서 (price):
      1. data["closePrice"]           → 정규장 현재가 / 장 전 전일종가
      2. data["stockPrice"]["closePrice"]
      3. data["stockEndPrice"]        → FIX-API-2: 구조 변화 대응
      4. data["stockPrice"]["stockEndPrice"]
      5. data["prevClosePrice"]       → FIX-PRICE-6: 위 모두 0일 때 폴백
      6. data["stockPrice"]["prevClosePrice"]

    탐색 순서 (change / change_pct):
      - 1~4번 경로로 price를 얻은 경우만 조회
      - prevClosePrice 폴백 시에는 change=0, change_pct=0.0 강제
        (어제 종가에 오늘 등락률을 붙이는 의미 혼동 방지)

    반환: (price: int, change: int, change_pct: float)
    price == 0 이면 조회 실패로 간주.
    """
    sp = data.get("stockPrice", {}) or {}

    # ── 1단계: closePrice / stockEndPrice 우선 탐색 ──────────────────────
    price         = 0
    used_fallback = False

    for key in ("closePrice", "stockEndPrice"):
        raw = data.get(key) or sp.get(key)
        if raw:
            price = _parse_int(raw)
            if price > 0:
                break

    # ── 2단계: 위 모두 0이면 prevClosePrice 폴백 ─────────────────────────
    if price == 0:
        for key in ("prevClosePrice",):
            raw = data.get(key) or sp.get(key)
            if raw:
                price = _parse_int(raw)
                if price > 0:
                    used_fallback = True
                    break

    # ── change / change_pct ───────────────────────────────────────────────
    # prevClosePrice 폴백 시에는 등락 정보가 의미 없으므로 0으로 강제
    change     = 0
    change_pct = 0.0

    if not used_fallback and price > 0:
        for key in ("compareToPreviousClosePrice",):
            raw = data.get(key) or sp.get(key)
            if raw is not None:
                change = _parse_int(raw)
                break
        for key in ("fluctuationsRatio",):
            raw = data.get(key) or sp.get(key)
            if raw is not None:
                change_pct = _parse_float(raw)
                break

    return price, change, change_pct


# ─────────────────────────────────────────────────────────────────────────────
# 현재가 조회
# ─────────────────────────────────────────────────────────────────────────────

def fetch_naver_stock_price(stock_name: str, code_override: str = "") -> dict:
    """
    전일 종가 + 전전일 대비 변동폭을 반환한다.

    우선순위:
      1) m.stock.naver.com/api/stock/{code}/basic  (모바일 JSON API)
         closePrice(전일 종가) + fluctuationsRatio(전전일 대비 등락률)
      2) sise_day 최근 2일치 종가로 직접 계산

    반환:
      {"name": str, "code": str, "price": int,
       "change": int, "change_pct": float, "url": str}
      실패 시 None.
    """
    # 1. 코드 확보
    code = code_override.strip()
    if not code:
        result = search_code_by_autocomplete(stock_name)
        if not result:
            print(f"[naver_finance] 코드 조회 실패: {stock_name}")
            return None
        code       = result["code"]
        stock_name = result.get("name", stock_name)

    naver_url = f"https://finance.naver.com/item/main.naver?code={code}"

    # [1순위] 모바일 API
    api_url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    data    = _get_json(api_url)

    if data:
        try:
            price, change, change_pct = _extract_price_from_api(data)
            if price > 0:
                # change_pct가 0.0이면 sise_day로 재계산 (월요일 등 주말 직후 API 이슈 대응)
                if change_pct == 0.0:
                    print(f"[naver_finance] {stock_name}: 모바일 API change_pct=0 → sise_day 재계산")
                    daily = fetch_naver_daily_prices(code, days=5)
                    if daily and len(daily) >= 2 and daily[1]["close"] > 0:
                        sise_change = round((daily[0]["close"] - daily[1]["close"]) / daily[1]["close"] * 100, 2)
                        if sise_change != 0.0:
                            change_pct = sise_change
                            change = daily[0]["close"] - daily[1]["close"]
                        else:
                            # FIX-SISE-4: 정말 보합인지, 아직 원인 불명인 파싱
                            # 문제인지 다음에도 판단할 수 있게 사용된 행을 남긴다.
                            print(
                                f"[naver_finance] {stock_name}: sise_day 재계산 결과도 0.00% "
                                f"(daily[0]={daily[0]['date']} {daily[0]['close']:,}원 / "
                                f"daily[1]={daily[1]['date']} {daily[1]['close']:,}원)"
                            )
                    else:
                        print(f"[naver_finance] {stock_name}: sise_day 재계산 실패 → 등락률 0.00%로 유지됨")
                print(
                    f"[naver_finance] {stock_name}({code}): "
                    f"{price:,}원 ({change_pct:+.2f}%) [전일종가]"
                )
                return {
                    "name":       stock_name,
                    "code":       code,
                    "price":      price,
                    "change":     change,
                    "change_pct": change_pct,
                    "url":        naver_url,
                }
        except Exception as e:
            print(f"[naver_finance] 모바일 API 파싱 오류 ({stock_name}): {e}")

    # [2순위] sise_day 최근 5일치 종가로 직접 계산 (주말 건너뛴 전거래일 확보)
    print(f"[naver_finance] {stock_name}: 모바일 API 실패 → sise_day 폴백")
    daily = fetch_naver_daily_prices(code, days=5)
    if daily:
        price = daily[0].get("close", 0)
        if price > 0:
            prev_price = daily[1].get("close", 0) if len(daily) >= 2 else 0
            change     = price - prev_price if prev_price > 0 else 0
            change_pct = round(change / prev_price * 100, 2) if prev_price > 0 else 0.0
            print(
                f"[naver_finance] {stock_name}({code}): "
                f"{price:,}원 ({change_pct:+.2f}%) [전일종가-sise]"
            )
            return {
                "name":       stock_name,
                "code":       code,
                "price":      price,
                "change":     change,
                "change_pct": change_pct,
                "url":        naver_url,
            }

    print(f"[naver_finance] 현재가 조회 최종 실패: {stock_name}({code})")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 기업 정보 / 일별 시세
# ─────────────────────────────────────────────────────────────────────────────

def fetch_naver_company_info(code: str) -> dict:
    """섹터 및 동종업종 상위 5개 기업명 반환."""
    url  = f"https://finance.naver.com/item/main.naver?code={code}"
    html = _get(url)
    sector = ""
    peers  = []
    try:
        m = re.search(r'업종</th>\s*<td[^>]*>([^<]+)', html)
        if m:
            sector = m.group(1).strip()
        peers = re.findall(r'<a[^>]+etf_compare[^>]*>([^<]+)</a>', html)[:5]
    except Exception:
        pass
    return {"sector": sector, "peers": peers}


_ROW_RE        = re.compile(r'<tr[^>]*>((?:(?!</tr>).)*?)</tr>', re.DOTALL)
_CELL_RE       = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_TAG_RE        = re.compile(r'<[^>]+>')
_NUM_RE        = re.compile(r'(\d[\d,]*)')
_DATE_RE       = re.compile(r'(\d{4}\.\d{2}\.\d{2})')


def _parse_sise_day_html(html: str, days: int) -> list:
    """html 문자열에서 sise_day 일별 OHLCV 행을 파싱한다."""
    rows = []
    for row_match in _ROW_RE.finditer(html):
        if len(rows) >= days:
            break
        row_html = row_match.group(1)
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 7:
            continue

        date_m = _DATE_RE.search(_TAG_RE.sub("", cells[0]))
        if not date_m:
            continue

        # 컬럼: [0]날짜 [1]종가 [2]전일비 [3]시가 [4]고가 [5]저가 [6]거래량
        # 태그를 먼저 제거한 뒤 숫자를 찾는다 — class="tah p11" 같은 속성에도
        # 숫자가 섞여 있어 태그를 남긴 채로 찾으면 엉뚱한 값을 집을 수 있다.
        nums = []
        for cell in cells[1:7]:
            num_m = _NUM_RE.search(_TAG_RE.sub("", cell))
            if not num_m:
                nums = None
                break
            nums.append(int(num_m.group(1).replace(",", "")))
        if nums is None:
            continue

        rows.append({
            "date":   date_m.group(1),
            "close":  nums[0],
            "open":   nums[2],
            "high":   nums[3],
            "low":    nums[4],
            "volume": nums[5],
        })
    return rows


def fetch_naver_daily_prices(code: str, days: int = 14, retries: int = 1) -> list:
    """
    sise_day에서 일별 OHLCV 데이터 반환 (최신순).

    네이버 sise_day 컬럼 순서: 날짜 / 종가 / 전일비 / 시가 / 고가 / 저가 / 거래량

    FIX-SISE-2: 이전 구현은 <td> 바로 뒤에 숫자가 온다고 가정했으나(예:
    r'<td[^>]*>\s*([\d,]+)\s*</td>'), 실제 페이지는 각 셀 값을
    <span class="tah p11">296,000</span> 처럼 <span>으로 감싸 렌더링해
    매 행이 매칭에 실패했다. 그 결과 fetch_naver_stock_price()의
    "API change_pct=0 → sise_day 재계산" 폴백도 항상 빈 리스트를 받아
    등락률이 계속 0.0%로 표시되는 문제가 있었다.
    <tr> 단위로 행을 분리한 뒤, 각 <td>...</td> 셀 내부에서(중첩 태그와
    무관하게) 첫 숫자 토큰만 추출하는 방식으로 견고하게 재작성한다.

    FIX-SISE-3: 2026-08-11 실행에서 관심종목 10개 전부 sise_day 재계산이
    빈 리스트를 반환해 등락률이 하루 종일 0.00%로 노출된 사고가 있었다.
    원인 진단을 위해 실패 시 (html 길이 / 매칭된 행 수)를 로그로 남기고,
    빈 응답(네이버 측 일시적 차단·타임아웃 추정)에 대해 짧은 대기 후
    1회 재시도한다.

    FIX-SISE-4: 위 재시도로도 행 개수 자체는 정상(2개 이상)인데 등락률이
    여전히 0.00%로 고정되는 새로운 사고가 있었다(2026-08-11/08-12 —
    관심종목 전부, 대형·고변동성 종목까지 동시에 정확히 0.00%). 이 파이프라인은
    항상 장 시작(09:00 KST) 전에 실행되므로, 네이버가 아직 미체결인
    "오늘" 행을 전일 종가와 동일한 값으로 최상단에 미리 얹어 보내면
    daily[0](오늘, 미확정) vs daily[1](전일 종가) 비교가 항상 0이 된다.
    최상단 행의 날짜가 오늘(KST)이면 그 행을 버리고 다음 행부터 쓴다.
    """
    url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page=1"
    today_str = datetime.now(KST).strftime("%Y.%m.%d")

    for attempt in range(retries + 1):
        html = _get(url)
        rows = []
        try:
            rows = _parse_sise_day_html(html, days)
        except Exception as e:
            print(f"[naver_finance] sise_day 파싱 오류 ({code}): {e}")

        if rows and rows[0]["date"] == today_str:
            print(
                f"[naver_finance] sise_day 오늘({today_str}) 미확정 행 제외 "
                f"({code}): {rows[0]['close']:,}원"
            )
            rows = rows[1:]

        if len(rows) >= 2:
            return rows

        print(
            f"[naver_finance] sise_day 데이터 부족 ({code}, 시도 {attempt + 1}/{retries + 1}): "
            f"html {len(html)}자, 파싱된 행 {len(rows)}개"
        )
        if attempt < retries:
            time.sleep(1.5)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 캔들차트 생성
# ─────────────────────────────────────────────────────────────────────────────

def generate_candlestick_base64(daily_prices: list, stock_name: str = "") -> str:
    """캔들차트 PNG → base64 문자열. 실패 시 None 반환."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import base64
        from io import BytesIO
    except ImportError:
        return None

    if not daily_prices or len(daily_prices) < 2:
        return None

    try:
        prices = list(reversed(daily_prices))  # 오래된 날짜 → 최신 순
        fig, ax = plt.subplots(figsize=(8, 4))
        fig.patch.set_facecolor("#1e1e2e")
        ax.set_facecolor("#1e1e2e")

        for i, row in enumerate(prices):
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            color = "#ef5350" if c >= o else "#26a69a"
            ax.plot([i, i], [l, h], color=color, linewidth=1)
            ax.add_patch(mpatches.FancyBboxPatch(
                (i - 0.3, min(o, c)), 0.6, abs(c - o),
                boxstyle="square,pad=0", color=color
            ))

        # 날짜 레이블 (최대 5개)
        step = max(1, len(prices) // 5)
        ax.set_xticks(range(0, len(prices), step))
        ax.set_xticklabels(
            [prices[i]["date"][5:] for i in range(0, len(prices), step)],
            color="#aaaaaa", fontsize=8
        )
        ax.tick_params(colors="#aaaaaa")
        for spine in ax.spines.values():
            spine.set_edgecolor("#444444")
        ax.set_title(stock_name, color="#ffffff", fontsize=10)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()
    except Exception as e:
        print(f"[naver_finance] 캔들차트 생성 오류: {e}")
        return None
