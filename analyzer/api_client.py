# analyzer/api_client.py
"""
Claude API 호출 클라이언트 - 재시도 로직 포함
BUG-CR-1: call_claude_with_retry 시그니처를 (prompt, api_key, ...) 순서로 수정
          기존 (api_key, prompt, ...) 순서에서 변경 → ai_analyzer.py, validation.py 호출부와 일치
"""

import time
import anthropic

# 기본 모델명 상수 (최신 claude-sonnet-4-6 사용)
DEFAULT_MODEL = "claude-sonnet-4-6"
# FIX-MAXTOK-1: 16000 → 24000. 관심종목 10개+주도주2개+히든픽 항목이 많은 날은
# 응답이 16000 토큰을 넘겨 JSON이 중간에 잘리고(stop_reason=max_tokens) 파싱이
# 항상 실패하는 문제가 있었다. max_tokens>16000은 SDK가 non-streaming 요청을
# 거부할 수 있어 streaming으로 전환한다.
DEFAULT_MAX_TOKENS = 24000
MAX_ALLOWED_TOKENS = 64000  # 잘림 감지 시 재시도에서 늘릴 수 있는 상한
DEFAULT_RETRIES = 3
DEFAULT_DELAY = 5  # 초


def call_claude_with_retry(
    prompt: str,                      # BUG-CR-1: 첫 번째 인자 (기존엔 api_key가 첫 번째였음)
    api_key: str = "",                # BUG-CR-1: 두 번째 인자
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = DEFAULT_MODEL,
    retries: int = DEFAULT_RETRIES,
    delay: int = DEFAULT_DELAY,
    system_prompt: str = "",
) -> str:
    """
    Claude API를 호출하고 응답 텍스트를 반환합니다.
    실패 시 최대 retries 횟수만큼 재시도합니다.

    Args:
        prompt      : 사용자 메시지 (필수)
        api_key     : Anthropic API 키
        max_tokens  : 최대 출력 토큰 수 (기본 8000)
        model       : 사용할 Claude 모델명
        retries     : 최대 재시도 횟수 (기본 3)
        delay       : 재시도 간격 초 (기본 5)
        system_prompt: 시스템 프롬프트 (선택)

    Returns:
        Claude 응답 텍스트 문자열
    """
    if not api_key:
        raise ValueError("[API-CLIENT] api_key가 비어 있습니다. ANTHROPIC_API_KEY를 확인하세요.")

    client = anthropic.Anthropic(api_key=api_key)

    # 메시지 구성
    messages = [{"role": "user", "content": prompt}]

    last_exception = None
    # FIX-MAXTOK-1: 잘림(stop_reason=max_tokens) 감지 시 다음 시도에서 예산을 늘린다
    current_max_tokens = max_tokens

    for attempt in range(1, retries + 1):
        try:
            print(f"  [Claude API] 호출 시도 {attempt}/{retries} (model={model}, max_tokens={current_max_tokens})")

            kwargs = {
                "model": model,
                "max_tokens": current_max_tokens,
                "messages": messages,
            }
            if system_prompt:
                kwargs["system"] = system_prompt

            # FIX-MAXTOK-1: max_tokens가 커질 수 있으므로 non-streaming HTTP 타임아웃을
            # 피하기 위해 streaming으로 호출하고 get_final_message()로 전체 응답을 받는다.
            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
            text = response.content[0].text

            if response.stop_reason == "max_tokens":
                print(
                    f"  [Claude API] 응답이 max_tokens({current_max_tokens}) 한도에서 잘림 "
                    f"(응답 길이: {len(text)}자)"
                )
                if current_max_tokens < MAX_ALLOWED_TOKENS and attempt < retries:
                    current_max_tokens = min(current_max_tokens * 2, MAX_ALLOWED_TOKENS)
                    print(f"  [Claude API] max_tokens를 {current_max_tokens}로 늘려 재시도")
                    last_exception = RuntimeError(
                        f"응답이 max_tokens 한도에서 잘림 (attempt {attempt})"
                    )
                    continue
                # 더 늘릴 수 없으면 잘린 텍스트라도 반환 — 호출부에서 파싱 실패로 처리
                print("  [Claude API] max_tokens 상한 도달, 잘린 응답을 그대로 반환")

            print(f"  [Claude API] 호출 성공 (응답 길이: {len(text)}자, stop_reason={response.stop_reason})")
            return text

        except anthropic.RateLimitError as e:
            last_exception = e
            wait = delay * attempt
            print(f"  [Claude API] RateLimitError (시도 {attempt}/{retries}) → {wait}초 대기 후 재시도")
            if attempt < retries:
                time.sleep(wait)

        except anthropic.APIStatusError as e:
            last_exception = e
            print(f"  [Claude API] APIStatusError {e.status_code}: {e.message} (시도 {attempt}/{retries})")
            if e.status_code in (500, 502, 503, 529):  # 서버 오류는 재시도
                if attempt < retries:
                    time.sleep(delay)
            else:
                # 클라이언트 오류(400, 401, 403 등)는 즉시 중단
                raise

        except anthropic.APIConnectionError as e:
            last_exception = e
            print(f"  [Claude API] 연결 오류 (시도 {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(delay)

        except Exception as e:
            last_exception = e
            print(f"  [Claude API] 알 수 없는 오류 (시도 {attempt}/{retries}): {type(e).__name__}: {e}")
            if attempt < retries:
                time.sleep(delay)

    # 모든 재시도 실패
    print(f"  [Claude API] {retries}회 모두 실패. 마지막 오류: {last_exception}")
    raise last_exception or RuntimeError("Claude API 호출 실패 (원인 불명)")


def estimate_tokens(text: str) -> int:
    """
    텍스트의 대략적인 토큰 수를 추정합니다.
    한국어는 글자당 약 1.5토큰, 영어는 단어당 약 1.3토큰으로 계산.
    """
    korean_chars = sum(1 for c in text if '\uac00' <= c <= '\ud7a3')
    other_chars = len(text) - korean_chars
    return int(korean_chars * 1.5 + other_chars / 4)
