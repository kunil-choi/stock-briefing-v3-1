# collectors/gemini_youtube_analyzer.py
"""
Gemini를 활용한 유튜브 영상 직접 분석 모듈

역할:
  - 이미 종목 선정이 끝난 뒤, 그 종목들과 실제로 연결된(텍스트 매칭으로
    확인된) 영상만 받아 Gemini로 영상을 직접 시청·분석
  - 발언자 / 타임스탬프 / 실제 발언 원문 / 종목명 / 감성 추출

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
- GEMINI-YT-6  : 전면 재구조화 — "수집 → 스캔 → 심층분석(최대 7개, 종목 미확정
                 상태에서 제목/스캔 기준으로 선별) → 종목 선정"이던 순서를
                 "텍스트 매칭만으로 종목 선정 → 그 종목에 실제로 연결된 영상만
                 심층분석"으로 뒤집었다. 이제 영상이 넘어오는 시점에는 이미
                 관련성이 검증돼 있으므로 1단계 스캔(worth_deep_analysis 판단)이
                 불필요해져 제거했고, 심층분석 대상 선정(패널리스트 제목/스캔
                 통과 우선순위)도 호출부(ai_analyzer.py)의 종목별 캡으로
                 대체되어 이 모듈에서는 더 이상 다루지 않는다.
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

# 영상 1개 분석 후 다음 호출까지 대기 (레이트리밋 여유)
_DEEP_SLEEP_SEC = 2.0

# ── 심층 분석 프롬프트 (영상 직접분석) ────────────────────────────────────────
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




# ── 타겟 심층 분석 ───────────────────────────────────────────────────────────
# GEMINI-YT-6: 종목 선정이 끝난 뒤 호출부(ai_analyzer.py)가 종목별 상위 영상만
# 추려서 넘겨준다. 여기서는 그 목록을 그대로 영상 직접분석에 돌리기만 하면
# 되므로, 관련성 재판단(스캔)이나 우선순위 로직이 필요 없다.

def analyze_target_videos(video_urls: list, api_key: str) -> dict:
    """
    이미 관련성이 확인된 영상 URL 목록을 받아 Gemini 영상 직접분석을 수행한다.

    반환: {video_url: {"speakers": [...], "mentions": [...]}, ...}
          (SDK/키 없음 또는 개별 영상 분석 실패 시 해당 URL은 결과에서 누락)
    """
    if not _GEMINI_AVAILABLE:
        print("[GeminiYT] google-genai SDK 없음 → 타겟 분석 스킵")
        return {}
    if not api_key:
        print("[GeminiYT] GEMINI_API_KEY 없음 → 타겟 분석 스킵")
        return {}
    if not video_urls:
        return {}

    client  = genai.Client(api_key=api_key)
    results = {}
    done    = 0
    fail    = 0

    print(f"[GeminiYT] 타겟 심층분석 대상: {len(video_urls)}개")
    for video_url in video_urls:
        result = None
        try:
            result = _analyze_via_video_url(client, video_url)
        except Exception as e:
            print(f"  ❌ [{video_url}] 심층분석 예외: {e}")

        if result:
            mentions = result.get("mentions", [])
            results[video_url] = {
                "speakers": result.get("speakers", []),
                "mentions": mentions,
            }
            done += 1
            print(f"  ✅ [{video_url}] → 종목 언급 {len(mentions)}개")
        else:
            fail += 1
            print(f"  ❌ [{video_url}] → 심층분석 실패")

        time.sleep(_DEEP_SLEEP_SEC)

    print(f"[GeminiYT] 타겟 심층분석 완료 — 성공:{done} / 실패:{fail}")
    return results
