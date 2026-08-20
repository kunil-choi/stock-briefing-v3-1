# analyzer/ai_review.py
"""
AI-REVIEW-1: 관리자 페이지 "AI 검토" 기능을 위한 최종 브리핑 검증 결과 조립 모듈.

analyzer/gemini_validator.py가 이미 계산하는 룰 검수 · Gemini 내용 검수 ·
애널리스트 리포트 대조 결과를 구조화된 issue 목록으로 모으고, 여기에
Claude 팩트체크(원문 대조) 결과를 비파괴적 diff 형태로 더해
briefing_data.json에 저장할 ai_review 객체를 만든다.

analyzer/validation.py::validate_stocks()의 검증-C(Claude 팩트체크) 로직을
참고했지만, 그 함수는 Claude가 반환한 JSON으로 종목 필드를 직접 치환하는
방식이라 사람의 확인 없이 자동 파이프라인에 그대로 넣기엔 위험하다.
이 모듈은 원본 stock 데이터를 절대 변경하지 않고, 문제가 있는 필드만
"원문 vs 제안" diff로 만들어 관리자가 직접 판단하게 한다.
"""

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from .api_client import call_claude_with_retry
from .gemini_validator import _issue, run_full_validation

CB   = "```"
_KST = timezone(timedelta(hours=9))

# fact_check_diff 대상 필드. name/signal/code 등은 여기서 다루지 않는다
# (name·signal은 gemini_validator의 룰 검수가 이미 담당).
_DIFF_FIELDS = ["summary", "catalyst", "risk"]

_SOURCE_TEXT_FIELDS = ["title", "summary", "content", "transcript"]
_MAX_SOURCES_PER_STOCK = 3
_MAX_SOURCE_CHARS = 300


def _collect_source_excerpts(stock_name: str, all_data: list) -> list:
    """
    all_data(수집된 원문 목록)에서 종목명이 실제로 언급된 항목만 골라
    짧게 잘라 반환한다. Claude 팩트체크가 브리핑 문구를 "원문과" 대조할 수
    있도록, 원문 근거가 있는 종목만 대상으로 삼기 위함이다.
    """
    name = (stock_name or "").strip()
    if not name:
        return []
    name_compact = name.replace(" ", "")
    excerpts = []
    for item in all_data:
        if len(excerpts) >= _MAX_SOURCES_PER_STOCK:
            break
        text = " ".join(filter(None, (item.get(f, "") for f in _SOURCE_TEXT_FIELDS))).strip()
        if not text:
            continue
        if name in text or name_compact in text.replace(" ", ""):
            excerpts.append(text[:_MAX_SOURCE_CHARS])
    return excerpts


def _now_iso() -> str:
    return datetime.now(_KST).isoformat()


def _try_parse_json(text: str) -> Optional[dict]:
    """
    ai_analyzer._try_parse_json과 같은 역할의 관용적 JSON 파서.
    순환 임포트 방지를 위해 별도로 정의한다 (validation.py의
    _try_parse_json_local과 동일한 이유).
    """
    if not text:
        return None
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = match.group(1).strip() if match else None
    if not candidate:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def check_facts_against_sources(stocks: list, all_data: list, api_key: str) -> list:
    """
    각 종목의 summary/catalyst/risk를, 그 종목이 실제로 언급된 원문
    (all_data) 발췌와 대조해 Claude에게 팩트체크시킨다. 원문에서 해당
    종목 언급을 전혀 찾을 수 없는 경우에는 대조할 근거가 없으므로
    검토 대상에서 제외한다(근거 없는 종목 자체는 gemini_validator의
    hallucinated_stock 룰 검사가 이미 별도로 잡아낸다).

    stocks/all_data는 읽기 전용으로만 쓰이며 이 함수 안에서 변경되지 않는다.
    """
    if not stocks or not api_key:
        return []

    check_target = []
    for s in stocks:
        name = s.get("name", "")
        if not name:
            continue
        sources = _collect_source_excerpts(name, all_data)
        if not sources:
            continue
        check_target.append({
            "name":     name,
            "signal":   s.get("signal", ""),
            "summary":  s.get("summary", ""),
            "catalyst": s.get("catalyst", ""),
            "risk":     s.get("risk", ""),
            "sources":  sources,
        })
    if not check_target:
        return []

    prompt = (
        "당신은 한국 주식시장 전문 팩트체커입니다.\n"
        "아래는 AI가 생성한 주식 브리핑의 종목별 요약(summary/catalyst/risk)과,\n"
        "그 판단의 근거가 됐어야 할 원문 발췌(sources)입니다.\n"
        "각 종목의 summary/catalyst/risk 문구가 sources 원문과 실제로 부합하는지,\n"
        "원문에 없는 수치·날짜·사건·업종 설명을 지어내지는 않았는지 검토하세요.\n\n"
        "## 검토 대상 (종목별 sources = 실제 수집된 원문 발췌):\n"
        + CB + "json\n"
        + json.dumps(check_target, ensure_ascii=False, indent=2) + "\n"
        + CB + "\n\n"
        "## 응답 규칙:\n"
        "- sources 원문과 명백히 어긋나거나 원문에 없는 내용을 지어낸 경우만 지적하세요\n"
        "- sources가 짧게 발췌된 것이라 판단이 애매하면 지적하지 마세요 (확실한 경우만)\n"
        "- 표현 스타일 취향 차이는 지적하지 마세요 (사실 오류만)\n"
        "- 오류가 없으면 issues를 빈 배열로 반환하세요\n"
        "- 종목명(name)이나 signal 값은 절대 바꾸지 마세요\n"
        "- 반드시 JSON만 반환하세요 (```json 블록으로 감싸세요)\n\n"
        "JSON 형식:\n"
        "{\n"
        '  "issues": [\n'
        '    {"name": "종목명", "field": "summary|catalyst|risk", '
        '"problem": "원문 sources의 어떤 부분과 왜 어긋나는지 설명", "corrected_text": "정정된 문장 전체"}\n'
        "  ]\n"
        "}"
    )

    try:
        raw = call_claude_with_retry(prompt, api_key, max_tokens=4000)
    except Exception as e:
        print(f"[AI검토/팩트체크] API 호출 실패: {e}")
        return []

    parsed = _try_parse_json(raw)
    if not parsed:
        print("[AI검토/팩트체크] 응답 파싱 실패 → 스킵")
        return []

    stock_lookup = {s.get("name", ""): s for s in stocks if s.get("name")}
    issues = []
    for item in parsed.get("issues", []):
        name  = item.get("name", "")
        field = item.get("field", "")
        stock = stock_lookup.get(name)
        if not stock or field not in _DIFF_FIELDS:
            continue

        original  = str(stock.get(field, "") or "")
        suggested = str(item.get("corrected_text", "") or "")
        if not suggested.strip() or suggested.strip() == original.strip():
            continue

        issues.append(_issue(
            "fact_check_diff",
            item.get("problem") or f"'{name}'의 {field} 문구에 사실 오류 가능성이 있습니다.",
            severity="high",
            stock_name=name,
            field=field,
            source="claude_factcheck",
            original_text=original,
            suggested_text=suggested,
        ))

    print(f"[AI검토/팩트체크] {len(issues)}건 발견")
    return issues


def _compute_overall(issues: list) -> str:
    if any(i.get("severity") == "high" for i in issues):
        return "fail"
    if issues:
        return "warn"
    return "pass"


def build_ai_review(
    result: dict,
    filtered_mentions: list,
    all_data: list,
    gemini_api_key: str,
    claude_api_key: str,
) -> tuple:
    """
    브리핑 생성 직후 호출되는 최종 AI 검토 조립 함수.

    gemini_validator의 룰/내용/애널리스트 검수 issue와 Claude 팩트체크
    diff issue를 합쳐 관리자 페이지에 저장할 ai_review 객체를 만든다.

    반환: (result, ai_review)
    - result: run_full_validation()이 patch_missing_stocks() 등으로
      변경했을 수 있는 브리핑 결과 (그 외에는 이 함수가 직접 수정하지 않는다)
    - ai_review: briefing_data.json에 그대로 저장할 dict
    """
    if not gemini_api_key and not claude_api_key:
        print("[AI검토] API 키 없음 → 전체 스킵")
        return result, {
            "generated_at": _now_iso(),
            "overall": "skip",
            "issues": [],
            "admin_reviewed": False,
            "admin_reviewed_at": None,
        }

    gemini_issues = []
    if gemini_api_key:
        result, gemini_issues = run_full_validation(
            result, filtered_mentions, all_data, gemini_api_key
        )
    else:
        print("[AI검토] GEMINI_API_KEY 없음 → 룰/내용/애널리스트 검수 스킵")

    # NOTE: hidden_picks는 admin 페이지의 "브리핑 교정"에 편집 화면 자체가
    # 없어(카드 목록/텍스트 탭 모두 market_leaders/stocks만 다룸) 여기서
    # issue를 만들어도 관리자가 "제안 적용"·"카드로 이동"을 할 수 없다.
    # 따라서 fact-check 대상은 stocks로 한정한다.
    factcheck_issues = check_facts_against_sources(
        result.get("stocks", []), all_data, claude_api_key
    )

    issues  = gemini_issues + factcheck_issues
    overall = _compute_overall(issues)

    print(f"[AI검토] 최종 이슈 {len(issues)}건 (overall={overall})")

    return result, {
        "generated_at": _now_iso(),
        "overall": overall,
        "issues": issues,
        "admin_reviewed": False,
        "admin_reviewed_at": None,
    }
