# analyzer/gemini_validator.py
"""
Gemini를 활용한 브리핑 교차 검수 모듈

역할:
  Claude가 생성한 브리핑 JSON을 독립적인 시각으로 검수.
  - signal ↔ summary 논리 일치 여부
  - 수집 데이터에 근거 없는 단정적 표현 탐지
  - PDF 리포트 원문 대조 (선택적)
  - 주가 사실 확인 (Google Search Grounding, 선택적)

호출 조건:
  rule_validator.py에서 경고가 발생했을 때만 호출 (비용 제어)
  또는 애널리스트 리포트 PDF 검수는 항상 호출

수정 이력:
- GEMINI-VAL-1 : 최초 작성 — signal/summary 교차 검수
- GEMINI-VAL-2 : PDF 리포트 원문 대조 추가
- GEMINI-VAL-3 : Google Search Grounding 주가 확인 추가
- GEMINI-VAL-4 : rule_validator 연동 — 경고 있을 때만 호출하는 조건부 구조
"""

import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from google import genai
    from google.genai import types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    print("[GeminiVal] google-genai 미설치 → 검수 비활성화")

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

# GEMINI-VAL-5: 모델명을 상수로 분리 (gemini-1.5-pro/flash는 완전히 shutdown됨).
# 2026-06 기준 안정 서비스 중. 다음에 또 막히면 이 값만 바꾸면 됨.
_MODEL_FLASH = "gemini-2.5-flash"
_MODEL_PRO   = "gemini-2.5-flash"


# ── 내부 유틸리티 ─────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _get_client(api_key: str):
    """GEMINI-VAL-5: legacy genai.configure()/GenerativeModel() → Client 객체로 교체."""
    return genai.Client(api_key=api_key)


def _model_name(use_flash: bool = False) -> str:
    """Flash는 저비용 검수용, Pro급은 PDF/영상 처리용 (현재는 동일 모델 사용)."""
    return _MODEL_FLASH if use_flash else _MODEL_PRO


def _today_str() -> str:
    """
    GEMINI-VAL-6: Gemini는 학습 시점 기준으로 '현재'를 추정하기 때문에,
    날짜 컨텍스트를 안 주면 2025~2026년 등 실제로는 이미 지났거나 진행 중인
    분기/연도를 "미래"로 잘못 판단해 엉뚱한 시제 오류를 지적하는 경우가 있음
    (예: 2Q26이 진행 중인 현재 분기인데도 '미래 시점'이라 오판).
    검수 프롬프트에 오늘 날짜를 명시해 이런 오판을 방지.
    """
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y년 %m월 %d일")


# ── 1. 코드 룰 검수 (비용 0, 항상 실행) ─────────────────────────────────────

def run_rule_validation(result: dict, filtered_mentions: list) -> list:
    """
    Claude 반환 JSON을 코드 룰로 검수.
    Gemini 호출 없이 즉시 실행, 비용 0.

    반환: 경고 문자열 목록 (비어 있으면 이상 없음)
    """
    warnings      = []
    filtered_names = {name for name, _ in filtered_mentions}
    stocks         = result.get("stocks", [])
    expected       = len(filtered_mentions)

    # 1. 종목 수 검증
    if len(stocks) < expected:
        warnings.append(
            f"종목 수 부족: 기대 {expected}개 → 실제 {len(stocks)}개 반환"
        )

    # 2. hallucination 검증 — 수집 데이터에 없는 종목 탐지
    for s in stocks:
        name = s.get("name", "")
        if name and name not in filtered_names:
            warnings.append(f"미수집 종목 포함: '{name}' (프롬프트에 없던 종목)")

    # 3. 필수 필드 누락 검증
    required_fields = ["name", "code", "signal", "summary"]
    for s in stocks:
        for field in required_fields:
            if not s.get(field):
                warnings.append(
                    f"필수 필드 누락: '{s.get('name', '?')}' → {field} 없음"
                )

    # 4. signal 값 범위 검증
    valid_signals = {"긍정", "중립", "부정"}
    for s in stocks:
        sig = s.get("signal", "")
        if sig and sig not in valid_signals:
            warnings.append(f"signal 비정상: '{s.get('name')}' → '{sig}'")

    # 5. market_summary 길이 검증
    ms = result.get("market_summary", "")
    if len(ms) < 200:
        warnings.append(f"market_summary 너무 짧음: {len(ms)}자")

    # 6. 히든픽 수집 데이터 검증
    hidden_picks   = result.get("hidden_picks", [])
    for hp in hidden_picks:
        name = hp.get("name", "")
        if name and name not in filtered_names:
            warnings.append(f"히든픽 미수집 종목: '{name}'")

    for w in warnings:
        print(f"[룰검수] ⚠️  {w}")

    if not warnings:
        print("[룰검수] ✅ 이상 없음")

    return warnings


# ── 2. Gemini 내용 검수 (경고 있을 때만 실행) ────────────────────────────────

def run_gemini_content_validation(
    result: dict,
    warnings: list,
    api_key: str,
) -> dict:
    """
    rule_validation에서 경고가 발생했을 때 Gemini Flash로
    signal ↔ summary 논리 일치 및 단정적 표현 탐지.

    반환:
    {
      "signal_issues": [{"name": "종목명", "issue": "설명"}],
      "overstatements": ["과도한 단정 표현 목록"],
      "overall": "pass|warn|fail"
    }
    """
    if not _GEMINI_AVAILABLE or not api_key:
        return {"signal_issues": [], "overstatements": [], "overall": "skip"}

    if not warnings:
        print("[Gemini내용검수] 룰 검수 경고 없음 → 스킵")
        return {"signal_issues": [], "overstatements": [], "overall": "pass"}

    print(f"[Gemini내용검수] 경고 {len(warnings)}개 → Gemini Flash 검수 시작")

    client = _get_client(api_key)

    stocks_summary = []
    for s in result.get("stocks", []):
        stocks_summary.append(
            f"종목:{s.get('name')} | signal:{s.get('signal')} | "
            f"요약:{s.get('summary', '')[:100]} | "
            f"촉매:{s.get('catalyst', '')[:80]}"
        )

    prompt = (
        f"오늘 날짜는 {_today_str()}입니다. 이 날짜를 기준으로 판단하세요.\n\n"
        "아래는 AI가 생성한 주식 브리핑 데이터입니다.\n"
        "다음 두 가지를 검토하세요:\n\n"
        "1. signal(긍정/중립/부정)이 요약(summary) 내용과 논리적으로 일치하는지\n"
        "   예: 요약이 '실적 부진, 목표주가 하향'인데 signal이 '긍정'이면 불일치\n\n"
        "2. 근거 없이 단정적인 표현이 있는지\n"
        "   예: '반드시 상승', '확실한 매수 타이밍' 등\n\n"
        "JSON으로만 응답하세요:\n"
        "{\n"
        '  "signal_issues": [{"name": "종목명", "issue": "불일치 설명"}],\n'
        '  "overstatements": ["과도한 표현1", "과도한 표현2"],\n'
        '  "overall": "pass(이상없음)|warn(경미)|fail(심각)" 중 택1\n'
        "}\n\n"
        "[검토 데이터]\n" + "\n".join(stocks_summary)
    )

    try:
        response = client.models.generate_content(
            model=_model_name(use_flash=True),
            contents=prompt,
        )
        parsed   = _parse_json(response.text)
        if parsed:
            overall = parsed.get("overall", "pass")
            issues  = parsed.get("signal_issues", [])
            overs   = parsed.get("overstatements", [])
            print(f"[Gemini내용검수] 결과: {overall} "
                  f"/ signal 불일치: {len(issues)}건 "
                  f"/ 단정 표현: {len(overs)}건")
            for issue in issues:
                print(f"  ⚠️  signal 불일치: {issue.get('name')} — {issue.get('issue')}")
            for o in overs:
                print(f"  ⚠️  단정 표현: {o}")
            return parsed
    except Exception as e:
        print(f"[Gemini내용검수] 실패: {e}")

    return {"signal_issues": [], "overstatements": [], "overall": "skip"}


# ── 3. 애널리스트 리포트 PDF 원문 대조 ───────────────────────────────────────

def verify_analyst_reports(all_data: list, api_key: str) -> list:
    """
    all_data에서 애널리스트 리포트 중 PDF/링크가 있는 항목을
    Gemini 1.5 Pro로 원문 대조 검수.

    반환: 검수 결과 목록
    [{
      "stock_name": "종목명",
      "broker": "증권사",
      "verified": True/False,
      "opinion_match": True/False,
      "target_price_match": True/False,
      "issues": ["문제점1"]
    }]
    """
    if not _GEMINI_AVAILABLE or not api_key:
        return []

    if not _HTTPX_AVAILABLE:
        print("[GeminiRPT] httpx 미설치 → PDF 검수 스킵")
        return []

    analyst_items = [
        d for d in all_data
        if d.get("source_type") == "애널리스트"
        and d.get("link")
        and d.get("ai_summary")
    ]

    if not analyst_items:
        print("[GeminiRPT] 검수 대상 리포트 없음")
        return []

    # PDF 링크 있는 항목만 — HTML 페이지는 스킵
    pdf_items = [
        d for d in analyst_items
        if d.get("link", "").endswith(".pdf")
    ]

    if not pdf_items:
        print("[GeminiRPT] PDF 링크 있는 리포트 없음 → 텍스트 기반 검수로 전환")
        return _verify_via_text(analyst_items, api_key)

    client  = _get_client(api_key)
    results = []

    for item in pdf_items[:5]:  # 비용 제어: 최대 5개
        stock_name   = item.get("stock_name", "")
        broker       = item.get("source_name", "")
        ai_summary   = item.get("ai_summary", "")
        opinion      = item.get("opinion", "")
        target_price = item.get("target_price", "")
        pdf_url      = item.get("link", "")

        try:
            pdf_bytes = httpx.get(pdf_url, timeout=15).content
        except Exception as e:
            print(f"  [GeminiRPT] PDF 다운로드 실패 ({stock_name}): {e}")
            continue

        prompt = (
            f"아래 AI 요약이 이 PDF 리포트 원문과 일치하는지 검토하세요.\n\n"
            f"[AI 요약]: {ai_summary}\n"
            f"[수집된 투자의견]: {opinion}\n"
            f"[수집된 목표주가]: {target_price}원\n\n"
            "검토 기준:\n"
            "1. 원문에 없는 수치가 요약에 있는가\n"
            "2. 투자의견(매수/중립/매도)이 원문과 일치하는가\n"
            "3. 목표주가가 원문과 일치하는가\n\n"
            "JSON으로만 응답:\n"
            "{\n"
            '  "verified": true/false,\n'
            '  "opinion_match": true/false,\n'
            '  "target_price_match": true/false,\n'
            '  "issues": ["문제점1"]\n'
            "}"
        )

        try:
            # GEMINI-VAL-5: dict 형태({"mime_type":...,"data":...})는 legacy SDK 방식.
            # 신규 SDK는 types.Part.from_bytes()로 인라인 바이너리 데이터를 전달.
            response = client.models.generate_content(
                model=_model_name(use_flash=False),
                contents=[
                    types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    prompt,
                ],
            )
            parsed = _parse_json(response.text)
            if parsed:
                parsed["stock_name"] = stock_name
                parsed["broker"]     = broker
                results.append(parsed)
                status = "✅" if parsed.get("verified") else "⚠️"
                print(f"  {status} [{stock_name}/{broker}] "
                      f"의견일치:{parsed.get('opinion_match')} "
                      f"목표가일치:{parsed.get('target_price_match')}")
        except Exception as e:
            print(f"  [GeminiRPT] 검수 실패 ({stock_name}): {e}")

        time.sleep(1.0)

    print(f"[GeminiRPT] PDF 검수 완료: {len(results)}건")
    return results


def _verify_via_text(analyst_items: list, api_key: str) -> list:
    """
    PDF 없을 때 텍스트(ai_summary + opinion)로 간이 검수.
    Gemini Flash 사용 (저비용).
    """
    client  = _get_client(api_key)
    results = []

    items_text = []
    for item in analyst_items[:10]:
        items_text.append(
            f"종목:{item.get('stock_name')} | 증권사:{item.get('source_name')} | "
            f"의견:{item.get('opinion')} | 목표가:{item.get('target_price')} | "
            f"요약:{item.get('ai_summary', '')[:150]}"
        )

    prompt = (
        f"오늘 날짜는 {_today_str()}입니다. 이 날짜를 기준으로 판단하세요 "
        "(예: 이미 지났거나 현재 진행 중인 연도/분기를 '미래 시점'으로 오판하지 마세요).\n\n"
        "아래 애널리스트 리포트 요약 목록에서 이상한 점을 찾아주세요.\n"
        "- 투자의견과 요약 내용이 서로 모순되는 경우\n"
        "- 목표주가가 현실적이지 않은 경우 (예: 0원, 음수, 비정상적 수치)\n"
        "- 요약에 근거 없는 수치가 포함된 경우\n\n"
        "JSON으로만 응답:\n"
        '{"issues": [{"stock_name": "종목명", "issue": "문제 설명"}]}\n\n'
        "[리포트 목록]\n" + "\n".join(items_text)
    )

    try:
        response = client.models.generate_content(
            model=_model_name(use_flash=True),
            contents=prompt,
        )
        parsed   = _parse_json(response.text)
        if parsed:
            issues = parsed.get("issues", [])
            for issue in issues:
                print(f"  ⚠️  [GeminiRPT텍스트] "
                      f"{issue.get('stock_name')}: {issue.get('issue')}")
            results = issues
    except Exception as e:
        print(f"[GeminiRPT텍스트] 실패: {e}")

    return results


# ── 4. 누락 종목 보충 ────────────────────────────────────────────────────────

def patch_missing_stocks(result: dict, filtered_mentions: list) -> dict:
    """
    rule_validation에서 탐지된 누락 종목을 빈 카드로 보충.
    Claude가 반환하지 않은 종목을 강제로 추가하여
    html_generator의 _filter_stocks_tiered가 처리할 수 있게 함.
    """
    existing_names = {s.get("name") for s in result.get("stocks", [])}
    added          = 0

    for name, data in filtered_mentions:
        if name not in existing_names:
            result.setdefault("stocks", []).append({
                "name":             name,
                "code":             data.get("code", ""),
                "signal":           "중립",
                "summary":          "",
                "catalyst":         "",
                "risk":             "",
                "channel_mentions": [],
                "channel_counts":   {},
                "total_count":      data.get("total_count", 0),
                "weighted_score":   round(data.get("weighted_score", 0), 2),
                "overlap_count":    len(data.get("channel_types", [])),
                "reasons":          [],
            })
            added += 1
            print(f"[누락보충] '{name}' 추가 (빈 카드)")

    if added:
        print(f"[누락보충] 총 {added}개 종목 보충 완료")
    return result


# ── 통합 검수 실행 함수 ──────────────────────────────────────────────────────

def run_full_validation(
    result: dict,
    filtered_mentions: list,
    all_data: list,
    api_key: str,
) -> dict:
    """
    전체 검수 파이프라인을 순서대로 실행하고 최종 result를 반환.

    순서:
    1. 코드 룰 검수 (항상, 비용 0)
    2. 누락 종목 보충 (항상, 비용 0)
    3. Gemini 내용 검수 (경고 있을 때만)
    4. 애널리스트 리포트 검수 (항상, PDF 있을 때만)
    """
    print("\n" + "=" * 50)
    print("[검수] 브리핑 검수 파이프라인 시작")

    # 1. 코드 룰 검수
    warnings = run_rule_validation(result, filtered_mentions)

    # 2. 누락 종목 보충
    result = patch_missing_stocks(result, filtered_mentions)

    # 3. Gemini 내용 검수 (경고 있을 때만)
    if warnings and api_key:
        run_gemini_content_validation(result, warnings, api_key)
    else:
        print("[Gemini내용검수] 경고 없음 → 스킵")

    # 4. 애널리스트 리포트 PDF 검수
    if api_key:
        verify_analyst_reports(all_data, api_key)
    else:
        print("[GeminiRPT] API 키 없음 → 스킵")

    print("[검수] 파이프라인 완료")
    print("=" * 50 + "\n")

    return result
