# config.py
"""
환경변수 및 전역 설정

수정 이력:
- INIT        : 초기 설정
- FIX-PAN-1   : POPULAR_PANELISTS 업데이트 (2026년 6월 기준, 최근 2개월 내 5만뷰+ 기준)
- GEMINI-1    : GEMINI_API_KEY 추가 — 유튜브 영상 분석 및 브리핑 교차 검수용
"""

import os
import json

# ── API 키 및 토큰 ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUTUBE_API_KEY   = os.getenv("YOUTUBE_API_KEY", "")
GH_TOKEN          = os.getenv("GH_TOKEN", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")   # GEMINI-1

# ── GitHub 저장소 설정 ──────────────────────────────────────────
GITHUB_REPO   = "kunil-choi/stock-briefing-v3-1"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

# ── 파일 경로 ───────────────────────────────────────────────────
CHANNELS_FILE = "channels.json"
OUTPUT_FILE   = "data/briefing_data.json"

# ── 관리자 패널 비밀번호 ────────────────────────────────────────
PANEL_PASSWORD = os.getenv("ADMIN_PASSWORD", "stock2026!")

# ── 수집 시간 범위 (시간 단위) ──────────────────────────────────
MIN_VIDEO_DURATION_SECONDS = 180   # YouTube 공식 쇼츠 기준(180초) 미만 제외
BROADCAST_HOURS  = 24
YOUTUBER_HOURS   = 24
SECURITIES_HOURS = 24
REPORT_DAYS      = 1

# ── 패널리스트 YouTube 검색 기준 ────────────────────────────────
PANELIST_SEARCH_HOURS   = 48
PANELIST_MIN_VIEWS      = 50000
PANELIST_MAX_RESULTS    = 10

# ── RSS 뉴스 피드 ───────────────────────────────────────────────
# FIX-NEWS-RSS-1: 2026-07-27 기준 실행 로그에서 8개 피드 중 6개가
# 403/404/커넥션 리셋으로 죽어있는 것을 확인 → 조사 가능한 범위에서 최신
# 주소로 교체. 파이낸셜뉴스는 URL 자체가 "fn_realestate_stock"(부동산)으로
# 잘못돼 있어 애초에 증권 피드가 아니었던 것으로 보임 → "fn_realnews_stock"
# (실시간 증권)로 수정. 언론사 RSS 주소는 사전 예고 없이 자주 바뀌므로,
# 이 값이 다시 죽더라도 news_collector.collect_news()가
# NEWS_SITE_HOMEPAGES를 이용해 자동으로 최신 주소를 탐지·재접속한다
# (RSS-AUTO-1 참고).
NEWS_RSS_FEEDS = {
    "한국경제":     "http://rss.hankyung.com/stock.xml",
    "매일경제":     "https://www.mk.co.kr/rss/30100041/",
    "머니투데이":   "http://rss.mt.co.kr/mt_news.xml",
    "이데일리":     "http://rss.edaily.co.kr/edaily/stock.xml",
    "연합인포맥스": "https://news.einfomax.co.kr/rss/subList/2.xml",
    "뉴스핌":       "https://www.newspim.com/rss/",
    "파이낸셜뉴스": "http://www.fnnews.com/rss/fn_realnews_stock.xml",
    "아시아경제":   "https://www.asiae.co.kr/rss/all.htm",
}

# FIX-NEWS-RSS-1: RSS 피드 주소가 죽어있을 때 자동 재탐지에 쓸 앵커 페이지.
# 표준 RSS 자동탐지 태그(<link rel="alternate" type="application/rss+xml">)가
# 있을 가능성이 높은 페이지(가능하면 증권/경제 섹션, 없으면 RSS 안내 페이지나
# 홈페이지)를 지정한다.
NEWS_SITE_HOMEPAGES = {
    "한국경제":     "https://www.hankyung.com/finance",
    "매일경제":     "https://www.mk.co.kr/",
    "머니투데이":   "https://www.mt.co.kr/rss/rss.html",
    "이데일리":     "https://www.edaily.co.kr/article/stock",
    "연합인포맥스": "https://news.einfomax.co.kr/",
    "뉴스핌":       "https://www.newspim.com/",
    "파이낸셜뉴스": "https://www.fnnews.com/rss",
    "아시아경제":   "https://www.asiae.co.kr/",
}

# ── 주요 패널리스트 이름 목록 ────────────────────────────────────
POPULAR_PANELISTS = [
    "오건영", "홍춘욱", "한상춘", "이진우",
    "염승환", "박세익", "이효석", "이선엽",
    "박병창", "이승우", "윤지호", "신형관",
    "장우석", "임형록",
    "강방천", "최준철", "이채원", "곽상준",
    "김동환", "박종훈",
    "김학균", "김한진",
]
POPULAR_PANELISTS = list(dict.fromkeys(POPULAR_PANELISTS))


# ── 채널 데이터 로드 함수 ────────────────────────────────────────
def load_channels() -> dict:
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for cat in ["broadcast", "youtuber", "securities"]:
            data.setdefault(cat, [])
        return data
    except FileNotFoundError:
        print(f"[설정] {CHANNELS_FILE} 없음 → 빈 채널 목록 사용")
        return {"broadcast": [], "youtuber": [], "securities": []}
    except json.JSONDecodeError as e:
        print(f"[설정] {CHANNELS_FILE} JSON 파싱 실패: {e}")
        return {"broadcast": [], "youtuber": [], "securities": []}
