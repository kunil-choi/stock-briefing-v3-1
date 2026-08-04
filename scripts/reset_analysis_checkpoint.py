#!/usr/bin/env python3
"""
종목 선정 등 코드 수정이 필요한 심각한 오류를 고친 뒤, 오늘자 뉴스/유튜브/
시황 원본 데이터는 재사용하고 Claude 분석(+Gemini 검수) 단계만 다시 돌리기
위한 스크립트.

main.py는 각 수집 단계를 data/_checkpoint/<오늘YYYYMMDD>/<단계>.json에
캐시해두고, DONE 마커가 있으면 전체를 건너뛴다. 이 스크립트는 그 DONE
마커만 지운다 — market/news/youtube_section1/youtube_panelist 체크포인트는
그대로 두므로, 다음 실행은 뉴스 크롤링·유튜브 API 재수집(쿼터 소모) 없이
바로 Claude/Gemini 분석 단계부터 시작해 briefing_data.json / docs/index.html
을 다시 만든다. (Claude/Gemini 호출 비용은 이 단계 자체가 고치려는 대상이므로
피할 수 없다.)

사용법:
    python scripts/reset_analysis_checkpoint.py
    git add -u data/_checkpoint
    git commit -m "..."
    git push
    # 이후 GitHub Actions에서 워크플로우를 workflow_dispatch로 재실행

주의:
    당일(KST) 안에서만 쓸 것. 날짜가 바뀐 뒤 재사용하면 전날 시세/뉴스로
    분석하게 되므로, 그 경우는 체크포인트를 지우지 말고 처음부터 재수집해야 한다.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_CHECKPOINT_ROOT = "data/_checkpoint"
_STAGE_FILES = ["market.json", "news.json", "youtube_section1.json", "youtube_panelist.json"]


def main():
    today = datetime.now(KST).strftime("%Y%m%d")
    checkpoint_dir = f"{_CHECKPOINT_ROOT}/{today}"
    done_path = f"{checkpoint_dir}/DONE"

    print(f"[체크포인트] 오늘(KST) 날짜: {today}")

    if not os.path.isdir(checkpoint_dir):
        print(f"[체크포인트] {checkpoint_dir} 없음 → 재사용할 원본 데이터가 없어 "
              f"다음 실행은 어차피 전체 재수집됩니다.")
        return

    print("[체크포인트] 재사용될 원본 데이터 단계:")
    for name in _STAGE_FILES:
        path = f"{checkpoint_dir}/{name}"
        mark = "있음 (재사용)" if os.path.exists(path) else "없음 (재수집됨)"
        print(f"  - {name}: {mark}")

    if not os.path.exists(done_path):
        print(f"[체크포인트] {done_path} 없음 → 이미 재실행 가능한 상태입니다.")
        return

    os.remove(done_path)
    print(f"[체크포인트] {done_path} 삭제 완료")
    print("  다음 실행은 위 재사용 단계는 건너뛰고 Claude/Gemini 분석부터 다시 진행됩니다.")
    print("  git add -u data/_checkpoint 후 커밋·푸시하고 워크플로우를 재실행하세요.")


if __name__ == "__main__":
    main()
