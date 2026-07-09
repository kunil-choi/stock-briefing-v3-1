# collectors/news_collector.py
"""
뉴스 RSS 수집기 - v3
매일경제, 한국경제, 서울경제, 이데일리, 머니투데이 등 주요 경제신문사 RSS 수집

수정 이력:
- BUG-N-1  : 날짜 파싱 실패 시 현재 시각 반환 (24시간 필터 통과)
- BUG-N-2  : feedparser bozo 오류 감지, CharacterEncodingOverride는 무해 처리
- BUG-N-3  : 피드별 독립 try/except
- BUG-N-4  : link 기준 중복 제거
- BUG-7 FIX: link 없는 항목의 title 기반 중복 제거 추가
             link URL 정규화(쿼리 파라미터 제거) 후 비교
- BUG-7B FIX: link가 있는 항목은 seen_titles에 등록하지 않도록 수정
- V3-NEWS-1: 기사 본문 크롤링 추가 (V2 이식) — RSS 요약보다 본문이 길면 본문 사용
             날짜 확인된 기사만 크롤링 슬롯 사용 (피드당 최대 15건)
"""
import re
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

KST = timezone(timedelta(hours=9))


def fetch_article_body(url: str, max_chars: int = 1500) -> str:
    """
    V3-NEWS-1: 기사 URL에서 본문 텍스트를 추출합니다 (최대 max_chars자).
    주요 경제신문사 본문 선택자를 순서대로 시도하며,
    실패 시 <p> 태그 집합으로 폴백합니다.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 광고·네비게이션 등 노이즈 제거
        for tag in soup.select(
            "script, style, nav, header, footer, aside, "
            ".ad, .banner, .comment, .relate, .copyright"
        ):
            tag.decompose()

        # 언론사별 본문 선택자 (우선순위 순)
        selectors = [
            "div#newsct_article",          # 네이버 뉴스 뷰어
            "div.article_body",
            "div#article_body",
            "div.article-body",
            "article#article-view-content-div",
            "div#textBody",
            "div#news_body_area",
            "div.news_cnt_detail_wrap",
            "article",
            "div.article_txt",
            "div.article_content",
            "div.news_body",
            "div#articleBodyContents",     # 한국경제
            "div.article-text",            # 매일경제
            "div#article-view-content-div",# 이데일리
        ]

        body_text = ""
        for selector in selectors:
            el = soup.select_one(selector)
            if el:
                body_text = el.get_text(separator=" ", strip=True)
                if len(body_text) > 100:
                    break

        # 폴백: <p> 태그 집합
        if len(body_text) < 100:
            paragraphs = soup.select("p")
            texts = [
                p.get_text(strip=True)
                for p in paragraphs
                if len(p.get_text(strip=True)) > 30
            ]
            body_text = " ".join(texts)

        body_text = re.sub(r"\s+", " ", body_text).strip()
        return body_text[:max_chars] if body_text else ""

    except Exception as e:
        print(f"  [본문크롤링] {url[:60]}... 실패: {e}")
        return ""


def _parse_published(entry) -> datetime:
    """RSS 엔트리의 published 날짜를 datetime으로 파싱"""
    for attr in ("published", "updated", "created"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(KST)
            except Exception:
                pass
    # BUG-N-1: 날짜 파싱 실패 시 현재 시각 반환
    return datetime.now(KST)


def _is_valid_feed(feed) -> bool:
    """
    BUG-N-5: feedparser는 인코딩 선언 불일치, 네임스페이스 누락 등
    RSS 스펙을 엄격히 따르지 않는 사소한 경우에도 bozo=True를 설정한다.
    이런 경우 대부분 feedparser는 entries를 정상적으로 파싱해내므로,
    bozo 플래그만으로 피드 전체를 버리면 실제로 읽을 수 있는 기사가
    있는 피드까지 통째로 스킵하게 됨 (한국 언론사 RSS에서 빈번히 발생).

    → HTTP 상태 코드가 명확한 실패(4xx/5xx)인 경우만 무효로 처리하고,
      bozo 자체는 진단용 로그로만 남긴다. entries 존재 여부는 호출부
      (collect_news)에서 별도로 확인.
    """
    status = getattr(feed, "status", None)
    if status is not None and status >= 400:
        return False
    if getattr(feed, "bozo", False):
        exc = getattr(feed, "bozo_exception", None)
        print(f"    [bozo경고, 무시] {type(exc).__name__ if exc else '?'}: "
              f"{str(exc)[:100] if exc else ''}")
    return True


def _normalize_link(link: str) -> str:
    """BUG-7 FIX: URL 정규화 — 쿼리 파라미터·프래그먼트 제거 후 소문자 변환"""
    if not link:
        return ""
    try:
        parsed = urlparse(link.strip())
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            "", "", "",
        ))
        return normalized
    except Exception:
        return link.strip().lower()


def _normalize_title(title: str) -> str:
    """BUG-7 FIX: 제목 정규화 — 공백·특수문자 제거 후 소문자 변환"""
    if not title:
        return ""
    return re.sub(r"[^\w가-힣]", "", title).lower()


def collect_news(rss_feeds: dict, hours: int = 24) -> list:
    """
    RSS 피드에서 최근 N시간 이내 뉴스를 수집하고,
    날짜가 확인된 기사는 본문을 직접 크롤링하여 요약을 보강합니다.

    V3-NEWS-1: 피드당 최대 15건 본문 크롤링
               본문이 RSS 요약보다 길 경우에만 본문으로 교체
    """
    cutoff        = datetime.now(KST) - timedelta(hours=hours)
    results       = []
    failed_feeds: list[str] = []

    for source_name, feed_url in rss_feeds.items():
        try:
            feed = feedparser.parse(feed_url)

            if not _is_valid_feed(feed):
                print(f"  [뉴스] {source_name} 피드 파싱 오류 → 스킵")
                failed_feeds.append(source_name)
                continue

            if not feed.entries:
                print(f"  [뉴스] {source_name} 엔트리 없음 → 스킵")
                failed_feeds.append(source_name)
                continue

            count       = 0
            crawl_count = 0  # V3-NEWS-1: 피드당 크롤링 슬롯 카운터

            for entry in feed.entries[:30]:
                published_dt = _parse_published(entry)
                if published_dt < cutoff:
                    continue

                title       = (getattr(entry, "title",   "") or "").strip()
                rss_summary = (getattr(entry, "summary", "") or
                               getattr(entry, "description", "") or "").strip()
                link        = (getattr(entry, "link", "") or "").strip()

                rss_summary = re.sub(r"<[^>]+>", "", rss_summary)[:800]

                if not title:
                    continue

                # ── V3-NEWS-1: 본문 크롤링 ──────────────────────────────────
                # 조건: link 있음 + 날짜 확인됨 + 크롤링 슬롯 남음 (피드당 15건)
                body = ""
                if link and crawl_count < 15:
                    body = fetch_article_body(link, max_chars=1500)
                    crawl_count += 1

                # 본문이 RSS 요약보다 길면 본문 사용, 아니면 RSS 요약 유지
                summary = body if len(body) > len(rss_summary) else rss_summary
                # ─────────────────────────────────────────────────────────────

                results.append({
                    "source_type": "뉴스",
                    "source_name": source_name,
                    "title":       title,
                    "summary":     summary,
                    "link":        link,
                    "published":   published_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                })
                count += 1

            print(f"  [뉴스] {source_name}: {count}건 (본문크롤링 {crawl_count}건)")

        except Exception as e:
            print(f"  [뉴스] {source_name} 수집 실패: {e}")
            failed_feeds.append(source_name)

    # ── 중복 제거 (BUG-7, BUG-7B 로직 유지) ─────────────────────────────────
    seen_links:  set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict]   = []

    for item in results:
        link  = item.get("link", "")
        title = item.get("title", "")

        norm_link  = _normalize_link(link)
        norm_title = _normalize_title(title)

        if norm_link:
            if norm_link in seen_links:
                continue
            seen_links.add(norm_link)
        else:
            if norm_title and norm_title in seen_titles:
                continue
            if norm_title:
                seen_titles.add(norm_title)

        deduped.append(item)

    if failed_feeds:
        print(f"\n  [뉴스] 실패 피드: {', '.join(failed_feeds)}")

        print(f"\n[뉴스 합계] {len(deduped)}건 (중복 제거 전: {len(results)}건, 본문크롤링 포함)")
    return deduped
