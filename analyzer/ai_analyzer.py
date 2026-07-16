# analyzer/ai_analyzer.py
"""
AI 주식 브리핑 분석 엔진

수정 이력:
- BUG-CR-1         : 상대 임포트로 전환
- BUG-HIDDEN       : 히든픽 로직 정비
- BUG-CACHE        : stock_map 캐시 키 우선순위 정비
- BUG-KEY-1        : ai_strategy 키 통일
- V2-PROMPT        : Claude 프롬프트를 V2 수준으로 개선
- V2-SYNC          : channel_mentions → reasons 동기화 블록 추가
- FIX-ANA-2        : generate_market_summary 데드코드 제거
- FIX-PRICE-LABEL-2: _get_price_label 08:00~08:59 구간 "현재가" 오표시 수정 → 09:00 이전 모두 "전일종가"
- FIX-PARA-1       : market_summary 5단락→4단락, "투자 포인트"+"리스크 요인"을 "핵심 포인트"로 통합
- FIX-API-1        : Claude API 호출 실패 시 fallback HTML 반환
- FIX-STRAT        : ai_strategy 구조화 JSON 객체 전환
- FIX-MAX-1        : 관심종목 최대 10개로 확대
- FIX-STRAT-2      : ai_strategy dict → HTML 렌더링 전 문자열 변환 버그 수정
- FIX-FILTER-1     : 관심종목 단계별 선정 로직
- FIX-SIG-4        : 프롬프트 signal 지시를 긍정/중립/부정으로 통일
- FIX-PROMPT-1     : rules 문자열 끝 손상 복구 및 stock_plans 규칙 명시
- FIX-APIKEY-1     : call_claude_with_retry에 api_key 전달 누락 버그 수정
- FIX-PRICE-1      : 관심종목 현재가 조회 후 Claude 프롬프트에 포함
- FIX-PRICE-3      : 프롬프트 price_str에 등락률 병기 추가
- FIX-FILTER-2     : 3차 필터 총 3회↑ → 총 2회↑로 완화
- FIX-ACC-1        : 근거 없는 수치 프롬프트 및 렌더링에서 완전 제거
- FIX-PRICE-4      : stock_prices를 result["stocks"]에 병합
- FIX-STOCK-COUNT-1: 프롬프트로 넘긴 종목 전체를 반드시 포함하도록 강제
- GEMINI-VAL-1     : Gemini 검수 파이프라인 연결
- TIER-FILTER-1    : 소스 티어 시스템 도입
- FIX-PRICE-5      : 한국 주식시장 프리마켓 없음 반영
- FIX-HIDDEN-PRICE : 히든픽 주가 별도 조회 추가
- FIX-OPINION-1    : extract_mentions()에 best_opinion 필드 추가
- FIX-GEMINI-IMPORT: GEMINI_API_KEY ImportError 방어
- FIX-SEEN-IDS     : extract_mentions() 중복 체크를 list → set으로 교체
- FIX-EMPTY-FILTER : filter_mentions() 결과 0개일 때 방어 로그 추가
- FIX-MAIN-1       : analyze_and_generate_html() gh_token 인자 수신 정상화
- FIX-JSON-NAME-1  : Claude 응답 JSON stocks/hidden_picks name 필드 정규화
                     "name" 키 외에 "stock_name","종목명","ticker" 폴백 탐색
                     name 공백 시 mention_lookup에서 복원 시도
- FIX-JSON-PARSE-1 : _try_parse_json() 비탐욕적 패턴 → 탐욕적 패턴으로 수정
                     중첩 JSON 전체가 올바르게 파싱되도록 {.*} 사용
- FIX-PRICE-LABEL-1: is_open_market 조건 명확화
                     15:30 이후 → "종가", 09:00~15:30 → "현재가",
                     09:00 이전 → "전일종가"
"""

import json
import os
import re
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

from .api_client import call_claude_with_retry

KST               = timezone(timedelta(hours=9))
STOCK_CACHE_FILE  = "data/stock_names_cache.json"
OUTPUT_FILE       = "data/briefing_data.json"
CB                = "```"

_SKIP_NAMES = {
    "삼성", "현대", "LG", "SK", "롯데", "한화", "포스코", "GS", "CJ",
    "KT", "LS", "DB", "OCI", "KG", "SG", "TG", "NH", "KB",
    "AI", "IT", "EV", "US", "EU", "UN", "M", "A", "S", "K",
    "전자", "화학", "건설", "증권", "은행", "보험", "자동차", "철강",
    "에너지", "바이오", "게임", "반도체", "배터리", "인터넷", "소프트웨어",
    "기업", "그룹", "홀딩스", "코리아", "코퍼레이션",
    "금리", "환율", "달러", "원화", "코스피", "코스닥", "나스닥",
    "매수", "매도", "상승", "하락", "급등", "급락",
    "시장", "투자", "주식", "펀드", "ETF", "채권", "선물", "옵션",
    "경제", "금융", "부동산", "인플레이션", "디플레이션",
    "중국", "미국", "유럽", "일본", "한국",
}
_MIN_NAME_LEN        = 2
_HIGH_QUALITY_TYPES  = {"애널리스트", "경제방송TV", "경제방송", "증권사"}

# ── TIER-FILTER-1: 소스 티어 정의 ────────────────────────────────────────
_SOURCE_TIER = {
    "애널리스트":  4.0,
    "증권사":      3.0,
    "경제방송TV":  2.0,
    "경제방송":    2.0,
    "유튜브":      2.0,
    "뉴스":        1.0,
}

# 긍정적 매수 의견 키워드 (소문자 통일)
_POSITIVE_OPINION_KEYWORDS = {
    "매수", "buy", "강력매수", "strong buy", "비중확대", "overweight",
    "outperform", "시장수익률상회", "적극매수",
}

# 긍정 감성 키워드
_POSITIVE_SENTIMENT = {
    "상승", "급등", "목표가 상향", "실적 개선", "호실적", "매출 증가",
    "수혜", "기대", "성장", "돌파", "신고가", "강세", "반등", "회복",
    "증가", "확대", "호조", "긍정", "추천", "주목", "주도주",
}

# 부정 감성 키워드
_NEGATIVE_SENTIMENT = {
    "하락", "급락", "목표가 하향", "실적 부진", "매출 감소", "리스크",
    "우려", "불확실", "약세", "매도", "하향", "감소", "위험", "부정",
}

_NEWS_TYPES = {"뉴스"}


# ── 유효성 검사 헬퍼 ─────────────────────────────────────────────────────

def _is_valid_stock_name(name: str) -> bool:
    if len(name) < _MIN_NAME_LEN:
        return False
    if name in _SKIP_NAMES:
        return False
    if re.match(r'^[A-Z]{2,3}$', name):
        return False
    return True


# ── 채널 가중치 계산 ──────────────────────────────────────────────────────

def _channel_weight(subscribers: int) -> float:
    if not subscribers or subscribers <= 0:
        return 0.5
    base = math.log10(max(subscribers, 10000)) - math.log10(100000)
    return min(1.0 + max(0.0, base), 3.0)


def _build_channel_weight_map(channels_data: dict) -> dict:
    weight_map = {}
    if not channels_data:
        return weight_map
    for section in ["broadcast", "youtuber", "securities"]:
        for ch in channels_data.get(section, []):
            name = ch.get("name", "")
            subs = ch.get("subscribers", 0)
            if name:
                weight_map[name] = _channel_weight(subs)
    return weight_map


# ── TIER-FILTER-1: 감성 가중치 계산 ─────────────────────────────────────

def _get_sentiment_weight(text: str, opinion: str = "") -> float:
    """
    텍스트와 애널리스트 의견을 기반으로 감성 가중치를 반환한다.
    긍정+부정 동시 존재 시 중립(1.0) — 부정 표현 앞 긍정 키워드 오분류 방지.
    """
    opinion_lower = opinion.strip().lower()
    if any(k in opinion_lower for k in _POSITIVE_OPINION_KEYWORDS):
        return 2.0

    text_lower   = text.lower()
    has_positive = any(k in text_lower for k in _POSITIVE_SENTIMENT)
    has_negative = any(k in text_lower for k in _NEGATIVE_SENTIMENT)

    if has_positive and has_negative:
        return 1.0
    if has_positive:
        return 1.5
    if has_negative:
        return 0.5
    return 1.0


# ── FIX-JSON-NAME-1: Claude 응답 JSON 정규화 헬퍼 ───────────────────────

def _normalize_stock_item(item: dict, mention_lookup: dict = None) -> dict:
    """
    Claude가 반환한 stocks/hidden_picks 항목의 name 필드를 정규화한다.
    탐색 순서: "name" → "stock_name" → "종목명" → "ticker"
    모두 없으면 mention_lookup 키 순서로 추정 (rank 기반).
    """
    name = (
        item.get("name")
        or item.get("stock_name")
        or item.get("종목명")
        or item.get("ticker")
        or ""
    )
    if name:
        item["name"] = name.strip()
    return item


def _normalize_result_stocks(result: dict, mention_lookup: dict) -> dict:
    """
    result["stocks"]와 result["hidden_picks"]의 name 필드를 일괄 정규화.
    name이 비어있으면 rank 순서로 mention_lookup에서 복원 시도.
    """
    mention_names = list(mention_lookup.keys())

    for i, stock in enumerate(result.get("stocks", [])):
        _normalize_stock_item(stock, mention_lookup)
        # name이 여전히 비어있으면 mention_lookup 순서로 복원
        if not stock.get("name") and i < len(mention_names):
            stock["name"] = mention_names[i]
            print(f"[정규화] stocks[{i}] name 복원: {mention_names[i]}")

    for hp in result.get("hidden_picks", []):
        _normalize_stock_item(hp, mention_lookup)

    return result


# ── 종목 이름 로드 ───────────────────────────────────────────────────────

def load_stock_names() -> dict:
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")

    if os.path.exists(STOCK_CACHE_FILE):
        try:
            with open(STOCK_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cached_map = cache.get("stock_map") or cache.get("stocks", {})
            if cache.get("date") == today_kst and cached_map:
                print(f"[종목캐시] {len(cached_map)}개 로드 (캐시)")
                return cached_map
        except Exception:
            pass

    stock_map = {}
    try:
        import requests
        for market_id in ["STK", "KSQ"]:
            url     = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
            payload = {
                "bld":         "dbms/MDC/STAT/standard/MDCSTAT01901",
                "mktId":       market_id,
                "share":       "1",
                "csvxls_isNo": "false",
            }
            headers = {"Referer": "http://data.krx.co.kr/"}
            resp    = requests.post(url, data=payload, headers=headers, timeout=10)
            data    = resp.json()
            for item in data.get("OutBlock_1", []):
                name = item.get("ISU_ABBRV", "").strip()
                code = item.get("ISU_SRT_CD", "").strip()
                if name and code:
                    stock_map[name] = code
        if stock_map:
            os.makedirs("data", exist_ok=True)
            with open(STOCK_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"date": today_kst, "stock_map": stock_map, "stocks": stock_map},
                    f, ensure_ascii=False,
                )
            print(f"[종목로드] KRX에서 {len(stock_map)}개 로드")
            return stock_map
    except Exception as e:
        print(f"[종목로드] KRX 요청 실패: {e}, fallback 사용")

    stock_map = {
        "삼성전자": "005930", "SK하이닉스": "000660", "LG에너지솔루션": "373220",
        "삼성바이오로직스": "207940", "현대차": "005380", "NAVER": "035420",
        "카카오": "035720", "셀트리온": "068270", "삼성SDI": "006400",
        "LG전자": "051910", "KB금융": "105560", "신한지주": "055550",
        "하나금융지주": "086790", "현대모비스": "012330", "LG화학": "066570",
        "삼성물산": "028260", "SK텔레콤": "017670", "롯데케미칼": "011170",
        "CJ제일제당": "097950", "한화솔루션": "009830", "삼성생명": "032830",
        "SK이노베이션": "096770", "KT": "030200", "한화에어로스페이스": "012450",
        "HMM": "011200", "현대글로비스": "015760", "삼성전기": "009150",
        "LG디스플레이": "034220", "현대제철": "004020",
        "HD현대": "329180", "두산에너빌리티": "034020",
    }
    print(f"[종목로드] fallback {len(stock_map)}개 사용")
    return stock_map


# ── 언급 추출 ────────────────────────────────────────────────────────────

def extract_mentions(all_data: list, stock_map: dict,
                     channels_data: dict = None) -> dict:
    """
    TIER-FILTER-1 + FIX-OPINION-1 + FIX-SEEN-IDS:
    - non_news_channel_types, max_tier, best_opinion 필드 추가
    - 감성 가중치 반영
    - 중복 체크를 set으로 교체 (성능 개선)
    """
    weight_map = _build_channel_weight_map(channels_data) if channels_data else {}

    type_map = {
        "뉴스":        "뉴스",
        "경제방송":    "경제방송",
        "경제방송TV":  "경제방송TV",
        "유튜브":      "유튜브",
        "증권사":      "증권사",
        "애널리스트":  "애널리스트",
    }
    default_weights = {
        "뉴스": 1.5, "경제방송": 1.8, "경제방송TV": 1.8,
        "애널리스트": 2.5, "유튜브": 1.0, "증권사": 2.0,
    }

    mentions = {}

    for item in all_data:
        raw_type = item.get("source_type", "유튜브")
        ch_type  = type_map.get(raw_type, "유튜브")
        src_name = item.get("source_name", "")
        title    = item.get("title", "")
        summary  = item.get("summary", "") or item.get("content", "")
        link     = item.get("link", "") or item.get("url", "")
        opinion  = item.get("opinion", "")
        text     = f"{title} {summary}"
        weight   = weight_map.get(src_name, default_weights.get(ch_type, 1.0))

        # GEMINI-VAL-1: confidence 낮음이면 가중치 절반
        if item.get("_from_gemini") and item.get("gemini_confidence") == "낮음":
            weight *= 0.5

        # TIER-FILTER-1: 감성 가중치 적용
        sentiment_w = _get_sentiment_weight(text, opinion)
        weight      = weight * sentiment_w

        for name, code in stock_map.items():
            if not _is_valid_stock_name(name):
                continue

            gemini_stock = item.get("stock_name", "")
            if gemini_stock:
                if name != gemini_stock:
                    continue
            else:
                if name not in text:
                    continue

            content_id = f"{src_name}_{link}_{name}"

            if name not in mentions:
                mentions[name] = {
                    "code":                   code,
                    "total_count":            0,
                    "weighted_score":         0.0,
                    "channel_types":          set(),
                    "non_news_channel_types": set(),
                    "max_tier":               0.0,
                    "channels":               {},
                    "best_opinion":           "",
                    "seen_ids":               set(),
                }

            entry = mentions[name]

            if content_id in entry["seen_ids"]:
                continue
            entry["seen_ids"].add(content_id)

            entry["channel_types"].add(ch_type)

            if ch_type not in _NEWS_TYPES:
                entry["non_news_channel_types"].add(ch_type)

            tier = _SOURCE_TIER.get(ch_type, 1.0)
            if tier > entry["max_tier"]:
                entry["max_tier"] = tier

            # FIX-OPINION-1: 애널리스트 매수 의견 저장 (매수 > 기타 우선)
            if ch_type == "애널리스트" and opinion:
                existing_op = entry.get("best_opinion", "")
                if any(k in opinion.lower() for k in _POSITIVE_OPINION_KEYWORDS):
                    entry["best_opinion"] = opinion
                elif not existing_op:
                    entry["best_opinion"] = opinion

            if ch_type not in entry["channels"]:
                entry["channels"][ch_type] = []

            idx     = text.find(name)
            snippet = text[max(0, idx - 50): idx + 150].strip()

            if item.get("_from_gemini") and item.get("content"):
                snippet = item["content"][:200]

            # SPEAKER-ID-1: Gemini 영상 분석이 식별한 실제 발언자(호스트/게스트
            # 애널리스트 등)를 채널 항목에 실어둔다. 지금까지는 이 정보가
            # headlines_text(상위 60개 개요용)에만 "[화자 발언]" 접두어로
            # 잠깐 쓰이고, 정작 종목별 channel_mentions을 만드는 근거가 되는
            # 이 리스트에는 전혀 안 들어가서 Claude가 채널명 이상을 알 방법이
            # 없었다.
            speaker_name = (
                item.get("gemini_speaker", "") if item.get("_from_gemini") else ""
            )
            entry["channels"][ch_type].append({
                "source_name":  src_name,
                "speaker_name": speaker_name,
                "snippet":      snippet,
                "link":         link,
                "content_id":   content_id,
                "weight":       round(weight, 2),
            })
            entry["total_count"]    += 1
            entry["weighted_score"] += weight

    # set → list 변환 + seen_ids 제거
    for name in mentions:
        mentions[name]["channel_types"] = list(mentions[name]["channel_types"])
        mentions[name]["non_news_channel_types"] = list(
            mentions[name]["non_news_channel_types"]
        )
        mentions[name].pop("seen_ids", None)

    print(f"[언급추출] {len(mentions)}개 종목 발견")
    return mentions


# ── TIER-FILTER-1: 단계별 관심종목 필터링 ───────────────────────────────

def filter_mentions(mentions: dict, target: int = 10, min_target: int = 8) -> list:
    """
    티어 기반 필터링. 뉴스 단독 종목은 어떤 단계에서도 통과 불가.

    FIX-FILTER-3: 1~4차를 통과한 종목이 min_target(기본 8개)에 못 미치면
                  5차(비뉴스 채널 1종 이상 + 가중점수 순)로 target까지 보완.
                  → "관심종목이 3개밖에 안 나온다" 문제의 근본 원인이었던
                  과도하게 엄격한 1~4차 기준을 우회하지 않고, 마지막 단계로
                  점수 순 보완만 추가해 8~10개를 안정적으로 채운다.
    """
    all_sorted = sorted(
        mentions.items(),
        key=lambda x: x[1]["weighted_score"],
        reverse=True,
    )

    selected       = []
    selected_names = set()

    # 1차: 비뉴스 채널 타입 2종 이상
    for name, data in all_sorted:
        if len(selected) >= target:
            break
        if len(data.get("non_news_channel_types", [])) >= 2:
            selected.append((name, data))
            selected_names.add(name)
    print(f"[필터링] 1차(비뉴스 채널타입 2종↑): {len(selected)}개")

    # 2차: 고품질 소스(T3이상) + 가중점수 8.0 이상
    if len(selected) < target:
        for name, data in all_sorted:
            if len(selected) >= target:
                break
            if name in selected_names:
                continue
            non_news = data.get("non_news_channel_types", [])
            if (len(non_news) >= 1
                    and data.get("max_tier", 0.0) >= 3.0
                    and data.get("weighted_score", 0.0) >= 8.0):
                selected.append((name, data))
                selected_names.add(name)
        print(f"[필터링] 2차(T3↑ + 가중점수8↑) 추가 후: {len(selected)}개")

    # 3차: 비뉴스 채널 1종 이상 + 총 언급 3회 이상
    if len(selected) < target:
        for name, data in all_sorted:
            if len(selected) >= target:
                break
            if name in selected_names:
                continue
            if (len(data.get("non_news_channel_types", [])) >= 1
                    and data["total_count"] >= 3):
                selected.append((name, data))
                selected_names.add(name)
        print(f"[필터링] 3차(비뉴스1종↑ + 3회↑) 추가 후: {len(selected)}개")

    # 4차: 고품질 단독 채널 + 매수 의견 또는 높은 평균 가중치
    if len(selected) < target:
        for name, data in all_sorted:
            if len(selected) >= target:
                break
            if name in selected_names:
                continue
            non_news = data.get("non_news_channel_types", [])
            best_op  = data.get("best_opinion", "")
            if len(non_news) == 1 and data.get("max_tier", 0.0) >= 3.0:
                has_buy = (
                    any(k in best_op.lower() for k in _POSITIVE_OPINION_KEYWORDS)
                    if best_op else False
                )
                avg_w = data.get("weighted_score", 0.0) / max(data.get("total_count", 1), 1)
                if has_buy or avg_w >= 3.75:
                    selected.append((name, data))
                    selected_names.add(name)
        print(f"[필터링] 4차(고품질단독+매수의견) 추가 후: {len(selected)}개")

    # FIX-FILTER-3 — 5차: min_target 미달 시 비뉴스 1종 이상 + 점수 순으로 보완
    if len(selected) < min_target:
        before = len(selected)
        for name, data in all_sorted:
            if len(selected) >= target:
                break
            if name in selected_names:
                continue
            if len(data.get("non_news_channel_types", [])) >= 1:
                selected.append((name, data))
                selected_names.add(name)
        print(f"[필터링] 5차(보완, 비뉴스1종↑ 점수순) {before}개 → {len(selected)}개")

    if not selected:
        print("[필터링] ⚠️ 경고: 필터링 결과 0개 — 뉴스 전용 언급만 있거나 수집 데이터 부족")
    elif len(selected) < min_target:
        print(f"[필터링] ⚠️ 참고: 비뉴스 채널 언급 종목 풀 자체가 부족해 "
              f"{min_target}개 목표에 못 미침 (최종 {len(selected)}개) "
              f"— 데이터 수집(채널/패널리스트) 확대가 근본 해결책")
    print(f"[필터링] 최종 {len(selected)}개 선택")
    return selected



# ── TIER-FILTER-1: 히든픽 후보 추출 ────────────────────────────────────

def extract_hidden_picks(mentions: dict, filtered_names: set,
                         max_picks: int = 3) -> list:
    """
    non_news_channel_types가 정확히 1종인 고품질 단독 언급 종목만 선택.
    """
    candidates = []
    for name, data in mentions.items():
        if name in filtered_names:
            continue
        non_news = set(data.get("non_news_channel_types", []))
        if len(non_news) != 1:
            continue
        sole_type = list(non_news)[0]
        if sole_type not in _HIGH_QUALITY_TYPES:
            continue
        candidates.append({
            "name":           name,
            "code":           data["code"],
            "channel_type":   sole_type,
            "channels":       data["channels"],
            "weighted_score": round(data["weighted_score"], 2),
            "total_count":    data["total_count"],
        })

    candidates.sort(key=lambda x: x["weighted_score"], reverse=True)
    # 동일 채널 최대 1개 제한 (채널 다양성 보장)
    result = []
    used_channels = set()
    for c in candidates:
        ch = c["channel_type"]
        if ch in used_channels:
            continue
        result.append(c)
        used_channels.add(ch)
        if len(result) >= max_picks:
            break
    print(f"[히든픽] 후보 {len(candidates)}개 중 {len(result)}개 선택 (채널 다양성 적용)")
    return result


# ── FIX-ACC-1: 애널리스트 리포트 요약 맵 구성 ───────────────────────────

def _build_analyst_summary_map(all_data: list) -> dict:
    summary_map = {}
    for item in all_data:
        if item.get("source_type") != "애널리스트":
            continue
        stock_name = item.get("stock_name", "")
        if not stock_name:
            continue
        summary_map.setdefault(stock_name, []).append({
            "broker":       item.get("source_name", "").strip(),
            "opinion":      item.get("opinion", "").strip(),
            "target_price": item.get("target_price", "").strip(),
            "ai_summary":   item.get("ai_summary", "").strip(),
        })
    return summary_map


# ── Claude 프롬프트 생성 ─────────────────────────────────────────────────

def build_analysis_prompt(
    filtered_mentions: list,
    hidden_candidates: list,
    all_data: list,
    today_date: str,
    now_kst: str,
    stock_prices: dict = None,
    yesterday_date: str = "",
    day_before_date: str = "",
    market_leaders_raw: list = None,
) -> str:

    analyst_summary_map = _build_analyst_summary_map(all_data)

    headlines = []
    for item in all_data[:150]:
        title   = (item.get("title") or "").strip()
        stype   = item.get("source_type", "")
        src     = item.get("source_name", "")
        stock   = item.get("stock_name", "")
        url     = item.get("link") or item.get("url", "")
        summary = (item.get("summary") or item.get("content") or "")[:120]

        if item.get("_from_gemini") and item.get("gemini_speaker"):
            speaker = item["gemini_speaker"]
            title   = f"[{speaker} 발언] {title}" if title else f"[{speaker} 발언]"

        if title:
            line = f"[{stype}/{src}] {title}"
            if stock:
                line += f" (종목: {stock})"
            if summary:
                line += f" → {summary}"
            if url:
                line += f" [URL: {url}]"
            headlines.append(line)

    headlines_text = "\n".join(headlines[:60])

    top_stocks   = filtered_mentions[:15]
    stock_prices = stock_prices or {}
    stocks_info  = []
    stock_name_list = [name for name, _ in top_stocks]

    for rank, (name, data) in enumerate(top_stocks, 1):
        price_info = stock_prices.get(name)
        if price_info and isinstance(price_info, dict) and price_info.get("price", 0) > 0:
            price_label = price_info.get("price_label", "전일종가")
            price_str   = (
                f"{price_label}:{price_info['price']:,}원 "
                f"({price_info.get('change_pct', 0.0):+.2f}%)"
            )
        elif isinstance(price_info, int) and price_info > 0:
            price_str = f"전일종가:{price_info:,}원"
        else:
            price_str = "가격:미수집"

        non_news = data.get("non_news_channel_types", data.get("channel_types", []))
        stocks_info.append(
            f"{rank}. {name} (코드:{data['code']}, "
            f"언급:{data['total_count']}회, "
            f"가중점수:{data['weighted_score']:.1f}, "
            f"채널유형:{','.join(data['channel_types'])}, "
            f"비뉴스채널:{','.join(non_news) if non_news else '없음'}, "
            f"{price_str})"
        )

        if name in analyst_summary_map:
            for rpt in analyst_summary_map[name]:
                parts = []
                if rpt["broker"]:       parts.append(f"증권사:{rpt['broker']}")
                if rpt["opinion"]:      parts.append(f"의견:{rpt['opinion']}")
                if rpt["target_price"]: parts.append(f"목표가:{rpt['target_price']}원")
                if rpt["ai_summary"]:   parts.append(f"요약:{rpt['ai_summary']}")
                if parts:
                    stocks_info.append(f"   [애널리스트리포트] {' | '.join(parts)}")

        for ch_type, items in data["channels"].items():
            for it in items[:5]:
                w_str      = f"[가중치:{it.get('weight', 1.0):.1f}]"
                url_str    = f" [URL: {it['link']}]" if it.get("link") else ""
                speaker    = it.get("speaker_name", "")
                speaker_str = f" (발언자:{speaker})" if speaker else ""
                stocks_info.append(
                    f"   [{ch_type}]{w_str} {it['source_name']}{speaker_str}: "
                    f"{it['snippet'][:200]}{url_str}"
                )

    hidden_info = []
    for i, pick in enumerate(hidden_candidates, 1):
        ch_type = pick["channel_type"]
        hidden_info.append(
            f"{i}. {pick['name']} (코드:{pick['code']}, "
            f"채널:{ch_type}, 가중점수:{pick['weighted_score']:.1f})"
        )
        for it in pick["channels"].get(ch_type, [])[:3]:
            url_str = f" [URL: {it['link']}]" if it.get("link") else ""
            hidden_info.append(
                f"   [{ch_type}] {it['source_name']}: "
                f"{it['snippet'][:200]}{url_str}"
            )

    stocks_text = "\n".join(stocks_info)
    if hidden_info:
        hidden_text = "※ 아래 종목은 반드시 hidden_picks에 포함하세요\n" + "\n".join(hidden_info)
    else:
        hidden_text = "해당 없음 (hidden_picks는 빈 배열로)"
    stock_list_str = ", ".join(stock_name_list)

    prompt_json_structure = (
        '{\n'
        f'  "briefing_date": "{today_date}",\n'
        '  "market_summary": "최근 시장 흐름 정리 (4개 단락, \\n\\n 구분, 각 단락 3~4문장. 400자 이상. 단락순서: 1)최근흐름개요 2)주요이슈 3)핵심포인트(긍정부정 균형있게 통합서술) 4)전망(오늘 전망 데이터가 있는 경우에만 \'오늘\'표현 사용))",\n'
        '  "hot_sectors": [{"name": "섹터이름", "reason": "이유 1~2단어"}],\n'
        '  "market_leaders": [\n'
        '    {\n'
        '      "rank": 1,\n'
        '      "name": "종목명",\n'
        '      "code": "종목코드",\n'
        '      "signal": "긍정|중립|부정 중 택1",\n'
        '      "summary": "종목 핵심 요약 2~3문장",\n'
        '      "catalyst": "상승 촉매 2~3문장",\n'
        '      "risk": "주요 리스크 1~2문장",\n'
        '      "channel_mentions": [\n'
        '        {"source_type": "뉴스|경제방송|경제방송TV|유튜브|증권사|애널리스트 중 택1",\n'
        '         "source_name": "채널명", "speaker_name": "실제 발언자 이름+직함, 식별 안 되면 빈 문자열",\n'
        '         "content": "언급 내용 1~2문장",\n'
        '         "url": "URL 없으면 빈 문자열"}\n'
        '      ],\n'
        '      "channel_counts": {}, "total_count": 0,\n'
        '      "weighted_score": 0.0, "overlap_count": 0, "reasons": []\n'
        '    }\n'
        '  ],\n'
        '  "stocks": [\n'
        '    {\n'
        '      "rank": 1,\n'
        '      "name": "종목명",\n'
        '      "code": "종목코드",\n'
        '      "signal": "긍정|중립|부정 중 택1",\n'
        '      "summary": "종목 핵심 요약 2~3문장",\n'
        '      "catalyst": "상승 촉매 2~3문장",\n'
        '      "risk": "주요 리스크 1~2문장",\n'
        '      "channel_mentions": [\n'
        '        {"source_type": "뉴스|경제방송|경제방송TV|유튜브|증권사|애널리스트 중 택1",\n'
        '         "source_name": "채널명", "speaker_name": "실제 발언자 이름+직함, 식별 안 되면 빈 문자열",\n'
        '         "content": "언급 내용 1~2문장",\n'
        '         "url": "URL 없으면 빈 문자열"}\n'
        '      ],\n'
        '      "channel_counts": {}, "total_count": 0,\n'
        '      "weighted_score": 0.0, "overlap_count": 0, "reasons": []\n'
        '    }\n'
        '  ],\n'
        '  "hidden_picks": [\n'
        '    {\n'
        '      "rank": 1, "name": "종목명", "code": "종목코드",\n'
        '      "signal": "긍정|중립|부정 중 택1",\n'
        '      "summary": "종목 핵심 요약 2~3문장",\n'
        '      "catalyst": "상승 촉매 2~3문장",\n'
        '      "risk": "주요 리스크 1문장",\n'
        '      "channel_type": "애널리스트|경제방송TV|경제방송|증권사 중 택1",\n'
        '      "channel_name": "채널명",\n'
        '      "reasons": [\n'
        '        {"source_type": "채널유형", "source_name": "출처명",\n'
        '         "detail": "언급 내용 요약", "source_url": "URL 없으면 빈 문자열"}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "ai_strategy": {\n'
        '    "core_scenario": "핵심 시나리오 2~3문장",\n'
        '    "sector_rotation": "섹터 로테이션 방향 및 근거",\n'
        '    "watch_points": ["포인트1", "포인트2"],\n'
        '    "risk_scenarios": [\n'
        '      {"scenario": "시나리오명", "probability": "높음|보통|낮음",\n'
        '       "response": "대응 방향"}\n'
        '    ],\n'
        '    "analyst_consensus": "애널리스트 리포트 종합 시각"\n'
        '  }\n'
        '}'
    )

    # 대형 주도주 텍스트 구성
    market_leaders_raw = market_leaders_raw or []
    if market_leaders_raw:
        leader_info_lines = []
        for name, data in market_leaders_raw:
            code = data.get("code", "")
            score = round(data.get("weighted_score", 0.0), 1)
            leader_info_lines.append(f"- {name} (코드:{code}, 가중점수:{score})")
        leaders_text = "\n".join(leader_info_lines)
        leaders_rule = (
            f"0. market_leaders 배열에 아래 대형 주도주 {len(market_leaders_raw)}개를 반드시 포함.\n"
            f"   (오늘 가장 높은 언급 점수를 받은 시장 주도 대형주)\n"
            f"{leaders_text}\n"
        )
    else:
        leaders_rule = "0. market_leaders는 빈 배열 []로.\n"

    rules = (
        "[작성 규칙]\n"
        + leaders_rule +
        f"1. stocks 배열에 아래 종목 전체를 반드시 포함. 임의 제외 금지.\n"
        f"   필수 포함 종목({len(stock_name_list)}개): {stock_list_str}\n"
        "2. 각 stocks 항목의\"name\" 필드는 반드시 위 종목명을 그대로 사용.\n"
        "3. signal: 긍정|중립|부정 중 택1\n"
        "   - 애널리스트 매수 의견 있으면 → 긍정\n"
        "   - 매도/부정 의견 있으면 → 부정\n"
        "   - 그 외 → 중립\n"
        "4. channel_mentions: 실제 언급 채널/기사 내용 최대 4개. reasons는 빈 배열 [] 유지.\n"
        "   speaker_name: 소스 데이터 항목에 \"(발언자:이름)\" 표기가 있으면 그 이름을\n"
        "   그대로 옮기고, 가능하면 직함/소속도 함께 적으세요(예: \"김민수 im증권 연구원\").\n"
        "   \"(발언자:...)\" 표기가 없는 항목은 speaker_name을 빈 문자열로 두세요 —\n"
        "   채널명만으로 특정 인물을 추측해 지어내지 마세요.\n"
        "5. hidden_picks: [히든픽 후보] 목록에 종목이 있으면 반드시 포함. 후보 목록이 \"해당 없음\"일 때만 [].\n"
        "6. market_summary: 4단락(\\n\\n 구분), 각 단락 3~4문장, 400자 이상. 순서: 1)최근흐름개요(수집 데이터 기준 최근 24시간 내 흐름 서술, '오늘' 표현 지양) 2)주요이슈(최근 이슈 서술, '오늘' 표현 지양) 3)핵심포인트(긍정·부정 균형 서술, 리스크와 기회 모두 포함, '오늘' 표현 지양) 4)전망(오늘 개장 전망이 수집 데이터에 명시된 경우에만 '오늘' 사용, 없으면 '단기' 또는 '향후' 표현 사용).\n"
        "7. ai_strategy: 수집 데이터 기반으로만 작성. 임의 수치 생성 금지.\n"
        "8. URL은 출처 데이터에 있는 것만 사용. 없으면 빈 문자열.\n"
        "9. 순수 JSON만 출력. 설명문·마크다운 코드블록 제거.\n"
    )

    return (
        f"오늘 날짜: {today_date} ({now_kst} KST)\n"
        f"어제(전일): {yesterday_date} / 그제(전전일): {day_before_date}\n"
        f"※ market_summary는 '최근 시장 흐름' 관점으로 작성. 수집 데이터의 대부분이 24시간 이내 과거 데이터이므로 '오늘' 표현은 4)전망 단락에서 오늘 전망이 수집 데이터에 명시된 경우에만 사용. 날짜 표현('전일', '전전일' 등)은 오늘 날짜 기준으로 정확히 사용할 것.\n\n"
        f"[최근 주요 헤드라인]\n{headlines_text}\n\n"
        f"[관심종목 후보 (가중점수 순)]\n{stocks_text}\n\n"
        f"[히든픽 후보]\n{hidden_text}\n\n"
        "위 데이터를 바탕으로 아래 JSON 형식으로 오늘의 AI 주식 브리핑을 작성하세요.\n\n"
        f"{rules}\n\n"
        f"[출력 JSON 구조]\n{CB}json\n{prompt_json_structure}\n{CB}"
    )


# ── JSON 파싱 헬퍼 ──────────────────────────────────────────────────────

def _try_parse_json(text: str) -> Optional[dict]:
    """Claude 응답에서 JSON 파싱 — 3단계 fallback (FIX-JSON-PARSE-2)"""
    text = text.strip()

    # 1단계: 마크다운 코드블록 내 JSON 추출
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 2단계: 코드블록 제거 후 탐욕적 중괄호 탐지
    cleaned = re.sub(r'```(?:json)?\s*', '', text)
    cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE)
    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # 3단계: 전체 텍스트 직접 파싱 시도
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    return None


# ── ai_strategy dict → 문자열 변환 ─────────────────────────────────────

def _format_ai_strategy(strategy: dict) -> str:
    if not isinstance(strategy, dict):
        return str(strategy)

    lines = []

    core = strategy.get("core_scenario", "")
    if core:
        lines.append(f"■ 핵심 시나리오\n{core}")

    sector_rotation = strategy.get("sector_rotation", "")
    if sector_rotation:
        lines.append(f"■ 섹터 로테이션\n{sector_rotation}")

    watch_points = strategy.get("watch_points", [])
    if watch_points:
        wp_lines = ["■ 오늘의 주목 포인트"]
        for wp in watch_points:
            if wp:
                wp_lines.append(f"• {wp}")
        lines.append("\n".join(wp_lines))

    risk_scenarios = strategy.get("risk_scenarios", [])
    if risk_scenarios:
        risk_lines = ["■ 리스크 시나리오"]
        for r in risk_scenarios:
            scenario = r.get("scenario", "")
            prob     = r.get("probability", "")
            response = r.get("response", "")
            if scenario:
                risk_lines.append(f"• [{prob}] {scenario} → {response}")
        lines.append("\n".join(risk_lines))

    analyst_consensus = strategy.get("analyst_consensus", "")
    if analyst_consensus:
        lines.append(f"■ 애널리스트 종합 시각\n{analyst_consensus}")

    return "\n\n".join(lines)


# ── URL 복원 헬퍼 ───────────────────────────────────────────────────────

def _restore_source_url(item: dict, all_data: list) -> None:
    for field in ("channel_mentions", "reasons"):
        entries = item.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            url_key  = "url" if field == "channel_mentions" else "source_url"
            if entry.get(url_key):
                continue
            src_name = entry.get("source_name", "")
            if not src_name:
                continue
            for d in all_data:
                if d.get("source_name") == src_name:
                    link = d.get("link") or d.get("url", "")
                    if link:
                        entry[url_key] = link
                        break


# ── 증권사 리포트 요약 (영상 내레이션용) ─────────────────────────────────
# collectors/analyst_collector.py가 all_data에 태그해 둔 analyst_category
# (simultaneous/new_coverage/single_broker)를 그대로 재사용해, 영상 파이프라인이
# briefing_data.json 하나만 보고도 "동시언급/신규커버리지/유의미 단독언급"을
# 재구성할 수 있도록 정리해 저장한다. (기존에는 all_data에만 존재하고
# briefing_data.json에는 저장되지 않아, 영상 스크립트 생성 단계에서
# 증권사 리포트 내용이 통째로 유실되고 있었다.)

def build_brokerage_reports(all_data: list) -> dict:
    reports = [d for d in all_data if d.get("source_type") == "애널리스트"]

    def _pick(items: list) -> list:
        out = []
        for r in items:
            brokers = r.get("simultaneous_brokers") or [r.get("source_name", "")]
            out.append({
                "stock_name": r.get("stock_name", ""),
                "brokers":    [b for b in brokers if b],
                "title":      r.get("report_title") or r.get("title", ""),
                "opinion":    r.get("opinion", ""),
                "target_price": r.get("target_price", ""),
                "ai_summary": r.get("ai_summary", ""),
                "date":       r.get("date", ""),
                "report_day": r.get("report_day", "today"),
            })
        return out

    return {
        "simultaneous":  _pick([r for r in reports if r.get("analyst_category") == "simultaneous"]),
        "new_coverage":  _pick([r for r in reports if r.get("analyst_category") == "new_coverage"]),
        "single_significant": _pick([
            r for r in reports
            if r.get("analyst_category") == "single_broker" and r.get("significance_reason")
        ]),
    }


# ── fallback HTML ───────────────────────────────────────────────────────

def _fallback_html(error_msg: str, briefing_date: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 주식 브리핑 — {briefing_date}</title>
<style>
body {{ background:#0d1117; color:#e6edf3;
       font-family:'Malgun Gothic',sans-serif;
       display:flex; align-items:center; justify-content:center;
       min-height:100vh; margin:0; }}
.box {{ background:#161b22; border:1px solid #30363d;
        border-radius:12px; padding:2rem; max-width:500px; text-align:center; }}
h2  {{ color:#ff6b6b; margin-bottom:1rem; }}
p   {{ color:#8b949e; font-size:.9rem; }}
</style>
</head>
<body>
<div class="box">
  <h2>⚠️ 브리핑 생성 실패</h2>
  <p>{briefing_date}</p>
  <p style="margin-top:1rem;">{error_msg}</p>
</div>
</body>
</html>"""


# ── FIX-PRICE-LABEL-1: 주가 라벨 결정 함수 ─────────────────────────────

def _get_price_label(now: datetime) -> str:
    """
    한국 주식시장 시간대별 가격 라벨 반환.
      - 09:00 이전          → "전일종가" (장 미개장)
      - 09:00 ~ 15:29       → "현재가"
      - 15:30 이후 (장마감)  → "종가"
    주말(토·일)은 항상 "전일종가".

    FIX-PRICE-LABEL-2: 08:00~08:59 구간을 "현재가"로 표시하던 버그 수정.
      개별 주식 시세는 09:00 개장 전에 없으므로, 09:00 이전은 모두 "전일종가".
      (기존: 08:00 이후 → "현재가" → 브리핑 08:34 생성 시 "현재가" 오표시)
    """
    if now.weekday() >= 5:
        return "전일종가"
    h, m = now.hour, now.minute
    if h < 9:
        return "전일종가"
    if h < 15 or (h == 15 and m < 30):
        return "현재가"
    return "종가"


# ── 메인 워크플로우 ─────────────────────────────────────────────────────

def analyze_and_generate_html(
    all_data: list,
    channels_data: dict = None,
    gh_repo: str = "",
    gh_token: str = "",
    market_overview: dict = None,
) -> str:
    from .html_generator import generate_html
    from .naver_finance  import fetch_naver_stock_price

    # FIX-GEMINI-IMPORT: config.py에 정의되어 있으므로 단순 import + 환경변수 폴백
    from config import ANTHROPIC_API_KEY
    try:
        from config import GEMINI_API_KEY
    except ImportError:
        GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

    now_kst          = datetime.now(KST)
    today_date       = now_kst.strftime("%Y년 %m월 %d일")
    yesterday_date   = (now_kst - timedelta(days=1)).strftime("%Y년 %m월 %d일")
    day_before_date  = (now_kst - timedelta(days=2)).strftime("%Y년 %m월 %d일")
    now_kst_str      = now_kst.strftime("%H:%M")
    briefing_date    = today_date

    stock_map = load_stock_names()
    mentions  = extract_mentions(all_data, stock_map, channels_data)

    if not mentions:
        print("[분석] 언급 종목 없음 → fallback")
        return _fallback_html("수집된 종목 언급이 없습니다.", briefing_date)

    filtered       = filter_mentions(mentions)
    filtered_names = {name for name, _ in filtered}

    # ── 대형 주도주 분리: 가중점수 상위 2개를 market_leaders로 별도 처리 ──
    # 나머지에서 관심종목 10개를 선발해 다양성 확보
    if len(filtered) > 2:
        market_leaders_raw = filtered[:2]
        filtered           = filtered[2:]
        filtered_names     = {name for name, _ in filtered}
    else:
        market_leaders_raw = []
    print(f"[주도주] 대형 주도주: {[n for n,_ in market_leaders_raw]}")

    if not filtered:
        print("[분석] 관심종목 0개 — 히든픽 및 시장 요약만 생성")

    # ── FIX-PRICE-LABEL-1: 시각 기반 가격 라벨 결정 ──────────────────────
    price_label_default = _get_price_label(now_kst)
    print(f"[주가조회] 현재 시각 {now_kst_str} KST — 가격 라벨: {price_label_default}")

    # ── 관심종목 주가 조회 ─────────────────────────────────────────────────
    stock_prices = {}
    all_stocks_for_price = market_leaders_raw + filtered
    print(f"[주가조회] 관심종목 {len(all_stocks_for_price)}개 주가 조회 시작")
    for name, data in all_stocks_for_price:
        code = data.get("code", "")
        if not code:
            continue
        price_info = fetch_naver_stock_price(name, code_override=code)
        if not price_info or price_info.get("price", 0) <= 0:
            continue
        price_info["price_label"] = price_label_default
        stock_prices[name] = price_info
    print(f"[주가조회] 관심종목 {len(stock_prices)}/{len(filtered)}개 수집 완료")

    # ── FIX-HIDDEN-PRICE: 히든픽 주가 조회 ───────────────────────────────
    hidden_candidates = extract_hidden_picks(mentions, filtered_names)

    print(f"[주가조회] 히든픽 {len(hidden_candidates)}개 주가 조회 시작")
    for pick in hidden_candidates:
        name = pick["name"]
        code = pick["code"]
        if name in stock_prices:
            continue
        if not code:
            continue
        price_info = fetch_naver_stock_price(name, code_override=code)
        if not price_info or price_info.get("price", 0) <= 0:
            continue
        price_info["price_label"] = price_label_default
        stock_prices[name] = price_info
    print(f"[주가조회] 히든픽 주가 수집 완료 (전체 stock_prices: {len(stock_prices)}개)")

    prompt = build_analysis_prompt(
        filtered, hidden_candidates, all_data, today_date, now_kst_str,
        stock_prices=stock_prices,
        yesterday_date=yesterday_date,
        day_before_date=day_before_date,
        market_leaders_raw=market_leaders_raw,
    )
    print(f"[Claude] 프롬프트 길이: {len(prompt)}자")

    try:
        response_text = call_claude_with_retry(prompt, api_key=ANTHROPIC_API_KEY)
    except Exception as e:
        print(f"[Claude] API 호출 실패: {e}")
        return _fallback_html(f"Claude API 오류: {e}", briefing_date)

    result = _try_parse_json(response_text)
    if not result:
        # FIX-JSON-PARSE-3: 파싱 실패 시 fallback으로 바로 넘어가지 않고 한 번 더
        # 호출한다. max_tokens 잘림은 api_client의 자동 예산 확대로 대부분 해소되지만,
        # 그 외 JSON 형식 오류는 비결정적 샘플링 특성상 재시도로 종종 해결된다.
        print(
            f"[Claude] JSON 파싱 실패 (응답 길이: {len(response_text)}자) → 재시도\n"
            f"  응답 시작 200자: {response_text[:200]!r}\n"
            f"  응답 끝 200자: {response_text[-200:]!r}"
        )
        try:
            response_text = call_claude_with_retry(prompt, api_key=ANTHROPIC_API_KEY)
            result = _try_parse_json(response_text)
        except Exception as e:
            print(f"[Claude] 재시도 API 호출 실패: {e}")
            result = None

    if not result:
        print(
            f"[Claude] JSON 파싱 최종 실패 (응답 길이: {len(response_text)}자) → fallback\n"
            f"  응답 시작 200자: {response_text[:200]!r}\n"
            f"  응답 끝 200자: {response_text[-200:]!r}"
        )
        return _fallback_html("AI 응답 파싱 실패. 잠시 후 다시 시도하세요.", briefing_date)

    # ── FIX-JSON-NAME-1: name 필드 정규화 (종목명 공백 버그 수정) ─────────
    mention_lookup = dict(filtered)
    result = _normalize_result_stocks(result, mention_lookup)

    # ── GEMINI-VAL-1: 검수 파이프라인 ───────────────────────────────────
    if GEMINI_API_KEY:
        try:
            from .gemini_validator import run_full_validation
            result = run_full_validation(result, filtered, all_data, GEMINI_API_KEY)
        except Exception as e:
            print(f"[검수] 파이프라인 오류 (브리핑은 계속 진행): {e}")
    else:
        print("[검수] GEMINI_API_KEY 없음 → Gemini 검수 스킵")

    # ── ai_strategy dict → 문자열 변환 ───────────────────────────────────
    ai_strat = result.get("ai_strategy")
    if isinstance(ai_strat, dict):
        result["ai_strategy"] = _format_ai_strategy(ai_strat)

    # ── market_leaders 주가 병합 ─────────────────────────────────────────
    ml_lookup = dict(market_leaders_raw)
    for leader in result.get("market_leaders", []):
        name = leader.get("name", "")
        price_info = stock_prices.get(name)
        if price_info and isinstance(price_info, dict) and price_info.get("price", 0) > 0:
            leader["price"]       = price_info["price"]
            leader["change_pct"]  = price_info.get("change_pct", 0.0)
            leader["price_label"] = price_info.get("price_label", price_label_default)
        else:
            leader["price"]       = 0
            leader["change_pct"]  = 0.0
            leader["price_label"] = price_label_default
        if name in ml_lookup:
            data = ml_lookup[name]
            leader["channel_counts"] = {
                ch_type: len(items)
                for ch_type, items in data["channels"].items()
            }
            leader["total_count"]    = data["total_count"]
            leader["weighted_score"] = round(data["weighted_score"], 2)
            leader["overlap_count"]  = len(data["non_news_channel_types"])
        _restore_source_url(leader, all_data)

    # ── FIX-PRICE-4/5: result["stocks"]에 주가 병합 ──────────────────────
    for stock in result.get("stocks", []):
        name       = stock.get("name", "")
        price_info = stock_prices.get(name)

        if price_info and isinstance(price_info, dict) and price_info.get("price", 0) > 0:
            stock["price"]       = price_info["price"]
            stock["change_pct"]  = price_info.get("change_pct", 0.0)
            stock["price_label"] = price_info.get("price_label", price_label_default)
        else:
            stock["price"]       = 0
            stock["change_pct"]  = 0.0
            stock["price_label"] = price_label_default

        if name in mention_lookup:
            data = mention_lookup[name]
            stock["channel_counts"] = {
                ch_type: len(items)
                for ch_type, items in data["channels"].items()
            }
            stock["total_count"]    = data["total_count"]
            stock["weighted_score"] = round(data["weighted_score"], 2)
            stock["overlap_count"]  = len(data["non_news_channel_types"])
            stock["reasons"]        = []

    # ── 히든픽 주가 병합 ───────────────────────────────────────────────────
    hidden_lookup = {p["name"]: p for p in hidden_candidates}
    for hp in result.get("hidden_picks", []):
        name = hp.get("name", "")
        if name in hidden_lookup:
            hp["weighted_score"] = hidden_lookup[name]["weighted_score"]
            hp["channel_type"]   = hidden_lookup[name]["channel_type"]

        price_info = stock_prices.get(name)
        if price_info and isinstance(price_info, dict) and price_info.get("price", 0) > 0:
            hp["price"]       = price_info["price"]
            hp["change_pct"]  = price_info.get("change_pct", 0.0)
            hp["price_label"] = price_info.get("price_label", price_label_default)
        else:
            hp["price"]       = 0
            hp["change_pct"]  = 0.0
            hp["price_label"] = price_label_default

    # ── URL 복원 ───────────────────────────────────────────────────────────
    for stock in result.get("stocks", []):
        _restore_source_url(stock, all_data)
    for hp in result.get("hidden_picks", []):
        _restore_source_url(hp, all_data)

    # ── 결과 저장 ──────────────────────────────────────────────────────────
    if market_overview:
        result["market_data"] = market_overview
    result["brokerage_reports"] = build_brokerage_reports(all_data)
    os.makedirs("data", exist_ok=True)
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[저장] {OUTPUT_FILE} 저장 완료")
    except Exception as e:
        print(f"[저장] 실패: {e}")

    return generate_html(
        result, channels_data, gh_repo, gh_token, market_overview, all_data
    )

