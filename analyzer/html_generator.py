# analyzer/html_generator.py
"""
AI 주식 브리핑 HTML 생성 엔진

수정 이력:
- FIX-PRICE-4   : verified_price → price/change_pct/price_label 필드로 읽기 통일
- TIER-FILTER-1 : _HP_SOURCE_META, _TAG_META에 증권사 항목 추가
- FIX-PRICE-5   : 미수집 시 "-" 표시
- FIX-OVERLAP-1 : _filter_stocks_tiered() overlap_count 재계산 조건 수정
- FIX-STAR-1    : _render_star_rating() max_score 5.0 → 20.0
- FIX-HIDDEN-SIG: filtered_hidden 조건 수정
                  "부정" 신호가 명시된 경우만 제외, 그 외(중립 포함) 모두 표시
- FIX-FILTER-HTML: _filter_stocks_tiered()를 단순 중복 제거 + 순서 유지로 변경
                   ai_analyzer가 이미 정교하게 선정·정렬한 종목을 HTML단에서
                   재필터링하지 않도록 수정 (관심종목 누락 방지)
- FIX-DISCLAIMER : 하단 투자 유의사항 문구 추가
"""

import re
import html as _he
from datetime import datetime, timedelta, timezone

KST         = timezone(timedelta(hours=9))
PARA_TITLES = ["📌 최근 흐름", "📊 주요 이슈", "🔍 핵심 포인트", "💡 전망"]
# FIX-PARA-1: "투자 포인트" + "리스크 요인" 두 단락을 "핵심 포인트" 하나로 통합.
# 기존 5단락 구조에서 긍정/부정 내용이 제목과 뒤집혀 표출되던 문제 해결.
# 프롬프트도 4단락으로 맞춰 수정 (ai_analyzer.py FIX-PARA-1 연동).

_HP_SOURCE_META = {
    "애널리스트": {"color": "#51cf66", "icon": "📊", "label": "애널리스트"},
    "경제방송TV": {"color": "#ffa94d", "icon": "📺", "label": "경제방송TV"},
    "경제방송":   {"color": "#74c0fc", "icon": "📡", "label": "경제방송"},
    "증권사":     {"color": "#cc5de8", "icon": "🏦", "label": "증권사 채널"},
}
_HP_SOURCE_DEFAULT = {"color": "#adb5bd", "icon": "📌", "label": "단독 언급"}

_INDICATOR_DEFS = [
    ("코스피",   ["kospi",  "KOSPI"]),
    ("코스닥",   ["kosdaq", "KOSDAQ"]),
    ("나스닥",   ["nasdaq", "NASDAQ"]),
    ("S&P500",   ["sp500",  "SP500", "s&p500"]),
    ("다우존스", ["dow",    "DOW",   "dow_jones"]),
    ("달러/원",  ["usd_krw", "USD_KRW", "usd"]),
]

_TAG_META = {
    "뉴스":       {"bg": "#2d3a4a", "color": "#74c0fc"},
    "경제방송":   {"bg": "#3a2d1a", "color": "#ffa94d"},
    "경제방송TV": {"bg": "#3a2d1a", "color": "#ffa94d"},
    "유튜브":     {"bg": "#2d1a3a", "color": "#cc5de8"},
    "애널리스트": {"bg": "#1a3a2d", "color": "#51cf66"},
    "증권사":     {"bg": "#2d1a3a", "color": "#cc5de8"},
}

_SIGNAL_MAP = [
    (["강력매수", "매수", "buy", "긍정", "positive"], ("signal-positive", "#51cf66", "긍정")),
    (["관망", "hold", "중립", "neutral"],             ("signal-neutral",  "#adb5bd", "중립")),
    (["매도", "sell", "부정", "negative"],            ("signal-negative", "#ff6b6b", "부정")),
]
_SIGNAL_DEFAULT = ("signal-neutral", "#adb5bd", "중립")


# ── 유틸리티 ─────────────────────────────────────────────────────────────

def _resolve_signal(signal: str) -> tuple:
    if not signal:
        return _SIGNAL_DEFAULT
    sig_l = signal.strip().lower()
    for keywords, meta in _SIGNAL_MAP:
        if any(k in sig_l for k in keywords):
            return meta
    return _SIGNAL_DEFAULT


def _safe_chart_key(prefix: str, name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9가-힣]", "_", name)
    return f"{prefix}_{safe}"


def _safe_js_str(s: str) -> str:
    s = s.replace('\r', '').replace('\n', ' ')
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key) or default)
    except (TypeError, ValueError):
        return default


def _indicator_badge(label: str, value, pct, direction: str = "",
                     is_premarket: bool = False) -> str:
    # FIX-MKT-10: 값이 없어도 카드 자체는 항상 표출 ("-" placeholder)
    if value is None:
        return (
            f'<div class="indicator-badge indicator-badge--empty">'
            f'<span class="ind-label">{label}</span>'
            f'<span class="ind-value" style="color:#666;">-</span>'
            f'<span class="ind-pct" style="color:#666;">데이터 없음</span>'
            f'</div>'
        )
    try:
        pct_num = float(pct) if pct is not None else 0.0
    except (TypeError, ValueError):
        pct_num = 0.0
    if not direction:
        direction = "up" if pct_num > 0 else "down" if pct_num < 0 else "flat"
    color_map = {"up": "#ff6b6b", "down": "#74c0fc", "flat": "#adb5bd"}
    arrow_map = {"up": "▲",       "down": "▼",        "flat": "━"}

    try:
        num = float(str(value).replace(',', '').replace(' ', ''))
        val_str = f"{num:,.0f}" if num == int(num) else f"{num:,.2f}"
    except Exception:
        val_str = str(value)

    pct_str   = f"{pct_num:+.2f}%"
    pre_label = (
        ' <span style="font-size:.65rem;color:#adb5bd;">(전일 종가)</span>'
        if is_premarket else ""
    )
    return (
        f'<div class="indicator-badge">'
        f'<span class="ind-label">{label}{pre_label}</span>'
        f'<span class="ind-value">{val_str}</span>'
        f'<span class="ind-pct" style="color:{color_map[direction]};">'
        f'{arrow_map[direction]} {pct_str}</span>'
        f'</div>'
    )


def _build_market_indicators(market_overview: dict) -> str:
    if not market_overview:
        return ('<div class="market-indicators">'
                '<p style="color:#666;font-size:.85em;">시장 데이터 없음</p></div>')
    badges = ""
    for label, key_candidates in _INDICATOR_DEFS:
        item  = None
        found = False
        for key in key_candidates:
            if key in market_overview:
                item  = market_overview.get(key)
                found = True
                break
        # FIX-MKT-10: 키 자체가 없을 때만 스킵. item이 dict이면(값 None 포함) 항상 렌더링.
        if not found or not isinstance(item, dict):
            continue
        value     = (item.get("value") or item.get("close") or
                     item.get("price") or item.get("index"))
        pct       = (item.get("change_pct") or item.get("pct") or
                     item.get("percent")    or item.get("change_percent"))
        direction = item.get("direction", "")
        is_pre    = item.get("is_premarket", False)
        badges   += _indicator_badge(label, value, pct, direction, is_pre)
    if not badges:
        return ('<div class="market-indicators">'
                '<p style="color:#666;font-size:.85em;">시장 데이터 없음</p></div>')
    return f'<div class="market-indicators">{badges}</div>'


def _render_market_summary(market_summary: str) -> str:
    if not market_summary or not market_summary.strip():
        return '<p style="color:#666;">시장 요약 데이터 없음</p>'
    paras = [
        p.strip()
        for p in re.split(r'\n{2,}|\n(?=\d+[\.\)])', market_summary.strip())
        if p.strip()
    ]
    html = ""
    for i, para in enumerate(paras):
        clean = re.sub(r'^\d+[\.\)]\s*', '', para).strip()
        title = PARA_TITLES[i] if i < len(PARA_TITLES) else f"📎 포인트 {i + 1}"
        html += (
            f'<div class="summary-block">'
            f'<div class="summary-title">{title}</div>'
            f'<p class="summary-text">{clean}</p>'
            f'</div>'
        )
    return html or f'<p style="color:#ccc;">{_he.escape(market_summary.strip())}</p>'


def _build_analyst_html(all_data: list) -> str:

    # FIX-ANALYST-COLOR-1: 카테고리별 강조색 (좌측 보더 + 증권사 배지에 사용)
    _CATEGORY_META = {
        "simultaneous": {"color": "#ff922b", "badge": "🔥 동시언급"},
        "new_coverage": {"color": "#51cf66", "badge": "🆕 신규 커버리지"},
        "single":       {"color": "#74c0fc", "badge": ""},
    }

    def _report_card(r: dict, category: str = "single") -> str:
        stock       = r.get("stock_name", "")
        title       = r.get("report_title") or r.get("title", "")
        brokers_raw = r.get("brokers") or r.get("source_name", "")
        broker      = (", ".join(brokers_raw)
                       if isinstance(brokers_raw, list) else str(brokers_raw))
        link        = r.get("link", "")
        date_str    = r.get("date", "")  # FIX-RPT-DATE-1: 날짜 표시 추가

        if not link and stock:
            enc  = stock.replace(" ", "+")
            link = (f"https://finance.naver.com/research/company_list.naver"
                    f"?searchType=keyword&keyword={enc}")

        meta       = _CATEGORY_META.get(category, _CATEGORY_META["single"])
        accent     = meta["color"]
        cat_badge  = (f'<span class="analyst-cat-badge" '
                      f'style="background:{accent}22;color:{accent};'
                      f'border:1px solid {accent}55;">{meta["badge"]}</span>'
                      if meta["badge"] else "")
        # FIX-RPT-DATE-1: 날짜 배지 추가
        date_badge = (f'<span style="font-size:.72rem;color:#868e96;margin-left:auto;">'
                      f'📅 {_he.escape(date_str)}</span>'
                      if date_str else "")
        title_html = (
            f'<a href="{link}" target="_blank" rel="noopener" '
            f'class="analyst-title-link">{_he.escape(title)}</a>'
            if link else
            f'<span class="analyst-title-text">{_he.escape(title)}</span>'
        )

        ai_summary   = r.get("ai_summary", "").strip()
        summary_html = (
            f'<p class="analyst-summary" style="color:#adb5bd;font-size:.88rem;'
            f'margin-top:.4rem;font-style:italic;">💬 {_he.escape(ai_summary)}</p>'
            if ai_summary else ""
        )

        return (
            f'<div class="analyst-card" style="border-left-color:{accent};">'
            f'<div class="analyst-card-meta" style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;">'
            f'<span class="analyst-stock" style="color:{accent};">'
            f'{_he.escape(stock)}</span>'
            f'<span class="analyst-broker" '
            f'style="background:{accent}1a;color:{accent};">'
            f'🏦 {_he.escape(broker)}</span>'
            f'{cat_badge}'
            f'{date_badge}'
            f'</div>'
            f'<div class="analyst-card-title">{title_html}</div>'
            f'{summary_html}'
            f'</div>'
        )

    analyst_items = [d for d in all_data if d.get("source_type") == "애널리스트"]
    if not analyst_items:
        return '<p style="color:#666;">애널리스트 리포트 데이터 없음</p>'

    # 오늘/어제 분리
    today_items = [r for r in analyst_items if r.get("report_day") != "yesterday"]
    yest_items  = [r for r in analyst_items if r.get("report_day") == "yesterday"]

    def _render_day_section(items: list, day_label: str, day_color: str) -> str:
        if not items:
            return ""
        simultaneous = [r for r in items if r.get("analyst_category") == "simultaneous"]
        new_cov      = [r for r in items if r.get("analyst_category") == "new_coverage"]
        single       = [r for r in items
                        if r.get("analyst_category") not in ("simultaneous", "new_coverage")]
        out = ""
        # 날짜 구분선 (어제만 표시)
        if day_label:
            out += (f'<div style="margin:1.2rem 0 .6rem;padding:.4rem .8rem;'
                    f'background:{day_color}18;border-left:3px solid {day_color};'
                    f'border-radius:4px;font-size:.82rem;font-weight:700;color:{day_color};">'
                    f'{day_label}</div>')
        if simultaneous:
            out += ('<div class="analyst-category-title" '
                    'style="border-left-color:#ff922b;color:#ff922b;">'
                    '🔥 복수 증권사 동시 언급</div>')
            for r in simultaneous[:10]:
                out += _report_card(r, "simultaneous")
        if new_cov:
            out += ('<div class="analyst-category-title" '
                    'style="border-left-color:#51cf66;color:#51cf66;">'
                    '🆕 신규 커버리지 개시</div>')
            for r in new_cov[:10]:
                out += _report_card(r, "new_coverage")
        if single:
            out += ('<div class="analyst-category-title" '
                    'style="border-left-color:#74c0fc;color:#74c0fc;">'
                    '📌 단독 언급</div>')
            for r in single[:10]:
                out += _report_card(r, "single")
        return out

    html = ""
    html += _render_day_section(today_items, "", "#ff922b")
    html += _render_day_section(yest_items,  "📅 전일 주목 리포트", "#adb5bd")
    return html or '<p style="color:#666;">분류된 리포트 없음</p>'


def _hidden_pick_source_badge(channel_type: str) -> str:
    meta = _HP_SOURCE_META.get(channel_type, _HP_SOURCE_DEFAULT)
    return (
        f'<span class="hp-source-badge" '
        f'style="background:{meta["color"]}22;color:{meta["color"]};'
        f'border:1px solid {meta["color"]}55;">'
        f'{meta["icon"]} {meta["label"]}</span>'
    )


def _render_star_rating(weighted_score, max_score: float = 20.0) -> str:
    """FIX-STAR-1: max_score 20.0 기준 별점 렌더링."""
    try:
        score = float(weighted_score)
    except (TypeError, ValueError):
        score = 0.0
    score  = max(0.0, score)
    filled = max(1, round((score / max_score) * 5)) if score > 0 else 0
    filled = min(filled, 5)
    empty  = 5 - filled
    stars  = (
        f'<span class="star filled">{"★" * filled}</span>'
        f'<span class="star empty">{"☆" * empty}</span>'
    )
    return f'<span class="star-rating">{stars}</span>'


def _render_reasons(reasons: list) -> str:
    """히든픽 전용 reasons 렌더링."""
    if not reasons:
        return ""
    items = ""
    for r in reasons:
        if isinstance(r, str):
            rd, rl, rn, rt = r.strip(), "", "", ""
        elif isinstance(r, dict):
            rd = (r.get("detail") or r.get("reason") or
                  r.get("text")   or r.get("summary", "")).strip()
            rl = r.get("source_url") or r.get("link") or r.get("url", "")
            rn = (r.get("source_name") or "").strip()
            rt = (r.get("source_type") or "").strip()
        else:
            continue
        if not rd:
            continue
        meta        = _TAG_META.get(rt, {"bg": "#2d2d44", "color": "#adb5bd"})
        source_html = (
            f'<span class="reason-source" '
            f'style="background:{meta["bg"]};color:{meta["color"]};">'
            f'{_he.escape(rn)}</span> '
        ) if rn else ""
        text_html = (
            f'<a href="{rl}" target="_blank" rel="noopener" '
            f'style="color:#adb5bd;text-decoration:none;">'
            f'{_he.escape(rd)}</a>'
            if rl and rl.startswith(("http://", "https://"))
            else f'<span style="color:#adb5bd;">{_he.escape(rd)}</span>'
        )
        items += f'<li>{source_html}{text_html}</li>'
    return f'<ul class="reasons-list">{items}</ul>' if items else ""


def _is_negative_signal(sig) -> bool:
    """부정 신호 여부만 판별 (FIX-HIDDEN-SIG 용)."""
    if not sig:
        return False
    sig_l = str(sig).strip().lower()
    return any(k in sig_l for k in ("부정", "매도", "sell", "negative"))


def _is_positive_signal(sig) -> bool:
    if not sig:
        return False
    sig_l = str(sig).strip().lower()
    return any(k in sig_l for k in ("긍정", "매수", "강력", "positive", "buy"))


def _render_stock_detail(stock: dict) -> str:
    """종목 카드 상세 렌더링."""
    html = ""

    summary = (stock.get("summary") or stock.get("description") or "").strip()
    if summary:
        html += (
            f'<div class="stock-section">'
            f'<span class="stock-section-label">📋 종목 요약</span>'
            f'<p class="stock-section-text">{_he.escape(summary)}</p>'
            f'</div>'
        )

    catalyst = (stock.get("catalyst") or stock.get("price_trend") or "").strip()
    if catalyst:
        html += (
            f'<div class="stock-section">'
            f'<span class="stock-section-label">🚀 상승 촉매</span>'
            f'<p class="stock-section-text">{_he.escape(catalyst)}</p>'
            f'</div>'
        )

    risk = (stock.get("risk") or "").strip()
    if risk:
        html += (
            f'<div class="stock-section">'
            f'<span class="stock-section-label">⚠️ 리스크</span>'
            f'<p class="stock-section-text">{_he.escape(risk)}</p>'
            f'</div>'
        )

    pq_list = stock.get("panelist_quotes", [])
    if pq_list:
        pq_items = ""
        for pq in pq_list:
            speaker = (pq.get("speaker_name") or "").strip()
            source  = (pq.get("source_name") or "").strip()
            quote   = (pq.get("quote") or "").strip()
            fact    = (pq.get("fact") or "").strip()
            url     = pq.get("source_url", "")
            body    = f'"{quote}"' if quote else fact
            speaker_label = f"{speaker} · {source}" if source else speaker
            body_html = (
                f'<a href="{url}" target="_blank" rel="noopener" '
                f'style="color:#adb5bd;text-decoration:none;">{_he.escape(body)}</a>'
                if url and url.startswith(("http://", "https://"))
                else f'<span style="color:#8b949e;">{_he.escape(body)}</span>'
            )
            fact_html = (
                f'<div style="font-size:.78rem;color:#868e96;margin-top:.15rem;">'
                f'{_he.escape(fact)}</div>'
                if fact and quote else ""
            )
            pq_items += (
                f'<li><span style="color:#ffd43b;font-weight:700;">'
                f'{_he.escape(speaker_label)}</span> {body_html}{fact_html}</li>'
            )
        html += (
            f'<div class="stock-section">'
            f'<span class="stock-section-label">🎙️ 패널 발언</span>'
            f'<ul class="reasons-list">{pq_items}</ul>'
            f'</div>'
        )

    cm_list = stock.get("channel_mentions", [])
    if cm_list:
        cm_items = ""
        for cm in cm_list:
            stype   = cm.get("source_type", "")
            sname   = cm.get("source_name", "")
            content = cm.get("content", "")
            url     = cm.get("url", "")
            meta    = _TAG_META.get(stype, {"bg": "#2d2d44", "color": "#adb5bd"})
            name_html = (
                f'<span style="color:{meta["color"]};font-weight:600;">'
                f'{_he.escape(sname)}</span>'
            )
            text_html = (
                f'<a href="{url}" target="_blank" rel="noopener" '
                f'style="color:#adb5bd;text-decoration:none;">'
                f'{_he.escape(content)}</a>'
                if url and url.startswith(("http://", "https://"))
                else f'<span style="color:#8b949e;">{_he.escape(content)}</span>'
            )
            cm_items += f'<li>{name_html} {text_html}</li>'
        html += (
            f'<div class="stock-section">'
            f'<span class="stock-section-label">📢 채널별 언급 내용</span>'
            f'<ul class="reasons-list">{cm_items}</ul>'
            f'</div>'
        )

    return html


def _render_ai_strategy(ai_strategy: str) -> str:
    if not ai_strategy or not ai_strategy.strip():
        return '<p style="color:#666;">AI 전략 데이터 없음</p>'

    # 1차: ■ 구분자로 파싱 시도
    raw_sections = re.split(r'\n(?=■ )', ai_strategy.strip())
    sections     = [s.strip() for s in raw_sections if s.strip().startswith("■")]

    # 2차 fallback: ## 헤더 방식 (Claude가 마크다운으로 응답한 경우)
    if not sections:
        raw_sections = re.split(r'\n(?=## )', ai_strategy.strip())
        sections = []
        for s in raw_sections:
            s = s.strip()
            if s.startswith("## "):
                s = "■ " + s[3:]  # ## → ■ 로 변환해서 기존 렌더링 재사용
                sections.append(s)

    # 3차 fallback: 번호 목록 방식 (1. 2. 3. …)
    if not sections:
        raw_sections = re.split(r'\n(?=\d+\. )', ai_strategy.strip())
        sections = []
        for s in raw_sections:
            s = s.strip()
            if re.match(r'^\d+\.', s):
                s = "■ " + re.sub(r'^\d+\.\s*', '', s)
                sections.append(s)

    # 모든 fallback 실패 시 원문 그대로 표시
    if not sections:
        return f'<p style="color:#ccc;white-space:pre-wrap;">{_he.escape(ai_strategy.strip())}</p>'

    icon_map = {
        "핵심 시나리오":        "🎯",
        "섹터 로테이션":        "🔄",
        "오늘의 주목 포인트":   "📌",
        "리스크 시나리오":      "⚠️",
        "애널리스트 종합 시각": "📊",
    }

    html = ""
    for sec in sections:
        lines      = sec.split("\n")
        title_line = lines[0].replace("■ ", "").strip()
        body_lines = [l.strip() for l in lines[1:] if l.strip()]
        icon       = next((v for k, v in icon_map.items() if k in title_line), "📌")

        body_html = ""
        for line in body_lines:
            escaped = _he.escape(line)
            if line.startswith("•") or line.startswith("["):
                body_html += f'<div class="strat-item">{escaped}</div>'
            else:
                body_html += f'<p class="strat-text">{escaped}</p>'

        html += (
            f'<div class="strat-section">'
            f'<div class="strat-title">{icon} {_he.escape(title_line)}</div>'
            f'<div class="strat-body">{body_html}</div>'
            f'</div>'
        )

    return html


def _filter_stocks_tiered(stocks: list, target: int = 10) -> list:
    """
    FIX-FILTER-HTML:
    ai_analyzer.py가 이미 4단계 티어 필터로 정교하게 선정·정렬한 종목을
    HTML 단에서 재필터링하지 않는다.

    역할:
      1) name 없는 항목 제거
      2) 중복 name 제거 (첫 번째 등장 우선)
      3) target 개수 제한
      4) overlap_count 키가 없을 때만 하위호환 재계산

    ai_analyzer의 선정 순서(weighted_score 내림차순)를 그대로 유지.
    """
    selected       = []
    selected_names = set()

    for s in stocks:
        name = s.get("name", "").strip()
        if not name:
            continue
        if name in selected_names:
            continue
        if len(selected) >= target:
            break

        # FIX-OVERLAP-1: 키 자체가 없을 때만 하위호환 재계산
        if "overlap_count" not in s:
            cc = s.get("channel_counts", {})
            safe_count = 0
            for v in cc.values():
                try:
                    if v is not None and int(v) > 0:
                        safe_count += 1
                except (TypeError, ValueError):
                    pass
            s["overlap_count"] = safe_count

        selected.append(s)
        selected_names.add(name)

    return selected


# ── 주가 표시 헬퍼 ───────────────────────────────────────────────────────

def _render_price_html(item: dict) -> str:
    """
    FIX-PRICE-4/5:
    - price_label: ai_analyzer가 시각 기준으로 설정한 라벨 표시
    - price=None 또는 0 → "-" 표시
    """
    price       = item.get("price")
    change_pct  = item.get("change_pct", 0.0)
    price_label = item.get("price_label", "전일종가")

    # price가 None이 아닌 숫자이고 0보다 클 때만 표시
    if isinstance(price, (int, float)) and price > 0:
        try:
            pct_num = float(change_pct)
        except (TypeError, ValueError):
            pct_num = 0.0

        pct_color = "#ff6b6b" if pct_num > 0 else "#74c0fc" if pct_num < 0 else "#adb5bd"
        pct_arrow = "▲" if pct_num > 0 else "▼" if pct_num < 0 else "━"

        label_html = (
            f'<span style="font-size:.7rem;color:#adb5bd;margin-left:.3rem;">'
            f'({price_label})</span>'
        )
        # FIX-PRICE-8: 전일종가 표시 시(장 전) → "전전일 대비" 부연
        if price_label == "전일종가":
            pct_suffix = (
                f'<span style="font-size:.7rem;color:#adb5bd;margin-left:.2rem;">'
                f'(전전일 대비)</span>'
            )
        else:
            pct_suffix = ""
        pct_html = (
            f'<span style="font-size:.82rem;color:{pct_color};margin-left:.35rem;">'
            f'{pct_arrow} {pct_num:+.2f}%</span>'
            f'{pct_suffix}'
        )
        return (
            f'<span class="price-value">{int(price):,}원</span>'
            f'{label_html}{pct_html}'
        )

    # 레거시 호환: verified_price
    verified = item.get("verified_price")
    if isinstance(verified, int) and verified > 0:
        return f'<span class="price-value">{verified:,}원</span>'
    if verified and str(verified).strip() not in ("None", "N/A", "", "0"):
        return f'<span class="price-value">{_he.escape(str(verified))}</span>'

    return '<span class="price-value" style="color:#666;">-</span>'


# ── 메인 HTML 생성 ───────────────────────────────────────────────────────

def generate_html(
    data,
    channels_data=None,
    gh_repo="",
    gh_token="",
    market_overview=None,
    all_data=None,
) -> str:
    data            = data or {}
    market_overview = market_overview or data.get("market_data", {}) or {}
    all_data        = all_data or []

    stocks         = data.get("stocks",         [])
    hidden_picks   = data.get("hidden_picks",   [])
    market_sum     = data.get("market_summary", "")
    hot_sectors    = data.get("hot_sectors",    [])
    ai_strategy    = data.get("ai_strategy",    "")
    briefing_date  = data.get("briefing_date",  "")
    market_leaders = data.get("market_leaders", [])

    now_kst = datetime.now(KST)
    if not briefing_date:
        briefing_date = now_kst.strftime("%Y년 %m월 %d일")
    briefing_time = now_kst.strftime("%H:%M")

    filtered_stocks = _filter_stocks_tiered(stocks)

    # FIX-HIDDEN-SIG: 부정 신호가 명시된 경우만 제외, 중립 포함 모두 표시
    filtered_hidden = [
        h for h in hidden_picks
        if not _is_negative_signal(h.get("signal"))
    ]

    market_indicators_html = _build_market_indicators(market_overview)
    market_summary_html    = _render_market_summary(market_sum)

    sector_badges_html = ""
    for sector in hot_sectors:
        if isinstance(sector, dict):
            reason_esc = sector.get("reason", "").replace('"', '&quot;')
            name_esc   = _he.escape(sector.get("name", ""))
            sector_badges_html += (
                f'<div class="sector-badge" title="{reason_esc}">'
                f'{name_esc}</div>'
            )
        elif sector:
            sector_badges_html += (
                f'<div class="sector-badge">{_he.escape(str(sector))}</div>'
            )

    chart_data_dict = {}

    # ── 대형 주도주 HTML ─────────────────────────────────────────────────
    leaders_html = ""
    for rank, leader in enumerate(market_leaders, 1):
        name         = leader.get("name", "")
        signal       = leader.get("signal", "")
        overlap      = leader.get("overlap_count", 0)
        channel_cnts = leader.get("channel_counts", {})
        naver_code   = leader.get("naver_code") or leader.get("code", "")
        naver_url    = leader.get("naver_url", "")

        sig_class, sig_color, signal_label = _resolve_signal(signal)
        price_html   = _render_price_html(leader)
        chart_btn_html = ""
        source_tags_html = "".join(
            f'<span class="source-tag" style="background:{_TAG_META.get(ct, {}).get("bg","#2d3a4a")};'
            f'color:{_TAG_META.get(ct, {}).get("color","#74c0fc")};">'
            f'{_TAG_META.get(ct, {}).get("label", ct)}</span>'
            for ct in channel_cnts
        )
        detail_html = _render_stock_detail(leader)
        leaders_html += f"""
<div class="stock-card market-leader-card">
  <div class="stock-card-header">
    <div class="stock-rank">#{rank}</div>
    <div class="stock-name-block">
      <a href="{naver_url}" target="_blank" rel="noopener"
         class="stock-name">{_he.escape(name)}</a>
      <span class="signal-badge {sig_class}"
            style="border-color:{sig_color};color:{sig_color};">{signal_label}</span>
    </div>
    <div class="overlap-badge" title="비뉴스 채널 중복 언급 수">🔥 {overlap}개</div>
  </div>
  <div class="stock-card-body">
    <div class="source-tags">{source_tags_html}</div>
    <div class="price-row">{price_html}{chart_btn_html}</div>
    {detail_html}
  </div>
</div>"""

    stocks_html = ""

    for rank, stock in enumerate(filtered_stocks, 1):
        name         = stock.get("name", "")
        signal       = stock.get("signal", "")
        overlap      = stock.get("overlap_count", 0)
        channel_cnts = stock.get("channel_counts", {})
        naver_code   = stock.get("naver_code") or stock.get("code", "")
        naver_url    = stock.get("naver_url", "")
        chart_b64    = stock.get("chart_base64", "")

        if not naver_url:
            if naver_code:
                naver_url = (f"https://finance.naver.com/item/main.naver"
                             f"?code={naver_code}")
            elif name:
                naver_url = (f"https://finance.naver.com/search/searchResult.naver"
                             f"?query={name.replace(' ', '+')}")

        sig_class, sig_color, signal_label = _resolve_signal(signal)

        source_tags_html = ""
        for src_type, cnt in channel_cnts.items():
            try:
                cnt_int = int(cnt) if cnt is not None else 0
            except (TypeError, ValueError):
                cnt_int = 0
            if cnt_int > 0:
                meta = _TAG_META.get(src_type, {"bg": "#2d2d44", "color": "#adb5bd"})
                source_tags_html += (
                    f'<span class="source-tag" '
                    f'style="background:{meta["bg"]};color:{meta["color"]};">'
                    f'{_he.escape(src_type)} {cnt_int}</span>'
                )

        price_html = _render_price_html(stock)

        if chart_b64:
            chart_key      = _safe_chart_key("chart", name)
            safe_name_js   = _safe_js_str(name)
            chart_data_dict[chart_key] = f"data:image/png;base64,{chart_b64}"
            chart_btn_html = (
                f"<button class=\"chart-btn\" "
                f"onclick=\"showChart('{chart_key}','{safe_name_js}')\">"
                f"📈 차트 보기</button>"
            )
        elif naver_url:
            chart_btn_html = (
                f'<a href="{naver_url}" target="_blank" rel="noopener" '
                f'class="chart-btn">🔗 Naver 차트</a>'
            )
        else:
            chart_btn_html = ""

        detail_html = _render_stock_detail(stock)

        stocks_html += f"""
<div class="stock-card">
  <div class="stock-card-header">
    <div class="stock-rank">#{rank}</div>
    <div class="stock-name-block">
      <a href="{naver_url}" target="_blank" rel="noopener"
         class="stock-name">{_he.escape(name)}</a>
      <span class="signal-badge {sig_class}"
            style="border-color:{sig_color};color:{sig_color};">{signal_label}</span>
    </div>
    <div class="overlap-badge" title="비뉴스 채널 중복 언급 수">🔥 {overlap}개</div>
  </div>
  <div class="stock-card-body">
    <div class="source-tags">{source_tags_html}</div>
    <div class="price-row">{price_html}{chart_btn_html}</div>
    {detail_html}
  </div>
</div>"""

    if not stocks_html:
        stocks_html = (
            '<p style="color:#666;text-align:center;padding:2rem;">'
            '오늘은 복수 채널 교차 언급 종목이 없습니다.</p>'
        )

    hidden_html = ""
    for idx, hp in enumerate(filtered_hidden, 1):
        name         = hp.get("name", "")
        channel_type = hp.get("channel_type", "")
        weighted_sc  = hp.get("weighted_score", 0)
        naver_code   = hp.get("naver_code") or hp.get("code", "")
        naver_url    = hp.get("naver_url", "")
        chart_b64    = hp.get("chart_base64", "")
        reasons      = hp.get("reasons", [])

        if not naver_url:
            if naver_code:
                naver_url = (f"https://finance.naver.com/item/main.naver"
                             f"?code={naver_code}")
            elif name:
                naver_url = (f"https://finance.naver.com/search/searchResult.naver"
                             f"?query={name.replace(' ', '+')}")

        source_badge_html = _hidden_pick_source_badge(channel_type)
        star_html         = _render_star_rating(weighted_sc)
        pick_badge_html   = f'<span class="hp-score-badge">Pick #{idx}</span>'

        price_html = _render_price_html(hp)

        if chart_b64:
            chart_key      = _safe_chart_key("hpchart", name)
            safe_name_js   = _safe_js_str(name)
            chart_data_dict[chart_key] = f"data:image/png;base64,{chart_b64}"
            chart_btn_html = (
                f"<button class=\"chart-btn\" "
                f"onclick=\"showChart('{chart_key}','{safe_name_js}')\">"
                f"📈 차트 보기</button>"
            )
        elif naver_url:
            chart_btn_html = (
                f'<a href="{naver_url}" target="_blank" rel="noopener" '
                f'class="chart-btn">🔗 Naver 차트</a>'
            )
        else:
            chart_btn_html = ""

        detail_html   = _render_stock_detail(hp)
        reasons_block = _render_reasons(reasons)

        hidden_html += f"""
<div class="hidden-pick-card">
  <div class="hp-card-header">
    <div class="hp-badges">{source_badge_html}{pick_badge_html}</div>
    <a href="{naver_url}" target="_blank" rel="noopener"
       class="hp-stock-name">{_he.escape(name)}</a>
    {star_html}
  </div>
  <div class="hp-card-body">
    <div class="price-row">{price_html}{chart_btn_html}</div>
    {detail_html}
    {reasons_block}
  </div>
</div>"""

    if not hidden_html:
        hidden_html = (
            '<p style="color:#666;text-align:center;padding:1.5rem;">'
            '오늘의 픽 없음</p>'
        )

    if chart_data_dict:
        entries       = [f'"{k}": "{v}"' for k, v in chart_data_dict.items()]
        chart_data_js = "const chartDataMap = {\n  " + ",\n  ".join(entries) + "\n};"
    else:
        chart_data_js = "const chartDataMap = {};"

    analyst_html  = _build_analyst_html(all_data)
    strategy_html = _render_ai_strategy(ai_strategy)

    css = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:         #0d1117;
  --surface:    #161b22;
  --surface2:   #21262d;
  --border:     #30363d;
  --text:       #e6edf3;
  --text-muted: #8b949e;
  --accent:     #58a6ff;
  --up:         #ff6b6b;
  --down:       #74c0fc;
  --flat:       #adb5bd;
}
html { font-size: 16px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Pretendard', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
  line-height: 1.6;
  padding: 0 0 4rem;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 900px; margin: 0 auto; padding: 0 1rem; }
.briefing-header {
  text-align: center;
  padding: 2.5rem 1rem 1.5rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}
.briefing-header h1 { font-size: 1.8rem; font-weight: 700; }
.subtitle { color: var(--text-muted); font-size: .9rem; margin-top: .4rem; }
.section { margin-bottom: 2.5rem; }
.market-leader-card {
  border-left: 3px solid #ffd43b;
}
.market-leader-card .stock-rank {
  color: #ffd43b;
}
.section-title {
  font-size: 1.15rem; font-weight: 700; color: var(--text);
  border-left: 4px solid var(--accent);
  padding-left: .75rem; margin-bottom: 1rem;
}
.market-indicators { display: flex; flex-wrap: wrap; gap: .6rem; }
.indicator-badge {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: .5rem .9rem;
  display: flex; flex-direction: column; align-items: center; min-width: 100px;
}
.ind-label { font-size: .75rem; color: var(--text-muted); margin-bottom: .15rem; }
.ind-value { font-size: .95rem; font-weight: 600; }
.ind-pct   { font-size: .8rem;  margin-top: .1rem; }
.summary-block { margin-bottom: 1.2rem; }
.summary-title {
  font-size: .85rem; font-weight: 700; color: var(--accent);
  margin-bottom: .35rem; text-transform: uppercase; letter-spacing: .04em;
}
.summary-text { font-size: .93rem; color: var(--text-muted); line-height: 1.65; }
.sector-badges { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
.sector-badge {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 20px; padding: .3rem .85rem;
  font-size: .82rem; color: var(--accent); cursor: default;
}
.stock-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; margin-bottom: 1rem; overflow: hidden;
}
.stock-card-header {
  display: flex; align-items: center; gap: .75rem;
  padding: .85rem 1rem; border-bottom: 1px solid var(--border);
  background: var(--surface2);
}
.stock-rank  { font-size: .8rem; font-weight: 700; color: var(--text-muted); min-width: 28px; }
.stock-name-block { display: flex; align-items: center; gap: .5rem; flex: 1; }
.stock-name  { font-size: 1.05rem; font-weight: 700; color: var(--text); }
.stock-name:hover { color: var(--accent); }
.signal-badge {
  font-size: .72rem; padding: .15rem .5rem;
  border-radius: 4px; border: 1px solid; font-weight: 600;
}
.overlap-badge { font-size: .78rem; color: #ffa94d; font-weight: 600; white-space: nowrap; }
.stock-card-body { padding: .85rem 1rem; }
.source-tags { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: .6rem; }
.source-tag  { font-size: .72rem; padding: .15rem .5rem; border-radius: 4px; font-weight: 600; }
.price-row   { display: flex; align-items: center; gap: .75rem; margin-bottom: .75rem; flex-wrap: wrap; }
.price-value { font-size: 1.05rem; font-weight: 700; }
.chart-btn {
  font-size: .78rem; padding: .3rem .75rem; border-radius: 6px;
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--accent); cursor: pointer; text-decoration: none;
  transition: background .15s;
}
.chart-btn:hover { background: var(--border); text-decoration: none; }
.stock-section { margin-bottom: .75rem; }
.stock-section-label { font-size: .78rem; font-weight: 700; color: var(--accent); display: block; margin-bottom: .25rem; }
.stock-section-text  { font-size: .88rem; color: var(--text-muted); line-height: 1.6; }
.reasons-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: .4rem; }
.reasons-list li { font-size: .85rem; line-height: 1.55; }
.reason-source { font-size: .72rem; padding: .1rem .4rem; border-radius: 3px; font-weight: 600; margin-right: .3rem; }
.hidden-pick-card {
  background: linear-gradient(135deg, var(--surface) 0%, #1a1f2e 100%);
  border: 1px solid #3d4f6e; border-radius: 12px;
  margin-bottom: 1rem; overflow: hidden;
}
.hp-card-header {
  display: flex; align-items: center; gap: .75rem;
  padding: .85rem 1rem; border-bottom: 1px solid #3d4f6e;
  background: #1a2235; flex-wrap: wrap;
}
.hp-badges { display: flex; gap: .4rem; align-items: center; }
.hp-source-badge {
  font-size: .72rem; padding: .2rem .6rem; border-radius: 20px; font-weight: 700;
}
.hp-score-badge {
  font-size: .72rem; padding: .2rem .6rem; border-radius: 20px;
  background: #ffa94d22; color: #ffa94d; border: 1px solid #ffa94d55; font-weight: 700;
}
.hp-stock-name { font-size: 1.05rem; font-weight: 700; color: var(--text); flex: 1; }
.hp-stock-name:hover { color: var(--accent); }
.star-rating { font-size: 1rem; }
.star.filled { color: #ffa94d; }
.star.empty  { color: #444; }
.hp-card-body { padding: .85rem 1rem; }
.analyst-card {
  background: var(--surface2); border: 1px solid var(--border);
  border-left-width: 3px; border-left-color: var(--accent);
  border-radius: 8px; padding: .75rem 1rem; margin-bottom: .6rem;
  transition: border-color .15s;
}
.analyst-card-meta { display: flex; align-items: center; gap: .5rem; margin-bottom: .3rem; flex-wrap: wrap; }
.analyst-stock  { font-weight: 700; font-size: .92rem; }
.analyst-broker {
  font-size: .76rem; font-weight: 600; padding: .12rem .5rem;
  border-radius: 999px;
}
.analyst-cat-badge {
  font-size: .68rem; padding: .1rem .45rem; border-radius: 4px;
  font-weight: 700;
}
.analyst-title-link, .analyst-title-text { font-size: .88rem; color: var(--text-muted); line-height: 1.5; }
.analyst-title-link:hover { color: var(--accent); }
.analyst-category-title {
  font-size: .85rem; font-weight: 700; color: var(--accent);
  margin: 1rem 0 .5rem; padding-left: .5rem; border-left: 3px solid var(--accent);
}
.strat-section { margin-bottom: 1.2rem; }
.strat-title { font-size: .9rem; font-weight: 700; color: var(--accent); margin-bottom: .4rem; }
.strat-body  { font-size: .88rem; color: var(--text-muted); }
.strat-text  { margin-bottom: .3rem; line-height: 1.6; }
.strat-item  { margin-bottom: .25rem; line-height: 1.55; }
.disclaimer {
  margin-top: 2.5rem;
  padding: 1rem 1.4rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid #58a6ff44;
  border-radius: 8px;
  font-size: .78rem;
  color: #6e7681;
  line-height: 1.8;
  text-align: center;
}
.disclaimer strong {
  display: block;
  color: #8b949e;
  font-weight: 600;
  margin-bottom: .35rem;
  font-size: .82rem;
}
.modal-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,.75); z-index: 1000;
  align-items: center; justify-content: center;
}
.modal-overlay.active { display: flex; }
.modal-box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.5rem; max-width: 700px; width: 95%; position: relative;
}
.modal-title  { font-size: 1rem; font-weight: 700; margin-bottom: 1rem; color: var(--text); }
.modal-close  {
  position: absolute; top: .75rem; right: .75rem;
  background: none; border: none; color: var(--text-muted); font-size: 1.2rem; cursor: pointer;
}
.modal-close:hover { color: var(--text); }
#modal-chart-img { width: 100%; border-radius: 8px; }
@media (max-width: 600px) {
  .briefing-header h1 { font-size: 1.4rem; }
  .stock-card-header  { flex-wrap: wrap; }
  .stock-name         { font-size: .95rem; }
}"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 주식 브리핑 — {briefing_date}</title>
<style>{css}</style>
</head>
<body>
<div class="container">

  <div class="briefing-header">
    <h1>📈 AI 주식 브리핑</h1>
    <div class="subtitle">{briefing_date} · 생성 시각 {briefing_time} KST</div>
  </div>

  <div class="section">
    <div class="section-title">📊 시장 지표</div>
    {market_indicators_html}
  </div>

  <div class="section">
    <div class="section-title">🗞 최근 시장 흐름</div>
    {market_summary_html}
  </div>

  <div class="section">
    <div class="section-title">🔥 오늘의 핫 섹터</div>
    <div class="sector-badges">{sector_badges_html}</div>
  </div>

  <div class="section">
    <div class="section-title">🏆 오늘의 대형 주도주</div>
    {leaders_html}
    <div class="section-title">👀 오늘의 관심종목</div>
    {stocks_html}
  </div>

  <div class="section">
    <div class="section-title">💎 오늘의 픽</div>
    {hidden_html}
  </div>

  <div class="section">
    <div class="section-title">📋 오늘의 증권사 리포트</div>
    {analyst_html}
  </div>

  <div class="section">
    <div class="section-title">🤖 AI 투자 전략</div>
    {strategy_html}
  </div>

  <div class="disclaimer">
    <strong>📌 투자 유의사항</strong>
    본 브리핑은 뉴스·유튜브·애널리스트 리포트 등 공개 데이터를 AI가 수집·분석한 참고 자료입니다.
    투자 권유 또는 종목 추천이 아니며, 수익을 보장하지 않습니다.
    최종 투자 판단과 그에 따른 책임은 전적으로 본인에게 있습니다.
  </div>

</div>

<div class="modal-overlay" id="chartModal">
  <div class="modal-box">
    <button class="modal-close" onclick="closeChart()">✕</button>
    <div class="modal-title" id="modal-chart-title"></div>
    <img id="modal-chart-img" src="" alt="차트">
  </div>
</div>

<script>
{chart_data_js}

function showChart(key, name) {{
  const src = chartDataMap[key];
  if (!src) return;
  document.getElementById('modal-chart-title').textContent = name + ' 주가 차트';
  document.getElementById('modal-chart-img').src = src;
  document.getElementById('chartModal').classList.add('active');
}}

function closeChart() {{
  document.getElementById('chartModal').classList.remove('active');
  document.getElementById('modal-chart-img').src = '';
}}

document.getElementById('chartModal').addEventListener('click', function(e) {{
  if (e.target === this) closeChart();
}});
</script>

</body>
</html>"""

