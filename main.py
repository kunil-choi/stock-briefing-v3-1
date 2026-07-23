# main.py
"""
stock-briefing-v3-1 — morning_core 영상용 "장전" 데이터 파이프라인

stock-briefing-v3의 main.py를 베이스로 하되, 애널리스트 리포트 수집 단계(08:00
KST 대기 ~ 08:30 강제진행 루프)를 제거해 07:10~08:20 KST 창 안에서 안정적으로
끝나도록 만든 버전이다. v3 원본은 수정하지 않고 이 레포에 독립적으로 복사·유지한다.

산출물:
- data/briefing_data.json : brokerage_reports가 비어있는 버전 (stock-briefing-step1이
  raw.githubusercontent.com으로 직접 소비)
- data/raw_YYYYMMDD.json  : 수집 원본 all_data 전체 (stock-briefing-v3-2가 재사용 —
  이 레포의 .gitignore는 v3와 달리 이 파일을 커밋 대상에서 제외하지 않는다)
- docs/index.html         : GitHub Pages 프리뷰 페이지(사람이 눈으로 데이터를
  확인하기 위한 용도). stock-briefing-v3의 공개 브리핑 사이트를 대체하는 게
  아니라 별도 프리뷰다 — v3는 현재 자동 실행이 중단된 상태로 그대로 유지된다.

완료 후 GH_TOKEN으로 stock-briefing-step1(morning_core.yml)과
stock-briefing-v3-2(main.yml)를 workflow_dispatch로 트리거한다.
"""
import os
import re
import json
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import checkpoint
from config import (
    ANTHROPIC_API_KEY, YOUTUBE_API_KEY, GH_TOKEN, GITHUB_REPO,
    GEMINI_API_KEY,
    NEWS_RSS_FEEDS, load_channels,
)
from collectors.news_collector    import collect_news
from collectors.youtube_collector import (
    get_youtube_client,
    collect_section1_youtube,
    collect_panelist_youtube,
    _PANELIST_HOURS,
)
from analyzer.ai_analyzer import analyze_and_generate_html

KST = ZoneInfo("Asia/Seoul")

# 이 레포는 아직 "정식 V3 공개 사이트"가 아니라, morning_core(step1) 영상 제작에
# 쓰인 데이터를 사람이 눈으로 확인할 수 있게 하는 프리뷰 페이지다. 나중에
# step1/step2 영상이 실제로 업로드되기 시작하면 stock-briefing-v3가 다시
# 정식 공개 사이트 역할을 맡을 계획이므로, 공유 모듈(analyzer/html_generator.py는
# v3/v3-2와 바이트 단위로 동일하게 유지)은 건드리지 않고 반환된 HTML 문자열에만
# 타이틀/배지를 얹고 애널리스트 리포트 섹션(V3_1은 데이터가 항상 비어있음)을
# 잘라내는 방식으로 구분한다.
_PREVIEW_TITLE = "주식 시장 AI 프리뷰"


def _label_preview_html(html: str) -> str:
    html = html.replace(
        "<title>AI 주식 브리핑",
        f"<title>{_PREVIEW_TITLE}",
        1,
    )
    html = html.replace(
        "<h1>📈 AI 주식 브리핑</h1>",
        f"<h1>📈 {_PREVIEW_TITLE}</h1>",
        1,
    )

    # FIX-BADGE-1: 상단 전체폭 경고 배너 대신, 헤더 우측 끝에 작은 사각형
    # 배지로 "정식 브리핑은 오전 10시에 별도 업데이트된다"만 짧게 안내한다.
    badge = (
        '<a href="https://kunil-choi.github.io/stock-briefing-v3/" '
        'target="_blank" rel="noopener" style="flex-shrink:0;background:#1f2937;'
        'border:1px solid #374151;border-radius:8px;padding:8px 14px;'
        'text-align:center;font-size:12px;line-height:1.4;color:#fbbf24;'
        'text-decoration:none;font-weight:700;white-space:nowrap;">'
        "주식시장 AI 브리핑<br>오전 10시 업데이트 예정"
        "</a>"
    )
    html = re.sub(
        r'(<h1>📈 주식 시장 AI 프리뷰</h1>\s*<div class="subtitle">.*?</div>)',
        r'<div style="text-align:left;">\1</div>' + badge,
        html,
        count=1,
        flags=re.DOTALL,
    )
    html = html.replace(
        '<div class="briefing-header">',
        '<div class="briefing-header" style="display:flex;align-items:center;'
        'justify-content:space-between;flex-wrap:wrap;gap:12px;text-align:left;">',
        1,
    )

    # FIX-NO-ANALYST-1: V3_1은 애널리스트 리포트를 아예 수집하지 않으므로
    # "오늘의 증권사 리포트" 섹션은 언제나 "데이터 없음"만 표시된다 — 통째로 제거.
    html = re.sub(
        r'\n\s*<div class="section">\s*<div class="section-title">'
        r'📋 오늘의 증권사 리포트</div>.*?'
        r'(?=\n\s*<div class="section">\s*<div class="section-title">🤖 AI 투자 전략</div>)',
        "",
        html,
        count=1,
        flags=re.DOTALL,
    )
    return html


def safe_collect(fn, *args, label="", **kwargs):
    try:
        result = fn(*args, **kwargs)
        return result if result else []
    except Exception as e:
        print(f"  [{label}] 수집 중 오류: {e}")
        return []


def main():
    now_kst    = datetime.now(KST)
    print(f"=== V3_1 morning_core 데이터 생성 시작: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')} ===")
    start_time = now_kst.timestamp()

    # ── 체크포인트 정리/확인 ────────────────────────────────────────────────
    # RESUME-1: 이전 실행이 타임아웃/취소로 죽은 뒤 코드 수정 후 재실행하는
    # 경우, 오늘 브리핑이 이미 끝까지 완료돼 커밋됐다면(DONE 마커) 처음부터
    # 다시 돌 필요가 없다. 반대로 완료되지 않았다면 아래에서 단계별로 이미
    # 끝난 수집 단계는 건너뛰고 남은 단계만 이어서 진행한다.
    checkpoint.prune_old()
    if checkpoint.is_done():
        print("  ✅ 오늘 브리핑 이미 완료됨(체크포인트 DONE) → 종료")
        return

    SKIP_YOUTUBE = os.getenv("SKIP_YOUTUBE", "false").lower() == "true"
    if SKIP_YOUTUBE: print("  ⚡ SKIP_YOUTUBE=true → 유튜브 수집/분석 스킵")

    # ── API 키 확인 ────────────────────────────────────────────────────────
    print("\n[API 키 확인]")
    keys = {
        "ANTHROPIC": ANTHROPIC_API_KEY,
        "YOUTUBE":   YOUTUBE_API_KEY,
        "GH_TOKEN":  GH_TOKEN,
        "GEMINI":    GEMINI_API_KEY,
    }
    all_ok = True
    for name, val in keys.items():
        if val:
            print(f"  {name}: ✅")
        else:
            print(f"  {name}: ❌ 없음")
            if name not in ("GEMINI",):
                all_ok = False
    print(f"  {'정상 동작' if all_ok else '일부 키 없음'}")

    # ── 채널 로드 ──────────────────────────────────────────────────────────
    print("\n[채널 로드]")
    channels = load_channels()
    for cat in ["broadcast", "youtuber", "securities"]:
        items = channels.get(cat, [])
        valid = [c for c in items if isinstance(c, dict) and c.get("id")]
        print(f"  {cat}: 전체 {len(items)}개 / 유효 ID {len(valid)}개")

    all_data = []

    # ── 1. 시장 데이터 ─────────────────────────────────────────────────────
    print("\n[시장 데이터 수집]")
    market_overview = checkpoint.load_stage("market")
    if market_overview is not None:
        print("  [재개] 체크포인트에서 로드 → 재수집 스킵")
    else:
        try:
            from collectors.market_collector import collect_market_overview
            market_overview = collect_market_overview()
        except Exception as e:
            print(f"  [시장데이터 수집 실패] {e}")
            market_overview = {}
        checkpoint.save_stage("market", market_overview)

    # ── 2. 뉴스 RSS ────────────────────────────────────────────────────────
    print("\n[1/3] 뉴스 RSS 수집...")
    news_data = checkpoint.load_stage("news")
    if news_data is not None:
        print(f"  [재개] 체크포인트에서 로드 ({len(news_data)}건) → 재수집 스킵")
    else:
        news_data = safe_collect(collect_news, NEWS_RSS_FEEDS, label="뉴스")
        checkpoint.save_stage("news", news_data)
    all_data.extend(news_data)
    print(f"  → {len(news_data)}건")

    # ── YouTube 클라이언트 ─────────────────────────────────────────────────
    youtube = get_youtube_client(YOUTUBE_API_KEY)

    # ── 3. 등록 채널 플레이리스트 수집 ────────────────────────────────────
    yt_data = []
    panelist_data = []
    if SKIP_YOUTUBE:
        print("\n[2/3] 유튜브 수집 스킵 (SKIP_YOUTUBE=true)")
    else:
        print("\n[2/3] 유튜브 수집 (경제방송/유튜버/증권사 24h)...")
        yt_data = checkpoint.load_stage("youtube_section1")
        if yt_data is not None:
            print(f"  [재개] 체크포인트에서 로드 ({len(yt_data)}건) → 재수집 스킵")
        elif youtube:
            yt_data = safe_collect(
                collect_section1_youtube, youtube, channels, label="유튜브"
            )
            print(f"  → {len(yt_data)}건")
            checkpoint.save_stage("youtube_section1", yt_data)
        else:
            yt_data = []
            print("  → YouTube 클라이언트 없음, 스킵")

        print(f"\n[3/3] 패널리스트 이름 검색 수집 ({_PANELIST_HOURS}h)...")
        panelist_data = checkpoint.load_stage("youtube_panelist")
        if panelist_data is not None:
            print(f"  [재개] 체크포인트에서 로드 ({len(panelist_data)}건) → 재수집 스킵")
        elif youtube:
            panelist_data = safe_collect(
                collect_panelist_youtube, youtube, label="패널리스트검색"
            )
            print(f"  → {len(panelist_data)}건")
            checkpoint.save_stage("youtube_panelist", panelist_data)
        else:
            panelist_data = []
            print("  → YouTube 클라이언트 없음, 스킵")

    # ── 유튜브 원본 데이터 추가 ────────────────────────────────────────────
    # GEMINI-YT-6: Gemini 영상 직접분석은 더 이상 여기서(종목 선정 전에)
    # 하지 않는다. all_data는 텍스트 매칭만으로 종목 점수를 매기는 데 쓰이고,
    # 종목 선정이 끝난 뒤 analyze_and_generate_html() 내부에서 선정된 종목에
    # 실제로 연결된 영상만 골라 Gemini로 심층분석한다 (analyzer/ai_analyzer.py
    # 의 gather_target_videos()/build_panelist_quotes() 참고).
    youtube_raw = yt_data + panelist_data
    all_data.extend(youtube_raw)

    # ── 애널리스트 리포트 수집 없음 (V3_1의 핵심 차이점) ──────────────────
    # morning_core 영상은 증권사 리포트를 다루지 않으므로, 08:00 대기~08:30 강제
    # 진행 루프(v3 main.py 참고)를 이 레포에는 넣지 않는다. 그 결과 all_data에는
    # source_type=="애널리스트" 항목이 전혀 없고, ai_analyzer.build_brokerage_reports()가
    # 자연히 빈 결과를 만들어 brokerage_reports가 비어있는 briefing_data.json이 나온다.

    # ── 수집 요약 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(f"총 수집: {len(all_data)}건")
    type_counts = {}
    for d in all_data:
        t = d.get("source_type", "기타")
        type_counts[t] = type_counts.get(t, 0) + 1
    print("\n[수집 유형 요약]")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}건")

    # ── 원본 저장 (V3_2가 raw.githubusercontent.com으로 재사용) ───────────
    os.makedirs("data", exist_ok=True)
    today_str = now_kst.strftime("%Y%m%d")
    with open(f"data/raw_{today_str}.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[저장] data/raw_{today_str}.json 저장 (V3_2 재사용용)")

    # ── 아카이브 (docs/index.html을 덮어쓰기 전에 오늘자 이전 버전을 보관) ──
    os.makedirs("docs/archive", exist_ok=True)
    existing_index = "docs/index.html"
    if os.path.exists(existing_index):
        archive_date = now_kst.strftime("%Y-%m-%d")
        archive_path = f"docs/archive/{archive_date}.html"
        if not os.path.exists(archive_path):
            shutil.copy2(existing_index, archive_path)
            print(f"[아카이브] 저장: {archive_path}")

    # ── AI 분석 (Claude) — data/briefing_data.json은 이 호출 내부에서 저장됨 ──
    print("\n[AI 분석] Claude 분석 + Gemini 검수 시작...")
    try:
        html = analyze_and_generate_html(
            all_data,
            channels_data=channels,
            gh_repo=GITHUB_REPO,
            gh_token=GH_TOKEN,
            market_overview=market_overview,
        )
    except Exception as e:
        print(f"[AI 분석 실패] {e}")
        raise

    # ── HTML 저장 (프리뷰 배너를 얹어 docs/index.html로 공개) ──────────────
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(_label_preview_html(html))
    print("[저장] docs/index.html 저장 (GitHub Pages 프리뷰)")

    # ── RESUME-1: 완료 마커 커밋 ──────────────────────────────────────────
    # 여기까지 왔다면 오늘 브리핑이 끝까지 완성된 것. DONE 마커를 최종
    # 산출물과 함께 커밋해두면, 이후 재실행(예: 사람이 실수로 다시 트리거)
    # 시 처음부터 다시 돌지 않고 바로 종료한다. 워크플로우의 마지막 커밋
    # 스텝은 이후 변경사항이 없어 스킵되는 게 정상이다.
    checkpoint.mark_done([
        "data/briefing_data.json",
        f"data/raw_{today_str}.json",
        "docs/index.html",
    ])

    elapsed = datetime.now(KST).timestamp() - start_time
    print(f"\n✅ V3_1 데이터 생성 완료 → data/briefing_data.json, data/raw_{today_str}.json, docs/index.html")
    print(f"=== 완료: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')} "
          f"(소요: {elapsed:.0f}초) ===")


if __name__ == "__main__":
    main()
