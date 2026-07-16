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
import json
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

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
# 배너/타이틀을 얹어 구분한다.
_PREVIEW_LABEL = "V3-1 프리뷰 · 증권사 리포트 제외 이른 스냅샷 (step1 영상 제작용 데이터)"


def _label_preview_html(html: str) -> str:
    html = html.replace(
        "<title>AI 주식 브리핑",
        "<title>[V3-1 프리뷰] AI 주식 브리핑",
        1,
    )
    banner = (
        '<div style="background:#111827;color:#fbbf24;text-align:center;'
        'padding:10px 16px;font-weight:700;font-size:14px;">'
        f"⚠️ {_PREVIEW_LABEL} — "
        '<a href="https://kunil-choi.github.io/stock-briefing-v3/" '
        'style="color:#fff;text-decoration:underline;">정식 공개 브리핑은 여기</a>'
        "</div>"
    )
    return html.replace('<div class="briefing-header">', banner + '<div class="briefing-header">', 1)


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
    try:
        from collectors.market_collector import collect_market_overview
        market_overview = collect_market_overview()
    except Exception as e:
        print(f"  [시장데이터 수집 실패] {e}")
        market_overview = {}

    # ── 2. 뉴스 RSS ────────────────────────────────────────────────────────
    print("\n[1/3] 뉴스 RSS 수집...")
    news_data = safe_collect(collect_news, NEWS_RSS_FEEDS, label="뉴스")
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
        if youtube:
            yt_data = safe_collect(
                collect_section1_youtube, youtube, channels, label="유튜브"
            )
            print(f"  → {len(yt_data)}건")
        else:
            print("  → YouTube 클라이언트 없음, 스킵")

        print(f"\n[3/3] 패널리스트 이름 검색 수집 ({_PANELIST_HOURS}h)...")
        if youtube:
            panelist_data = safe_collect(
                collect_panelist_youtube, youtube, label="패널리스트검색"
            )
            print(f"  → {len(panelist_data)}건")
        else:
            print("  → YouTube 클라이언트 없음, 스킵")

    # ── GEMINI: 유튜브 영상 직접 분석 (v3의 GEMINI-MAIN 로직 동일) ────────
    youtube_raw = yt_data + panelist_data
    if SKIP_YOUTUBE:
        all_data.extend(youtube_raw)
    elif GEMINI_API_KEY and youtube_raw:
        try:
            from collectors.gemini_youtube_analyzer import (
                analyze_youtube_items,
                expand_gemini_mentions,
            )
            print(f"\n[GEMINI] 유튜브 영상 분석 시작 ({len(youtube_raw)}개)...")
            enriched = analyze_youtube_items(youtube_raw, GEMINI_API_KEY)
            expanded = expand_gemini_mentions(enriched)

            analyzed_urls = {item.get("link", "") for item in youtube_raw}
            for item in youtube_raw:
                if item.get("link", "") not in {e.get("link", "") for e in expanded}:
                    expanded.append(item)

            all_data.extend(expanded)
            print(f"  → Gemini 분석 완료: {len(expanded)}건 (원본+발언 확장 포함)")
        except Exception as e:
            print(f"  [GEMINI] 유튜브 분석 실패 (기존 데이터로 계속 진행): {e}")
            all_data.extend(youtube_raw)
    else:
        all_data.extend(youtube_raw)
        if not GEMINI_API_KEY:
            print("\n[GEMINI] API 키 없음 → 유튜브 영상 분석 스킵")

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

    elapsed = datetime.now(KST).timestamp() - start_time
    print(f"\n✅ V3_1 데이터 생성 완료 → data/briefing_data.json, data/raw_{today_str}.json, docs/index.html")
    print(f"=== 완료: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')} "
          f"(소요: {elapsed:.0f}초) ===")


if __name__ == "__main__":
    main()
