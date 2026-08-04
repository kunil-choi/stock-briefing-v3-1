#!/usr/bin/env python3
"""
data/briefing_data.json을 손으로 수정한 뒤 docs/index.html을 그 내용과
다시 맞추기 위한 스크립트. Claude/Gemini/수집기를 전혀 호출하지 않으므로
비용이 들지 않고 몇 초 안에 끝난다.

briefing_data.json은 step1(영상 생성)이 raw.githubusercontent.com으로
직접 읽어가는 "원본"이고, docs/index.html은 사람이 보는 프리뷰 렌더링일
뿐이다 — 텍스트 오류를 고칠 때는 이 JSON을 고치는 게 본질이고, 이 스크립트는
그 수정을 프리뷰 페이지에도 반영해 둘이 어긋나지 않게 해준다.

사용법:
    1. data/briefing_data.json 을 직접 수정한다.
    2. python scripts/regenerate_preview.py
    3. git diff로 data/briefing_data.json, docs/index.html 변경분을 확인하고 커밋한다.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer.html_generator import generate_html  # noqa: E402

DATA_FILE = "data/briefing_data.json"
OUT_FILE = "docs/index.html"

# main.py의 _label_preview_html()과 동일한 로직을 그대로 옮겨왔다 — main.py를
# 직접 import하면 collectors(news/youtube)까지 딸려 들어와 무거워지고
# 불필요한 의존성(API 키 등)을 요구할 수 있어 여기서는 이 부분만 복제한다.
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

    badge = (
        '<a href="https://kunil-choi.github.io/stock-briefing-v3-2/" '
        'target="_blank" rel="noopener" style="flex-shrink:0;background:#1f2937;'
        'border:1px solid #374151;border-radius:8px;padding:8px 14px;'
        'text-align:center;font-size:12px;line-height:1.4;color:#fbbf24;'
        'text-decoration:none;font-weight:700;white-space:nowrap;">'
        "증권사 리포트 AI 분석<br>오전 10시 업데이트 예정"
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

    # V3_1은 애널리스트 리포트를 수집하지 않으므로 "오늘의 증권사 리포트"
    # 섹션은 언제나 "데이터 없음"만 표시된다 — 통째로 제거.
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


def main():
    if not os.path.exists(DATA_FILE):
        print(f"[재생성] {DATA_FILE} 없음 → 중단")
        sys.exit(1)

    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    html = generate_html(data, all_data=[])
    html = _label_preview_html(html)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[재생성] {DATA_FILE} → {OUT_FILE} 완료 (AI 호출 없음)")


if __name__ == "__main__":
    main()
