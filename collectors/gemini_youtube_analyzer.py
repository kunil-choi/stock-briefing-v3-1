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
- GEMINI-YT-7  : 타겟 영상 심층분석을 순차(for-loop) → 스레드풀 병렬 처리로
                 변경. 영상 9~10개를 순차로 돌리면 전체 파이프라인 소요시간의
                 60%대(15~19분)를 이 단계 하나가 차지했음(2026-08-09,
                 2026-08-11 실행 로그로 확인). 영상별 호출이 서로 독립적인
                 네트워크 요청이라 _DEEP_ANALYSIS_CONCURRENCY개씩 동시 처리로
                 바꿔 가장 느린 영상 하나의 소요시간 수준까지 단축한다.
"""

import concurrent.futures
import json
import re
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

# GEMINI-YT-7: 영상 1개당 Gemini 응답이 1.5~4.5분 걸려 순차 처리 시 영상
# 9~10개에 15~19분이 소요됐음(전체 파이프라인 소요시간의 60%대 비중).
# 각 호출은 서로 독립적인 네트워크 요청이라 동시에 처리해도 무방하므로,
# 이 값만큼 스레드풀로 병렬 처리한다 (레이트리밋 여유를 위해 동시 실행
# 개수 자체를 제한 — 무제한 동시 요청 대신).
_DEEP_ANALYSIS_CONCURRENCY = 4

# ── 심층 분석 프롬프트 (영상 직접분석) ────────────────────────────────────────
# SELECT-CRITERIA-1: 패널 발언은 아래 5가지 기준에 해당하는 것만 뽑는다.
#   1) 해당 종목의 최근 주가 움직임에 대한 이유 분석/근거
#   2) 향후 해당 종목의 주가 방향 예상
#   3) 향후 관련 섹터에 대한 전망
#   4) 시청자가 알아야 할, 해당 기업과 관련된 이벤트
#   5) 해당 종목의 주가에 영향을 미칠만한 주요 이슈
# MERGE-SPEAKER-1: 같은 발언자가 같은 종목에 대해 영상 안에서 여러 차례
# 말하더라도 mentions에는 그 발언자·종목 조합으로 항목을 하나만 만든다
# (여러 발언 내용은 하나의 insight 문장/여러 문장으로 통합).
# REPHRASE-1: 원문을 그대로 옮겨적지 말고, 발언자의 의도가 정확히 드러나도록
# 시청자가 이해하기 쉬운 문장으로 재구성한다(핵심 근거·수치는 유지).
# NO-ANALOGY-1: 2026-08-18, KBS 1라디오 우주산업 특집에서 "삼성전자와 같은
# 국내 기업은 익숙하지만 우주산업은 아직 생소하다"는 도입부 비유 문장이,
# 문장 속 예시로 등장한 삼성전자·SK하이닉스·유진투자증권 각각의 mentions로
# 개별 추출돼, 정작 그 종목들과 무관한 우주산업 소개 발언이 삼성전자 카드의
# "패널 발언"으로 노출되는 사고가 있었다. 종목명이 문장에 등장했다는 사실
# 만으로는 그 종목에 대한 실질적 코멘트가 아니다 — 다른 주제(신산업/섹터/
# 시장 전반)를 설명하려고 예시·비유로 종목명을 잠깐 든 경우는 그 종목의
# insight로 추출하지 않는다.
_PROMPT_VIDEO = """
이 유튜브 영상을 분석하여 주식 종목 언급을 추출하세요.
방송 제작용 데이터로 사용되므로, 정확성이 최우선입니다.

[선정 기준 — 아래 중 하나 이상에 해당하는 발언만 포함]
1. 해당 종목의 최근 주가 움직임에 대한 이유 분석이나 근거
2. 향후 해당 종목의 주가 방향에 대한 예상
3. 향후 관련 섹터에 대한 전망
4. 해당 기업과 관련해 시청자가 알아야 할 이벤트(신제품, 실적, 계약 등)
5. 해당 종목의 주가에 영향을 미칠만한 주요 이슈
- 위 기준에 해당하지 않는 단순 종목명 언급, 잡담성 언급은 제외
- 영상에서 확인되지 않은 내용 절대 추가 금지

[예시·비유 제외 규칙 — 중요]
- 발언자가 다른 주제(예: 새로운 산업/섹터, 시장 전반 분위기, 일반적인 투자
  원칙)를 설명하면서 "OO전자와 같은 친숙한 기업은…", "OO처럼 예를 들면…"
  식으로 종목명을 예시·비유로만 잠깐 언급한 경우, 그 종목 자체에 대한
  실질적 코멘트가 아니므로 mentions에 포함하지 마세요.
- stock_name으로 넣으려면, 그 발언의 실제 주제가 해당 종목(또는 그 종목이
  속한 섹터) 자체여야 합니다. "이 종목이 예시로 등장했다"와 "이 종목에
  대한 코멘트다"를 구분하세요.

[발언자 확인]
- 화면 하단 자막(이름/소속)을 최우선으로 확인
- 특정 불가하면 speaker를 빈 문자열로

[통합 규칙]
- 같은 발언자가 같은 종목에 대해 영상 중 여러 시점에 언급했다면, 별도
  항목으로 나누지 말고 mentions에 그 발언자·종목 조합으로 하나의 항목만
  만들어 핵심 내용을 모아서 정리하세요.

[문장 재구성 규칙]
- insight는 발언을 그대로 옮긴 문장(verbatim)이 아니라, 발언자의 의도를
  정확히 반영하면서 시청자가 한 번에 이해할 수 있도록 자연스럽게 다시 쓴
  문장이어야 합니다. 위 5가지 선정 기준 중 해당하는 내용(이유/전망/이벤트/
  이슈)과 구체적 수치·근거를 포함해 1~2문장으로 작성하세요.
- "~에 대해 논의했다", "~섹터를 제시했다"처럼 무엇을 다뤘는지만 말하고
  실제로 뭐라고 말했는지는 밝히지 않는 두루뭉술한 문장은 금지합니다.
  실제 언급된 구체적 내용(예: 어떤 섹터/종목명이었는지, 수치가 얼마였는지)
  까지 반드시 포함하세요(나쁜 예: "반도체 다음 수급이 몰릴 섹터를 제시했다"
  → 좋은 예: "반도체 다음으로는 2차전지·바이오로 수급이 이동할 것으로
  전망했다"). 영상에서 그 구체적 내용을 확인할 수 없다면 추측해서 채우지
  말고 해당 mention 자체를 포함하지 마세요.

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
      "insight": "위 [문장 재구성 규칙]에 따라 재구성한 핵심 내용 1~2문장 (예: 3분기 실적 호조와 신제품 출시 기대감이 겹치며 반등했고, 목표주가는 10만원으로 제시했다)",
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

    print(f"[GeminiYT] 타겟 심층분석 대상: {len(video_urls)}개 "
          f"(동시 {_DEEP_ANALYSIS_CONCURRENCY}개씩 병렬 처리)")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_DEEP_ANALYSIS_CONCURRENCY
    ) as executor:
        future_to_url = {
            executor.submit(_analyze_via_video_url, client, video_url): video_url
            for video_url in video_urls
        }
        for future in concurrent.futures.as_completed(future_to_url):
            video_url = future_to_url[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"  ❌ [{video_url}] 심층분석 예외: {e}")
                result = None

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

    print(f"[GeminiYT] 타겟 심층분석 완료 — 성공:{done} / 실패:{fail}")
    return results
