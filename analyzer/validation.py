# analyzer/validation.py
"""
AI 주식 브리핑 검증 엔진

수정 이력:
- BUG-V-1  : 팩트체크 후 보호 필드 덮어쓰기 방지
- BUG-V-3  : reasons "reason" 필드 → "detail"로 매핑
- BUG-V-4  : 차트 데이터 부족 시 7일 → 3일 재시도
- BUG-V-11 : 보호 필드 복원 로직 강화
- FIX-PRICE-1: verified_price를 int/None으로 평탄화
- FIX-VAL-1: source_pool None 필터 처리 (NoneType iterable 오류 수정)
- FIX-VAL-2: _PROTECTED_FIELDS에 "signal" 추가 (팩트체크 후 signal 덮어쓰기 방지)
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from .api_client import call_claude_with_retry
from .naver_finance import (
    fetch_naver_company_info,
    fetch_naver_daily_prices,
    fetch_naver_stock_price,
    generate_candlestick_base64,
    search_code_by_autocomplete,
    verify_stock_via_naver,
)

CB          = "```"
_CACHE_PATH = "data/stock_names_cache.json"
_KST        = timezone(timedelta(hours=9))

FOREIGN_KEYWORDS = [
    "엔비디아", "테슬라", "애플", "마이크로소프트", "구글", "알파벳",
    "아마존", "메타", "넷플릭스", "AMD", "인텔", "퀄컴", "브로드컴",
    "NVIDIA", "Tesla", "Apple", "Microsoft", "Google", "Amazon",
    "Meta", "Netflix", "TSMC", "ASML", "ARM", "팔란티어",
    "마이크론", "코스트코", "월마트", "비자", "마스터카드",
]

TYPE_ALIASES = {
    "유튜브":     ["유튜브", "youtube", "유튜버", "증권사"],
    "경제방송":   ["경제방송", "방송", "broadcast"],
    "경제방송TV": ["경제방송TV", "경제방송tv", "securities_tv"],
    "애널리스트": ["애널리스트", "리포트", "report", "증권사"],
}

# FIX-VAL-2: "signal" 추가 → 팩트체크 Claude가 signal 값을 임의 변경하지 못하도록 보호
_PROTECTED_FIELDS = [
    "verified_price", "market", "naver_code", "code", "naver_url",
    "chart_base64", "rank", "total_count", "overlap_count",
    "weighted_score", "channel_counts", "channel_type", "signal",
]


# ── 캐시 저장 ─────────────────────────────────────────────────────────────────

def _save_to_cache(stock_name: str, code: str):
    try:
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        cache = {"date": today, "stock_map": {}}
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        cache.setdefault("stock_map", {})[stock_name] = code
        os.makedirs("data", exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"  [캐시저장] '{stock_name}' ({code}) 완료")
    except Exception as e:
        print(f"  [캐시저장] 실패: {e}")


# ── 섹션 순회 ─────────────────────────────────────────────────────────────────

def _iter_sections(data: dict):
    yield "stocks",       data.get("stocks",       [])
    yield "hidden_picks", data.get("hidden_picks", [])


def _get_all_stocks(data: dict) -> list:
    result = []
    for _, stock_list in _iter_sections(data):
        result.extend(stock_list)
    return result


# ── 리스트 정규화 ─────────────────────────────────────────────────────────────

def _listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# ── reasons 정규화 ────────────────────────────────────────────────────────────

def _normalize_reason(reason: Any) -> dict:
    if isinstance(reason, dict):
        return {
            "source_type": str(
                reason.get("source_type") or reason.get("section") or ""
            ).strip(),
            "source_name": str(
                reason.get("source_name") or reason.get("channel") or
                reason.get("firm") or ""
            ).strip(),
            # BUG-V-3: "reason" 필드도 detail로 매핑
            "detail": str(
                reason.get("detail") or reason.get("reason") or
                reason.get("summary") or ""
            ).strip(),
            "source_url": str(
                reason.get("source_url") or reason.get("url") or ""
            ).strip(),
        }
    text = str(reason).strip()
    if not text:
        return {}
    return {"source_type": "", "source_name": "", "detail": text, "source_url": ""}


def _normalize_reasons(stock: dict):
    normalized = []
    for reason in _listify(stock.get("reasons", [])):
        item = _normalize_reason(reason)
        if any(item.values()):
            normalized.append(item)
    stock["reasons"] = normalized


# ── 원본 데이터 정규화 ────────────────────────────────────────────────────────

def _normalize_all_data(all_data) -> list:
    if isinstance(all_data, list):
        return all_data
    return []


# ── 소스 타입 매칭 ────────────────────────────────────────────────────────────

def _match_source_type(type_a: str, type_b: str) -> bool:
    a = (type_a or "").lower()
    b = (type_b or "").lower()
    if a in b or b in a:
        return True
    for aliases in TYPE_ALIASES.values():
        if any(alias in a for alias in aliases) and any(alias in b for alias in aliases):
            return True
    return False


def _name_in_text(stock_name: str, text: str) -> bool:
    n = stock_name.lower().strip()
    t = (text or "").lower()
    if not n or not t:
        return False
    if n in t or n.replace(" ", "") in t.replace(" ", ""):
        return True
    for part in n.split():
        if len(part) >= 2 and part in t:
            return True
    clean = re.sub(r"[(주식회사\s)]", "", n)
    if len(clean) >= 2 and clean in t:
        return True
    return False


# ── 코드 조회 팩토리 ──────────────────────────────────────────────────────────

def _cached_verify_factory(stock_map: dict | None):
    _naver_cache = {}

    def _get_code(stock_name: str, naver_result: dict | None) -> str | None:
        if naver_result and naver_result.get("code"):
            return str(naver_result["code"]).zfill(6)
        if stock_map and stock_name in stock_map:
            return str(stock_map[stock_name]).zfill(6)
        if stock_map:
            name_clean = stock_name.replace(" ", "")
            best_match = None
            best_len   = 9999
            for key, code in stock_map.items():
                key_clean = str(key).replace(" ", "")
                if name_clean in key_clean or key_clean in name_clean:
                    if len(key_clean) < best_len:
                        best_match = (key, code)
                        best_len   = len(key_clean)
            if best_match:
                print(f"  [부분일치] '{stock_name}' → '{best_match[0]}' ({best_match[1]})")
                return str(best_match[1]).zfill(6)
        auto_result = search_code_by_autocomplete(stock_name)
        if auto_result and auto_result.get("code"):
            code = str(auto_result["code"]).zfill(6)
            if stock_map is not None:
                stock_map[stock_name] = code
            _save_to_cache(stock_name, code)
            return code
        return None

    def _cached_verify(stock_name: str) -> dict:
        if stock_name not in _naver_cache:
            _naver_cache[stock_name] = verify_stock_via_naver(stock_name)
        return _naver_cache[stock_name]

    return _cached_verify, _get_code


# ── 차트 생성 ─────────────────────────────────────────────────────────────────

def _fetch_chart(name: str, code: str) -> str | None:
    """
    14일 캔들스틱 차트 base64 반환.
    BUG-V-4: 데이터 부족 시 7일 → 3일 순서로 재시도
    """
    for days in [14, 7, 3]:
        try:
            daily = fetch_naver_daily_prices(code, days=days)
            if daily and len(daily) >= 2:
                chart_b64 = generate_candlestick_base64(daily, name)
                if chart_b64:
                    print(f"  [CHART] {name} 차트 생성 완료 ({len(daily)}일)")
                    return chart_b64
        except Exception as e:
            print(f"  [CHART] {name} {days}일 시도 실패: {e}")
    print(f"  [CHART] {name} 차트 데이터 없음 → 네이버 링크 사용")
    return None


# ── FIX-PRICE-1: verified_price를 int/None으로 평탄화 ────────────────────────

def _fetch_price_with_fallback(name: str, code: str) -> tuple[int | None, str]:
    """
    주가 조회 후 html_generator가 기대하는 형태로 반환한다.

    Returns
    -------
    (verified_price, naver_url)
        verified_price : int (현재가) 또는 None (조회 실패)
        naver_url      : str (네이버 금융 종목 URL)
    """
    naver_url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        result = fetch_naver_stock_price(name, code_override=code)
        if result:
            price_raw = result.get("price")
            if isinstance(price_raw, str):
                price_int = int(re.sub(r"[^\d]", "", price_raw)) if re.search(r"\d", price_raw) else None
            elif isinstance(price_raw, (int, float)):
                price_int = int(price_raw)
            else:
                price_int = None
            return price_int, naver_url
    except Exception as e:
        print(f"  [PRICE] {name} 조회 예외: {e}")

    print(f"  [PRICE] {name} ({code}) 주가 조회 실패 → None 반환")
    return None, naver_url


# ── 메인 검증 함수 ────────────────────────────────────────────────────────────

def validate_stocks(data: dict, all_data=None, api_key: str = "",
                    stock_map: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("[검증] 분석 결과 원본 재확인 시작...")
    print("=" * 60)

    # reasons 정규화
    for _, stock_list in _iter_sections(data):
        for stock in stock_list:
            _normalize_reasons(stock)

    all_data_list = _normalize_all_data(all_data)
    _cached_verify, _get_code = _cached_verify_factory(stock_map)

    # ── 검증-A: 원본 데이터 재확인 ────────────────────────────────────────────
    if all_data_list:
        print("\n[검증-A] 각 소스별 원본 데이터 재확인...")
        source_pool: dict[str, list[str]] = {}

        for item in all_data_list:
            st = item.get("source_type", "기타")
            # FIX-VAL-1: filter(None, ...) 으로 None/빈문자열 제거 후 join
            text = " ".join(filter(None, [
                item.get("title",      "") or "",
                item.get("summary",    "") or "",
                item.get("content",    "") or "",
                item.get("transcript", "") or "",
                item.get("stock_name", "") or "",
            ])).strip().lower()
            if text:
                source_pool.setdefault(st, []).append(text)

        for stype, texts in source_pool.items():
            print(f"  [DATA] {stype}: {len(texts)}건")

        for _, stock_list in _iter_sections(data):
            for stock in stock_list:
                name             = stock.get("name", "")
                verified_reasons = []
                removed_sources  = []

                for reason in stock.get("reasons", []):
                    reason_stype  = reason.get("source_type", "")
                    matched_texts = []
                    for pool_stype, pool_texts in source_pool.items():
                        if _match_source_type(reason_stype, pool_stype):
                            matched_texts.extend(pool_texts)
                    if not matched_texts:
                        verified_reasons.append(reason)
                        continue
                    # FIX-VAL-1: if t 조건으로 None/빈문자열 건너뜀
                    if any(_name_in_text(name, t) for t in matched_texts if t):
                        verified_reasons.append(reason)
                    else:
                        removed_sources.append(reason_stype or "기타")

                if removed_sources:
                    print(f"  [TRIM] {name}: [{', '.join(removed_sources)}] 근거 없음 → 제거")
                stock["reasons"] = verified_reasons

        print("[검증-A] 완료")
    else:
        print("[검증-A] 원본 데이터 없음 → 스킵")

    # ── 검증-B: 네이버 금융 주가/차트 조회 ────────────────────────────────────
    print("\n[검증-B] 네이버 금융 종목 확인 및 주가 조회...")
    for _, stock_list in _iter_sections(data):
        for stock in stock_list:
            name       = stock.get("name", "")
            is_foreign = any(kw.lower() in name.lower() for kw in FOREIGN_KEYWORDS)

            if is_foreign:
                stock["market"]         = "해외"
                stock["verified_price"] = None
                stock["chart_base64"]   = None
                stock["naver_url"]      = ""
                print(f"  [해외] {name} 스킵")
                continue

            stock["market"] = "국내"

            code = str(
                stock.get("code", "") or
                stock.get("naver_code", "") or
                stock.get("ticker", "")
            ).strip()
            code = code.zfill(6) if code.isdigit() else ""

            if not code:
                naver_result = _cached_verify(name)
                code         = _get_code(name, naver_result) or ""

            if code:
                stock["naver_code"] = code
                stock["code"]       = code

                price_int, naver_url = _fetch_price_with_fallback(name, code)
                stock["verified_price"] = price_int
                stock["naver_url"]      = naver_url

                if price_int is not None:
                    print(f"  [PRICE] {name}: {price_int:,}원")
                else:
                    print(f"  [PRICE] {name}: 조회 실패 (링크: {naver_url})")

                stock["chart_base64"] = _fetch_chart(name, code)

            else:
                print(f"  [WARN] {name}: 코드 조회 실패 → 종목 유지, 주가/차트 없음")
                stock["verified_price"] = None
                stock["chart_base64"]   = None
                stock["naver_url"]      = (
                    f"https://finance.naver.com/search/searchResult.naver"
                    f"?query={name.replace(' ', '+')}"
                )

    print("[검증-B] 완료")

    # ── 검증-C: Claude 팩트체크 ───────────────────────────────────────────────
    print("\n[검증-C] 최종 데이터 팩트체크...")
    all_stocks = _get_all_stocks(data)
    if not all_stocks:
        print("[검증-C] 종목 없음 → 스킵")
        return data
    if not api_key:
        print("[검증-C] ANTHROPIC_API_KEY 없음 → 스킵")
        return data

    try:
        company_info_lines: list[str] = []
        price_info_lines:   list[str] = []

        for stock in all_stocks:
            name = stock.get("name", "")
            code = str(
                stock.get("code", "") or
                stock.get("naver_code", "")
            ).strip()

            if code:
                print(f"  [기업정보] {name}({code}) 조회 중...")
                try:
                    company_info = fetch_naver_company_info(code)
                    if company_info.get("sector"):
                        peers     = company_info.get("peers", [])[:5]
                        peers_str = ", ".join(peers) if peers else "정보없음"
                        company_info_lines.append(
                            f"- {name}: 업종={company_info['sector']}, 동종업종=[{peers_str}]"
                        )
                except Exception as e:
                    print(f"  [기업정보] {name} 조회 실패: {e}")

            price_val = stock.get("verified_price")
            if isinstance(price_val, int):
                price_info_lines.append(f"- {name}: {price_val:,}원")
            elif stock.get("market") == "해외":
                price_info_lines.append(f"- {name}: 해외 종목")

        company_block = "\n".join(company_info_lines) if company_info_lines else "기업정보 없음"
        price_block   = "\n".join(price_info_lines)   if price_info_lines   else "주가 데이터 없음"

        check_target = json.loads(json.dumps(data, ensure_ascii=False))
        for key in ["stocks", "hidden_picks"]:
            for stock in check_target.get(key, []):
                stock.pop("chart_base64", None)

        target_json = json.dumps(check_target, ensure_ascii=False, indent=2)

        fc_prompt = (
            "당신은 한국 주식시장 전문 팩트체커입니다.\n"
            "아래 AI 브리핑 데이터를 읽고 내용상 사실 오류가 있는지 검토하세요.\n\n"
            "## 검토 항목:\n"
            "1. 기업 설명 오류 (잘못된 업종, 사업 내용)\n"
            "2. 사실관계 오류 (수치, 날짜, 인물, 이벤트)\n"
            "3. 업종 설명이 아래 네이버 기업정보와 일치하는지 확인\n\n"
            f"## 네이버 금융 기업정보:\n{company_block}\n\n"
            f"## 실시간 주가 데이터 (참고용):\n{price_block}\n\n"
            "## 검토 대상 브리핑 데이터:\n"
            + CB + "json\n"
            + target_json + "\n"
            + CB + "\n\n"
            "## 응답 규칙:\n"
            "- 오류 발견 시 해당 부분만 수정해 전체 JSON 반환\n"
            "- 오류 없으면 원본 JSON 그대로 반환\n"
            "- 종목을 삭제하지 마세요. 내용만 교정하세요\n"
            f"- 다음 필드는 절대 변경하지 마세요: "
            f"{', '.join(_PROTECTED_FIELDS)}, source_url, market_summary\n"
            "- 반드시 JSON만 반환하세요 (```json 블록으로 감싸세요)"
        )

        print("  [API] 팩트체크 Claude 호출...")
        fc_result = call_claude_with_retry(fc_prompt, api_key, max_tokens=12000)

        if not fc_result:
            print("[검증-C] API 응답 없음 → 원본 유지")
            return data

        corrected = _try_parse_json_local(fc_result)
        if not corrected:
            print("[검증-C] JSON 파싱 실패 → 원본 유지")
            return data

        # BUG-V-11: 보호 필드 복원
        for key in ["stocks", "hidden_picks"]:
            orig_list = data.get(key, [])
            corr_list = corrected.get(key, [])
            if not isinstance(corr_list, list):
                continue

            orig_map = {s.get("name", ""): s for s in orig_list if s.get("name")}

            for corr_stock in corr_list:
                name       = corr_stock.get("name", "")
                orig_stock = orig_map.get(name)
                if not orig_stock:
                    continue

                for field in _PROTECTED_FIELDS:
                    if field in orig_stock:
                        corr_stock[field] = orig_stock[field]

                corr_reasons = corr_stock.get("reasons", [])
                orig_reasons = orig_stock.get("reasons", [])
                if isinstance(corr_reasons, list):
                    for idx, corr_reason in enumerate(corr_reasons):
                        if not isinstance(corr_reason, dict):
                            continue
                        if idx < len(orig_reasons):
                            corr_reason["source_url"] = (
                                orig_reasons[idx].get("source_url", "")
                            )

            if corr_list:
                data[key] = corr_list

        # FIX-KEY-1: ai_strategy 키로 저장
        for try_key in ("ai_strategy", "investment_strategy"):
            val = corrected.get(try_key, "")
            if isinstance(val, str) and val.strip():
                data["ai_strategy"] = val.strip()
                break

        print("[검증-C] 완료")

    except Exception as e:
        print(f"[검증-C] 오류: {e} → 원본 유지")

    total = sum(len(data.get(k, [])) for k in ["stocks", "hidden_picks"])
    print("\n" + "=" * 60)
    print(f"[검증 완료] 총 {total}개 종목")
    print("=" * 60)
    return data


def _try_parse_json_local(text: str):
    """
    validation.py 내부용 JSON 파싱 헬퍼.
    순환 임포트 방지를 위해 ai_analyzer._try_parse_json과 별도로 정의.
    """
    match = re.search(r'```json\s*([\s\S]*?)```', text)
    if match:
        candidate = match.group(1).strip()
    else:
        start = text.find('{')
        end   = text.rfind('}')
        if start == -1 or end == -1:
            return None
        candidate = text[start:end + 1]

    def _clean_0(s): return s
    def _clean_1(s): return s.replace('\n', ' ').replace('\r', '')
    def _clean_2(s): return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    def _clean_3(s): return re.sub(r',\s*([}\]])', r'\1', _clean_2(s))

    for step, cleaner in enumerate([_clean_0, _clean_1, _clean_2, _clean_3], 1):
        try:
            result = json.loads(cleaner(candidate))
            if step > 1:
                print(f"  [JSON파싱] {step}단계 복구 성공")
            return result
        except json.JSONDecodeError:
            continue

    print("  [JSON파싱] 모든 단계 실패")
    return None
