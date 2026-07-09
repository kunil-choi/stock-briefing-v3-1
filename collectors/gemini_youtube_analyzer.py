# collectors/gemini_youtube_analyzer.py
"""
Gemini를 활용한 유튜브 영상 직접 분석 모듈

역할:
  - youtube_collector.py가 수집한 YouTube URL을 받아
    Gemini로 영상을 직접 시청·분석
  - 발언자 / 타임스탬프 / 실제 발언 원문 / 종목명 / 감성 추출
  - transcript(자막) 기반 분석을 병행하여 API 비용 절감

수정 이력:
- GEMINI-YT-1  : 최초 작성 — 영상 직접 분석 + transcript 폴백
- GEMINI-YT-2  : 배치 처리 추가 — 순차 처리
- GEMINI-YT-3  : 비용 제어 — 조회수/길이 기준으로 분석 대상 선별
- GEMINI-YT-4  : Content 구조 오류 수정
                 {"video_url": url} → parts 리스트 구조로 변경
                 YouTube URL은 file_data가 아닌 직접 url 방식 사용
                 (※ GEMINI-YT-5에서 이 판단이 잘못됐던 것으로 확인 — 되돌림)
- GEMINI-YT-5  : 전면 재작성.
                 1) google-generativeai(legacy) SDK는 2025-11-30 EOL,
                    저장소도 archived 상태 → google-genai(신규 통합 SDK)로 교체.
                 2) gemini-1.5-pro 모델은 이미 완전히 shutdown(404) →
                    현재 서비스 중인 모델로 교체. 모델명은 GEMINI_MODEL
                    상수로 분리해 다음 모델 교체 시 한 곳만 고치면 되도록 함.
                 3) GEMINI-YT-4의 "URL을 문자열로 직접 전달" 방식은 실제로는
                    Gemini가 영상으로 인식하지 못하는 잘못된 구조였음 →
                    공식 문서대로 types.Part(file_data=types.FileData(...))
                    구조로 복원.
"""

import json
import re
import time
from typing import Optional

# ── Gemini SDK 임포트 (GEMINI-YT-5: 신규 통합 SDK google-genai) ──────────────
try:
    from google import genai
    from google.genai import types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    print("[GeminiYT] google-genai 미설치 → 영상 분석 비활성화")

# GEMINI-YT-5: 모델명을 상수로 분리.
# Gemini는 모델을 자주 셧다운하므로(예: gemini-1.5-pro, gemini-2.0-flash 등
# 이미 shutdown) 다음에 또 막히면 이 한 줄만 바꾸면 되도록 구성.
# 2026-06 기준 안정 서비스 중인 모델. 추후 ai.google.dev/gemini-api/docs/models
# 의 deprecation 페이지에서 현재 상태 확인 권장.
GEMINI_MODEL = "gemini-2.5-flash"


# ── 분석 단계별 설정 ──────────────────────────────────────────────────────────
# 1단계: 경량 스캔 — transcript 있는 영상 대상 + 패널리스트 제목 포함 영상 우선
_MIN_TRANSCRIPT_CHARS = 50    # transcript 최소 길이 (짧아도 활용)
_SCAN_SLEEP_SEC       = 0.5   # 1단계 스캔 간격 (빠르게)

# 2단계: 심층 분석 — 선별된 영상만, 영상 직접분석으로 fact/quote 추출
_MAX_DEEP_ANALYSIS    = 7     # 심층 분석 최대 건수 (타임라인 기준: 영상당 45초 × 7개 ≈ 5분)
_DEEP_TIER1_MAX       = 4     # 1순위 (패널리스트 제목) 최대 건수
_DEEP_TIER2_MAX       = 3     # 2순위 (스캔 통과) 최대 건수
_DEEP_SLEEP_SEC       = 2.0   # 2단계 분석 간격 (여유있게)
_DEEP_TIMEOUT_SEC     = 45    # 영상 1개당 최대 대기 시간

# 패널리스트 실명 목록 (우선순위 판단용)
from config import POPULAR_PANELISTS as _PANELISTS

# ── 프롬프트 템플릿 ───────────────────────────────────────────────────────────
# ── 1단계: 경량 스캔 프롬프트 (transcript 텍스트만 사용) ─────────────────────
_PROMPT_SCAN = """
아래는 유튜브 영상의 자막입니다.
다음 두 가지만 판단하세요.

1. 실명이 확인된 금융 전문가/애널리스트가 등장하는가?
2. 특정 종목에 대해 구체적인 투자 의견, 목표주가, 실적 전망 등을 언급하는가?

JSON으로만 응답:
{{
  "has_expert": true/false,
  "has_specific_mention": true/false,
  "detected_names": ["발견된 전문가 이름 (있을 경우)"],
  "detected_stocks": ["언급된 종목명 (있을 경우)"],
  "worth_deep_analysis": true/false
}}

worth_deep_analysis = true 조건: has_expert AND has_specific_mention 둘 다 true일 때

[자막]
{transcript}
"""

# ── 2단계: 심층 분석 프롬프트 (영상 직접분석) ────────────────────────────────
_PROMPT_VIDEO = """
이 유튜브 영상을 분석하여 주식 종목 언급을 추출하세요.
방송 제작용 데이터로 사용되므로, 정확성이 최우선입니다.

[분석 기준]
- 출연자가 특정 종목에 대해 투자 의견/전망/수치를 명확히 언급한 경우만 포함
- 단순 종목명 언급, 지나가는 언급은 제외
- 영상에서 확인되지 않은 내용 절대 추가 금지

[발언자 확인]
- 화면 하단 자막(이름/소속)을 최우선으로 확인
- 특정 불가하면 speaker를 빈 문자열로

JSON 형식으로만 응답:
{
  "video_summary": "영상 전체 주제 1~2문장",
  "main_speaker": "주요 발언자 이름과 소속/직책 (예: 염승환 LS증권 이사)",
  "speakers": ["출연자1 이름/소속", "출연자2 이름/소속"],
  "mentions": [
    {
      "stock_name": "종목명 (한국어 정식 명칭)",
      "timestamp": "MM:SS (확인된 경우만, 모르면 빈 문자열)",
      "speaker": "발언자 이름과 소속/직책 (화면 자막 기준, 모르면 빈 문자열)",
      "fact": "핵심 팩트 1문장 — 구체적 수치/전망 포함 (예: 목표주가 10만원, 3분기 영업이익 15조 전망)",
      "quote": "발언자 실제 발언 원문 1~2문장 (예: 지금 삼성전자 안 사면 평생 후회합니다)",
      "sentiment": "긍정|중립|부정 중 택1",
      "confidence": "높음|보통|낮음"
    }
  ]
}
"""




# ── 내부 유틸리티 ────────────────────────────────────────────────────────────

def _parse_gemini_response(text: str) -> Optional[dict]:
    """Gemini 응답에서 JSON 추출."""
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


def _analyze_via_video_url(client, video_url: str) -> Optional[dict]:
    """
    GEMINI-YT-5:
    YouTube URL은 file_data(FileData) 구조로 전달해야 Gemini가
    실제 영상으로 인식한다. 단순 문자열로 넘기면 텍스트로만 취급되어
    영상 내용을 전혀 보지 못한 채 항상 실패한다 (GEMINI-YT-4의 오판 수정).
    """
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=types.Content(parts=[
                types.Part(file_data=types.FileData(file_uri=video_url)),
                types.Part(text=_PROMPT_VIDEO),
            ]),
        )
        return _parse_gemini_response(response.text)
    except Exception as e:
        print(f"    [GeminiYT] 영상 직접 분석 실패 ({video_url}): {e}")
        return None


# ── 메인 분석 함수 ───────────────────────────────────────────────────────────

def _scan_transcript(client, transcript: str, video_url: str) -> dict:
    """
    1단계: transcript 텍스트만으로 경량 스캔.
    전문가 실명 + 종목 구체 언급 여부만 판단 (YES/NO).
    """
    if not transcript or len(transcript) < _MIN_TRANSCRIPT_CHARS:
        return {"worth_deep_analysis": False}
    prompt = _PROMPT_SCAN.format(transcript=transcript[:3000])
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        result = _parse_gemini_response(response.text)
        return result or {"worth_deep_analysis": False}
    except Exception as e:
        print(f"    [GeminiYT 스캔] 실패 ({video_url[:50]}): {e}")
        return {"worth_deep_analysis": False}


def analyze_youtube_items(
    youtube_items: list,
    api_key: str,
) -> list:
    """
    2단계 파이프라인으로 Gemini 분석 수행.

    1단계 — 경량 스캔: transcript 있는 전체 영상 대상, 전문가+종목 언급 여부만 판단
    2단계 — 심층 분석: 1단계 통과 영상만, 영상 직접분석으로 fact/quote 추출 (최대 5개)

    우선순위:
      1순위: 제목에 패널리스트 실명 포함 영상 (최대 3개)
      2순위: 1단계 스캔 통과한 유튜브/경제방송 영상 (최대 2개)
      증권사 채널: 심층 분석 제외
    """
    if not _GEMINI_AVAILABLE:
        print("[GeminiYT] google-genai SDK 없음 → 전체 스킵")
        return youtube_items

    if not api_key:
        print("[GeminiYT] GEMINI_API_KEY 없음 → 전체 스킵")
        return youtube_items

    client = genai.Client(api_key=api_key)

    # 스캔 대상 선별
    # - 증권사 채널 제외
    # - transcript 있는 영상 OR 패널리스트 이름이 제목에 포함된 영상
    def _has_panelist_in_title(item):
        title = item.get("title", "")
        return any(name in title for name in _PANELISTS)

    # 스캔 대상: 증권사 포함 전체 — description 또는 transcript가 있거나 패널리스트 제목
    scannable = [
        item for item in youtube_items
        if len(item.get("summary", "") or "") >= _MIN_TRANSCRIPT_CHARS
        or len(item.get("description", "") or "") >= _MIN_TRANSCRIPT_CHARS
        or _has_panelist_in_title(item)
    ]
    non_scannable = [
        item for item in youtube_items
        if item not in scannable
    ]

    has_desc  = sum(1 for i in scannable if len(i.get("description","") or "") >= _MIN_TRANSCRIPT_CHARS)
    has_trans = sum(1 for i in scannable if i.get("has_transcript"))
    print(f"[GeminiYT] 1단계 스캔 대상: {len(scannable)}개 "
          f"(transcript:{has_trans}개 / description:{has_desc}개 / 무관 {len(non_scannable)}개 제외)")

    # ── 1단계: 경량 스캔 ─────────────────────────────────────────────────────
    for item in scannable:
        transcript = item.get("summary", "") or ""
        title      = item.get("title", "")
        video_url  = item.get("link", "")

        has_panelist_in_title = _has_panelist_in_title(item)
        item["_panelist_in_title"] = has_panelist_in_title

        # 패널리스트가 제목에 있으면 스캔 없이 바로 통과
        if has_panelist_in_title:
            panelist_names = [n for n in _PANELISTS if n in title]
            item["_scan_result"]    = {"worth_deep_analysis": True, "detected_names": panelist_names, "detected_stocks": []}
            item["_detected_names"] = panelist_names
            item["_worth_deep"]     = True
            print(f"  ✅ 제목패널 [{title[:35]}] [{','.join(panelist_names)}]")
            continue

        # transcript 또는 description으로 스캔
        description = item.get("description", "") or ""
        scan_text = transcript if len(transcript) >= _MIN_TRANSCRIPT_CHARS else description
        if len(scan_text) >= _MIN_TRANSCRIPT_CHARS:
            scan = _scan_transcript(client, scan_text, video_url)
            item["_scan_result"]    = scan
            item["_detected_names"] = scan.get("detected_names", [])
            item["_worth_deep"]     = scan.get("worth_deep_analysis", False)
            status = "✅ 통과" if item["_worth_deep"] else "⏭ 스킵"
            panelist_tag = f" [{','.join(item['_detected_names'])}]" if item["_detected_names"] else ""
            print(f"  {status} [{title[:35]}]{panelist_tag}")
            time.sleep(_SCAN_SLEEP_SEC)
        else:
            item["_scan_result"]    = {"worth_deep_analysis": False}
            item["_detected_names"] = []
            item["_worth_deep"]     = False

    # ── 2단계 대상 선별 (최대 5개, 우선순위 적용) ────────────────────────────
    passed   = [i for i in scannable if i.get("_worth_deep")]
    priority = []

    # 1순위: 제목에 패널리스트 실명 포함 (최대 4개)
    tier1 = [i for i in passed if i.get("_panelist_in_title")][:_DEEP_TIER1_MAX]
    priority.extend(tier1)
    used_urls = {i.get("link") for i in tier1}

    # 2순위: 나머지 스캔 통과 영상 전체 (유튜브/경제방송/증권사 모두, 최대 3개)
    tier2 = [
        i for i in passed
        if i.get("link") not in used_urls
    ][:_DEEP_TIER2_MAX]
    priority.extend(tier2)

    print(f"\n[GeminiYT] 2단계 심층 분석 대상: {len(priority)}개 "
          f"(1순위 패널리스트:{len(tier1)}개, 2순위 스캔통과:{len(tier2)}개)")

    # ── 2단계: 심층 분석 (영상 직접분석) ─────────────────────────────────────
    deep_done  = 0
    deep_fail  = 0
    deep_urls  = {i.get("link") for i in priority}

    for item in priority:
        video_url = item.get("link", "")
        title     = item.get("title", "")

        result = None
        try:
            result = _analyze_via_video_url(client, video_url)
        except Exception as e:
            print(f"  ❌ [{title[:30]}] 심층분석 예외: {e}")

        if result:
            item["gemini_summary"]  = result.get("video_summary", "")
            item["gemini_speaker"]  = result.get("main_speaker", "")
            item["gemini_speakers"] = result.get("speakers", [])
            item["gemini_mentions"] = result.get("mentions", [])
            item["gemini_analyzed"] = True
            deep_done += 1
            print(f"  ✅ [{title[:30]}] → 종목 언급 {len(item['gemini_mentions'])}개")
        else:
            item["gemini_analyzed"] = False
            deep_fail += 1
            print(f"  ❌ [{title[:30]}] → 심층분석 실패")

        time.sleep(_DEEP_SLEEP_SEC)

    # 2단계 미대상 항목은 스캔 결과(detected_stocks)로 간이 처리
    for item in scannable:
        if item.get("link") not in deep_urls:
            scan = item.get("_scan_result", {})
            item["gemini_summary"]  = ""
            item["gemini_speaker"]  = ", ".join(item.get("_detected_names", []))
            item["gemini_speakers"] = item.get("_detected_names", [])
            item["gemini_mentions"] = [
                {
                    "stock_name": s, "timestamp": "", "speaker": "",
                    "fact": "", "quote": "", "sentiment": "중립", "confidence": "낮음"
                }
                for s in scan.get("detected_stocks", [])
            ]
            item["gemini_analyzed"] = False

    # 스캔 불가 항목 기본값
    for item in non_scannable:
        item["gemini_summary"]  = ""
        item["gemini_speaker"]  = ""
        item["gemini_speakers"] = []
        item["gemini_mentions"] = []
        item["gemini_analyzed"] = False

    enriched = scannable + non_scannable
    print(f"\n[GeminiYT] 완료 — 심층분석 성공:{deep_done} / 실패:{deep_fail} / "
          f"스캔통과(간이):{len(passed)-len(priority)}개 / 스캔스킵:{len(non_scannable)}개")
    return enriched


# ── gemini_mentions → all_data 확장 헬퍼 ─────────────────────────────────────

def expand_gemini_mentions(enriched_items: list) -> list:
    """
    gemini_mentions에서 추출된 발언을 별도 항목으로 확장.

    변경사항:
    - fact/quote 필드 추가 (방송 제작용)
    - data/youtube_mentions.json 별도 저장 (외부 앱 연동용)
    - speakers 필드(출연자 목록) 저장
    """
    import os
    import json
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))

    expanded       = list(enriched_items)
    youtube_export = []  # 외부 앱 연동용 별도 저장 데이터

    for item in enriched_items:
        mentions = item.get("gemini_mentions", [])
        if not mentions:
            continue

        base_url    = item.get("link", "")
        source_name = item.get("source_name", "")
        source_type = item.get("source_type", "유튜브")
        published   = item.get("published", "")
        main_speaker = item.get("gemini_speaker", "")
        speakers    = item.get("gemini_speakers", [])
        video_summary = item.get("gemini_summary", "")

        for mention in mentions:
            stock_name = mention.get("stock_name", "")
            fact       = mention.get("fact", "")
            quote      = mention.get("quote", "")
            # 구버전 호환: statement가 있으면 fact/quote 폴백
            statement  = mention.get("statement", "")
            if not fact and statement:
                fact = statement
            timestamp  = mention.get("timestamp", "")
            m_speaker  = mention.get("speaker") or main_speaker
            sentiment  = mention.get("sentiment", "중립")
            confidence = mention.get("confidence", "보통")

            if not stock_name or (not fact and not quote):
                continue

            timestamp_url = f"{base_url}&t={timestamp}" if timestamp else base_url

            # 브리핑용 summary (기존 흐름 유지)
            summary_parts = []
            if m_speaker:
                summary_parts.append(f"[{m_speaker}]")
            if fact:
                summary_parts.append(fact)
            if quote:
                summary_parts.append(f'"{quote}"')
            summary = " ".join(summary_parts)
            if sentiment != "중립":
                summary += f" (감성:{sentiment})"

            expanded.append({
                "source_type":       source_type,
                "source_name":       source_name,
                "title":             f"{m_speaker or source_name}: {stock_name} 언급",
                "summary":           summary,
                "content":           fact or quote,
                "link":              timestamp_url,
                "url":               timestamp_url,
                "published":         published,
                "stock_name":        stock_name,
                "gemini_speaker":    m_speaker,
                "gemini_fact":       fact,
                "gemini_quote":      quote,
                "gemini_sentiment":  sentiment,
                "gemini_confidence": confidence,
                "_from_gemini":      True,
            })

            # 외부 앱 연동용 데이터 축적
            youtube_export.append({
                "date":          datetime.now(KST).strftime("%Y-%m-%d"),
                "channel":       source_name,
                "video_url":     base_url,
                "video_title":   item.get("title", ""),
                "video_summary": video_summary,
                "published":     published,
                "speakers":      speakers,
                "main_speaker":  main_speaker,
                "stock_name":    stock_name,
                "timestamp":     timestamp,
                "timestamp_url": timestamp_url,
                "speaker":       m_speaker,
                "fact":          fact,
                "quote":         quote,
                "sentiment":     sentiment,
                "confidence":    confidence,
            })

    # 외부 앱 연동용 JSON 저장
    if youtube_export:
        os.makedirs("data", exist_ok=True)
        export_path = "data/youtube_mentions.json"
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(youtube_export, f, ensure_ascii=False, indent=2)
            print(f"[GeminiYT] 방송제작용 데이터 저장: {export_path} ({len(youtube_export)}건)")
        except Exception as e:
            print(f"[GeminiYT] 방송제작용 데이터 저장 실패: {e}")

    original_count = len(enriched_items)
    expanded_count = len(expanded) - original_count
    print(f"[GeminiYT] 발언 확장: {expanded_count}개 항목 추가 "
          f"(원본 {original_count}개 유지)")
    return expanded
