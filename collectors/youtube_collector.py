# collectors/youtube_collector.py
# - FIX-SHORTS-1: 유튜브 쇼츠 필터링 추가 (is_shorts 함수, 채널/패널리스트 두 경로 모두 적용)
# - CHANNEL-FILTER-1: 패널리스트 검색 결과에서 비등록 채널 품질 필터링 추가
#   등록 채널(화이트리스트) → 무조건 통과
#   비등록 채널 → channels.list API로 구독자 수 배치 조회 (1유닛/채널)
#   구독자 미달(_PANELIST_MIN_SUBSCRIBERS 미만) 또는 블랙리스트 → 제외
"""
수정 이력:
- BUG-1: collect_panelist_youtube()의 publishedAfter UTC 변환 오류 수정
         KST 시각을 UTC 포맷으로 전달하던 문제 → UTC 기준으로 cutoff 계산
- CHANNEL-FILTER-1: 패널리스트 검색 결과 채널 품질 필터 추가
         등록 채널 화이트리스트, 구독자 수 기준(5만 이상), 블랙리스트 차단
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_AVAILABLE = True
except ImportError:
    _TRANSCRIPT_AVAILABLE = False

from config import (
    YOUTUBE_API_KEY,
    BROADCAST_HOURS,
    YOUTUBER_HOURS,
    SECURITIES_HOURS,
    POPULAR_PANELISTS,
    MIN_VIDEO_DURATION_SECONDS,
)

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc  # BUG-1: UTC 상수 추가

STOCK_KEYWORDS = [
    "주식", "종목", "투자", "매수", "매도", "코스피", "코스닥",
    "증권", "ETF", "수익률 전망", "실적발표", "어닝", "리포트",
    "급등", "급락", "섹터", "포트폴리오", "코멘트",
    "삼성전자", "SK하이닉스", "LG에너지솔루션", "현대차",
    "금리인상", "환율", "달러", "AI반도체", "배터리",
]

EXPERT_KEYWORDS = [
    "매수전략", "애널리스트", "증권사", "리포트", "전망",
    "목표주가", "투자의견", "매수추천", "시장분석",
    "섹터분석", "포트폴리오", "리스크", "코멘트",
]

SECURITIES_ANALYSIS_KEYWORDS = [
    "분석", "리포트", "전망", "시황", "코멘트",
    "목표주가", "투자의견", "매수추천", "종목분석",
    "수익률 전망", "섹터분석", "포트폴리오", "리스크",
    "매수전략", "수익률", "실적발표", "이슈", "스탁",
    "신규 커버리지",
]

AD_KEYWORDS = [
    "광고비", "협찬", "홍보영상", "신청하기", "무료강의",
    "유료과정", "강의모집", "수강생", "연락처", "카카오링크",
]

# ── 패널리스트 검색 전용 설정 ──────────────────────────────────────────
# PANELIST-2: 이름별 suffix 순차검색(최악 22×5=110회 search.list, 11,000유닛 →
# 일일 quota 10,000유닛 초과로 quotaExceeded 양산) 대신, 이름을 배치로 묶어
# OR(|) 연산자로 한 번에 검색하는 방식으로 교체. suffix 키워드는 더 이상 사용하지 않음
# (콘텐츠 관련성 판단은 is_stock_related + 다운스트림 Gemini 분석에 위임).
# 검색 결과에서 수집할 최대 영상 수 (배치당, search.list 응답 한도)
_PANELIST_MAX_RESULTS = 50
# 검색 대상 기간 (시간) — 사용자 요청에 따라 48h → 24h로 변경
_PANELIST_HOURS = 24
# 한 번의 OR 검색에 묶을 패널리스트 수.
# 22명 ÷ 5명 = 배치 5회 × 100유닛 = 500유닛 (기존 최악 11,000유닛 대비 22배 절감)
_PANELIST_BATCH_SIZE = 5

# CHANNEL-FILTER-1: 비등록 채널 최소 구독자 수 기준
# 이 값 미만이면 "저품질/복제 채널"로 간주하고 패널리스트 검색 결과에서 제외
_PANELIST_MIN_SUBSCRIBERS = 50_000  # 5만 명

# channels.json 경로 (CHANNEL-FILTER-1)
_CHANNELS_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "channels.json")


def _load_channel_filter_sets() -> tuple:
    """
    CHANNEL-FILTER-1:
    channels.json에서 등록 채널 ID 화이트리스트와 블랙리스트를 로드.

    반환: (whitelist_ids: set, blacklist_ids: set, blacklist_names: set)
    - whitelist_ids: 등록 채널 채널ID 집합 → 구독자 체크 없이 통과
    - blacklist_ids: 차단 채널ID 집합 → 무조건 제외
    - blacklist_names: 차단 채널명 집합 → 채널ID 미등록 시 이름으로도 차단
    """
    whitelist_ids  = set()
    blacklist_ids  = set()
    blacklist_names = set()

    try:
        path = os.path.abspath(_CHANNELS_JSON_PATH)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        # 등록 채널 화이트리스트
        for section in ("broadcast", "youtuber", "securities"):
            for ch in data.get(section, []):
                cid = ch.get("id", "").strip()
                if cid and not cid.startswith("@"):
                    whitelist_ids.add(cid)

        # 블랙리스트
        for ch in data.get("blacklist", []):
            cid  = ch.get("id", "").strip()
            name = ch.get("name", "").strip()
            if cid:
                blacklist_ids.add(cid)
            if name:
                blacklist_names.add(name)

        print(f"  [채널필터] 화이트리스트 {len(whitelist_ids)}개, "
              f"블랙리스트 ID {len(blacklist_ids)}개 / 이름 {len(blacklist_names)}개")
    except Exception as e:
        print(f"  [채널필터] channels.json 로드 실패: {e}")

    return whitelist_ids, blacklist_ids, blacklist_names


def _batch_fetch_subscriber_counts(youtube, channel_ids: list) -> dict:
    """
    CHANNEL-FILTER-1:
    YouTube channels.list API로 채널 ID 목록의 구독자 수를 배치 조회.
    최대 50개씩 묶어 호출 (API 제한).
    quota 비용: 1유닛/호출 (search.list의 100유닛 대비 매우 저렴)

    반환: {channel_id: subscriber_count, ...}
    조회 실패한 채널은 결과에서 누락됨.
    """
    result = {}
    if not channel_ids:
        return result

    # 중복 제거 및 최대 50개씩 배치
    unique_ids = list(set(channel_ids))
    batch_size = 50

    for i in range(0, len(unique_ids), batch_size):
        batch = unique_ids[i:i + batch_size]
        try:
            resp = youtube.channels().list(
                part="statistics",
                id=",".join(batch),
                maxResults=batch_size,
            ).execute()
            for ch in resp.get("items", []):
                cid   = ch.get("id", "")
                stats = ch.get("statistics", {})
                # hiddenSubscriberCount=True인 경우 subscriberCount 키 없음 → 0
                count = int(stats.get("subscriberCount", 0))
                result[cid] = count
        except Exception as e:
            print(f"  [채널필터] 구독자 수 조회 실패 (배치 {i//batch_size+1}): {e}")
        time.sleep(0.2)

    return result


def get_youtube_client(api_key: str = None):
    if not api_key:
        api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        print("  [YouTube] API 키 없음")
        return None
    try:
        client = build("youtube", "v3", developerKey=api_key)
        print("  [YouTube] 클라이언트 초기화 성공")
        return client
    except Exception as e:
        print(f"  [YouTube] 클라이언트 초기화 실패: {e}")
        return None


def get_uploads_playlist_id(channel_id: str) -> str:
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id


def resolve_channel_id(youtube, handle: str) -> str:
    try:
        resp = youtube.channels().list(
            part="id",
            forHandle=handle.lstrip("@"),
        ).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
    except Exception as e:
        print(f"  [채널ID 조회 실패] {handle}: {e}")
    return None


def get_recent_videos_via_playlist(youtube, channel_id: str, hours: int) -> list:
    playlist_id = get_uploads_playlist_id(channel_id)
    cutoff      = datetime.now(KST) - timedelta(hours=hours)
    videos      = []

    try:
        next_page_token = None
        while True:
            resp = youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=20,
                pageToken=next_page_token,
            ).execute()

            items = resp.get("items", [])
            if not items:
                break

            found_old = False
            for item in items:
                snippet      = item.get("snippet", {})
                published_at = snippet.get("publishedAt", "")
                if not published_at:
                    continue
                pub_dt = datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                ).astimezone(KST)
                if pub_dt < cutoff:
                    found_old = True
                    break

                video_id = (
                    item.get("contentDetails", {}).get("videoId")
                    or snippet.get("resourceId", {}).get("videoId", "")
                )
                if not video_id:
                    continue

                videos.append({
                    "video_id":     video_id,
                    "title":        snippet.get("title", ""),
                    "description":  snippet.get("description", "")[:1000],  # description 추가
                    "channel_id":   snippet.get("channelId", channel_id),
                    "channel_name": snippet.get("channelTitle", ""),
                    "published_at": pub_dt.strftime("%Y-%m-%d %H:%M"),
                    "thumbnail":    snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                })

            if found_old:
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break
            time.sleep(0.2)

    except HttpError as e:
        code = e.resp.status if hasattr(e, "resp") else 0
        if "playlistNotFound" in str(e) or code == 404:
            print(f"  [플레이리스트 없음] {channel_id}")
        else:
            print(f"  [플레이리스트 오류] {channel_id}: {e}")
    except Exception as e:
        print(f"  [일반 오류 발생] {channel_id}: {e}")

    return videos


def get_transcript(video_id: str, max_chars: int = 2000) -> str:
    if not _TRANSCRIPT_AVAILABLE:
        return ""
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            t = transcript_list.find_transcript(["ko"])
        except Exception:
            try:
                t = transcript_list.find_generated_transcript(["ko"])
            except Exception:
                return ""
        entries = t.fetch()
        texts   = []
        for e in entries:
            if hasattr(e, "text"):
                texts.append(str(e.text))
            elif isinstance(e, dict):
                texts.append(e.get("text", ""))
            else:
                try:
                    texts.append(str(e))
                except Exception:
                    pass
        return " ".join(texts)[:max_chars]
    except Exception:
        return ""


def is_stock_related(title: str, transcript: str = "") -> bool:
    combined = (title + " " + transcript).lower()
    return any(kw in combined for kw in STOCK_KEYWORDS)


def is_securities_analysis(title: str, transcript: str = "") -> bool:
    combined = (title + " " + transcript).lower()
    return any(kw in combined for kw in SECURITIES_ANALYSIS_KEYWORDS)


def is_ad_content(title: str) -> bool:
    return any(kw in title for kw in AD_KEYWORDS)


def is_shorts(title: str, video_id: str = "") -> bool:
    """
    유튜브 쇼츠 여부 제목 기반 1차 판별 (빠른 사전 필터).
    태그 없이 짧게 올리는 우회는 _fetch_video_durations()로 2차 필터링.
    """
    title_lower = title.lower()
    return (
        "#shorts" in title_lower
        or "#short" in title_lower
        or "# shorts" in title_lower
    )


def _parse_iso_duration(duration: str) -> int:
    """ISO 8601 duration (PT1H2M3S) → 초 단위 정수."""
    import re as _re
    m = _re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _fetch_video_durations(youtube, video_ids: list) -> dict:
    """
    videos().list(contentDetails)로 재생시간 배치 조회.
    반환: {video_id: duration_seconds}
    조회 실패한 video_id는 결과에서 누락됨 (→ 보수적으로 통과 처리).
    quota 비용: 1유닛/호출 (50개씩 배치).
    """
    result = {}
    if not video_ids or not youtube:
        return result
    unique_ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(unique_ids), 50):
        batch = unique_ids[i:i + 50]
        try:
            resp = youtube.videos().list(
                part="contentDetails",
                id=",".join(batch),
                maxResults=50,
            ).execute()
            for item in resp.get("items", []):
                vid = item.get("id", "")
                dur = item.get("contentDetails", {}).get("duration", "PT0S")
                result[vid] = _parse_iso_duration(dur)
        except Exception as e:
            print(f"  [duration조회] 배치 {i // 50 + 1} 실패: {e}")
        time.sleep(0.2)
    return result


def has_popular_panelist(title: str, transcript: str = "") -> bool:
    combined = title + " " + transcript
    return any(name in combined for name in POPULAR_PANELISTS)


def _normalize_channel_list(raw) -> list:
    result = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                ch_id   = item.get("id", "").strip()
                ch_name = item.get("name", ch_id)
                if ch_id:
                    result.append({"id": ch_id, "name": ch_name})
            elif isinstance(item, str) and item.strip():
                result.append({"id": item.strip(), "name": item.strip()})
    elif isinstance(raw, dict):
        for name, val in raw.items():
            if isinstance(val, dict):
                ch_id = val.get("id", "").strip()
                if ch_id:
                    result.append({"id": ch_id, "name": name})
            elif isinstance(val, str) and val.strip():
                result.append({"id": val.strip(), "name": name})
    return result


# ── 섹션1: 등록 채널 플레이리스트 수집 ─────────────────────────────────

def collect_section1_youtube(youtube, channels: dict) -> list:
    all_items  = []
    categories = [
        ("broadcast",  BROADCAST_HOURS,  "경제방송", False),  # 경제방송 채널 추가
        ("youtuber",   YOUTUBER_HOURS,   "유튜브",   False),
        ("securities", SECURITIES_HOURS, "증권사",   True),
    ]

    for cat_key, hours, source_type, securities_filter in categories:
        raw     = channels.get(cat_key, [])
        ch_list = _normalize_channel_list(raw)
        if not ch_list:
            print(f"  [섹션1-{cat_key}] 채널 없음 → 스킵")
            continue

        print(f"  [섹션1-{cat_key}] {len(ch_list)}개 채널 ({hours}h, type={source_type})")
        collected = 0

        for ch in ch_list:
            channel_id   = ch.get("id", "")
            channel_name = ch.get("name", channel_id)

            if not channel_id:
                continue

            if channel_id.startswith("@"):
                resolved = resolve_channel_id(youtube, channel_id)
                if not resolved:
                    print(f"    [스킵] {channel_name} — 채널ID 조회 실패")
                    continue
                channel_id = resolved

            videos = get_recent_videos_via_playlist(youtube, channel_id, hours)

            # 재생시간 배치 조회 → MIN_VIDEO_DURATION_SECONDS 미만 제외
            dur_map = _fetch_video_durations(youtube, [v["video_id"] for v in videos])

            for v in videos:
                title    = v.get("title", "")
                video_id = v.get("video_id", "")
                if is_ad_content(title):
                    continue
                if is_shorts(title):
                    print(f"    [쇼츠 제외-제목] {title[:40]}")
                    continue
                # 재생시간 기반 쇼츠 필터 (태그 없이 올린 우회 차단)
                dur_sec = dur_map.get(video_id)
                if dur_sec is not None and dur_sec < MIN_VIDEO_DURATION_SECONDS:
                    print(f"    [쇼츠 제외-길이] {dur_sec}s < {MIN_VIDEO_DURATION_SECONDS}s: {title[:40]}")
                    continue

                if is_stock_related(title):
                    transcript = get_transcript(v["video_id"])
                    stock_ok   = True
                else:
                    transcript = get_transcript(v["video_id"])
                    stock_ok   = is_stock_related(title, transcript)

                if not stock_ok:
                    continue

                if securities_filter and not is_securities_analysis(title, transcript):
                    continue

                description = v.get("description", "")
                # transcript 우선, 없으면 description, 없으면 title
                summary = transcript[:500] if transcript else (description[:500] if description else title)
                all_items.append({
                    "source_type":   source_type,
                    "source_name":   channel_name,
                    "title":         title,
                    "summary":       summary,
                    "description":   description,
                    "link":          f"https://www.youtube.com/watch?v={v['video_id']}",
                    "published":     v.get("published_at", ""),
                    "has_transcript": bool(transcript),
                })
                collected += 1

            time.sleep(0.2)

        print(f"   → {collected}건 수집")

    print(f"  [섹션1] 총 {len(all_items)}건")
    return all_items


# ── 섹션2: 패널리스트 이름 검색 수집 ────────────────────────────────────

def collect_panelist_youtube(youtube) -> list:
    """
    PANELIST-2: POPULAR_PANELISTS를 배치로 묶어 OR(|) 검색으로 후보를 모으고,
    실제 발언자/내용 검증은 다운스트림 Gemini 분석(gemini_youtube_analyzer.py)에
    위임하는 구조로 전면 재작성.

    이전 방식(이름당 suffix 5개를 결과 나올 때까지 순차 시도)은 최악의 경우
    22명 × 5suffix = 110회 search.list 호출 = 11,000유닛으로, search.list가
    100유닛/회인 YouTube Data API의 일일 기본 quota(10,000유닛)를 그 자체로
    초과해 quotaExceeded 에러를 양산했음.

    새 방식: 이름을 _PANELIST_BATCH_SIZE개씩 묶어 q="이름1|이름2|..."로
    1배치당 1회만 검색 (22명 ÷ 5 ≈ 5배치 × 100유닛 = 500유닛, 22배 절감).
    제목/자막에 이름이 정확히 포함돼야 한다는 엄격한 매칭은 제거하고,
    가벼운 주식관련성 체크(is_stock_related)만 거쳐 후보로 채택 —
    실제 누가 무슨 말을 했는지는 Gemini가 영상을 직접 보고 판단하게 함.

    CHANNEL-FILTER-1: 채널 품질 필터 추가.
    패널리스트 이름이 제목에 들어있어도 복제·저품질 채널일 수 있으므로:
    1. 블랙리스트 채널 (ID 또는 이름 기준) → 즉시 제외
    2. 등록 채널 화이트리스트 (channels.json broadcast/youtuber/securities) → 통과
    3. 비등록 채널 → channels.list로 구독자 수 배치 조회 (1유닛/호출)
       → _PANELIST_MIN_SUBSCRIBERS 미만이면 제외

    - 수집 기간: _PANELIST_HOURS (24h, 사용자 요청 반영)
    - source_type: "유튜브"
    - 중복 제거: video_id 기준
    - BUG-1: cutoff를 UTC 기준으로 계산 (publishedAfter는 UTC 기준 RFC3339 요구)
    """
    if not youtube:
        print("  [패널리스트 검색] YouTube 클라이언트 없음 → 스킵")
        return []

    # CHANNEL-FILTER-1: 채널 필터 세트 로드
    whitelist_ids, blacklist_ids, blacklist_names = _load_channel_filter_sets()

    # BUG-1 수정: KST → UTC 기준으로 변경
    # publishedAfter 파라미터는 UTC 기준 RFC3339 포맷("Z" suffix)을 요구함
    # 기존 코드는 KST 시각에 "Z"를 붙여 UTC인 척 전달 → 실제로 9시간 오차 발생
    cutoff    = datetime.now(UTC) - timedelta(hours=_PANELIST_HOURS)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_items = []
    seen_ids  = set()

    batches = [
        POPULAR_PANELISTS[i:i + _PANELIST_BATCH_SIZE]
        for i in range(0, len(POPULAR_PANELISTS), _PANELIST_BATCH_SIZE)
    ]

    print(f"  [패널리스트 검색] {len(POPULAR_PANELISTS)}명 → {len(batches)}배치, "
          f"최근 {_PANELIST_HOURS}h (예상 quota: {len(batches) * 100}유닛)")

    # CHANNEL-FILTER-1: 1단계 — 검색 후보를 수집하되 채널 ID를 기록해둠
    # 구독자 수 조회는 배치로 한꺼번에 처리 (quota 최소화)
    raw_candidates = []  # [(item_dict, channel_id, channel_name), ...]

    for batch_idx, batch in enumerate(batches, start=1):
        query = "|".join(batch)

        try:
            resp = youtube.search().list(
                part="snippet",
                q=query,
                type="video",
                order="date",
                publishedAfter=cutoff_str,
                maxResults=_PANELIST_MAX_RESULTS,
                relevanceLanguage="ko",
                regionCode="KR",
            ).execute()
        except HttpError as e:
            print(f"    [배치 {batch_idx}/{len(batches)} 검색 오류] {batch}: {e}")
            continue
        except Exception as e:
            print(f"    [배치 {batch_idx}/{len(batches)} 검색 오류] {batch}: {e}")
            continue

        items = resp.get("items", [])
        batch_count = 0

        for item in items:
            snippet    = item.get("snippet", {})
            video_id   = item.get("id", {}).get("videoId", "")
            if not video_id or video_id in seen_ids:
                continue

            title        = snippet.get("title", "").strip()
            channel_id   = snippet.get("channelId", "").strip()
            channel_name = snippet.get("channelTitle", "").strip()
            published_at = snippet.get("publishedAt", "")

            if not title or is_ad_content(title):
                continue
            if is_shorts(title, video_id):
                print(f"    [쇼츠 제외] {title[:40]}")
                continue

            # CHANNEL-FILTER-1: 블랙리스트 즉시 차단 (ID 또는 이름)
            if channel_id and channel_id in blacklist_ids:
                print(f"    [블랙리스트 차단] {channel_name} ({channel_id}): {title[:40]}")
                continue
            if channel_name and channel_name in blacklist_names:
                print(f"    [블랙리스트 차단] {channel_name}: {title[:40]}")
                continue

            # 주식 관련성 체크 (자막은 구독자 필터 통과 후에 가져옴)
            if not is_stock_related(title):
                continue

            # 패널리스트 매칭 태그 (추적용)
            matched = [name for name in batch if name in title]
            panelist_tag = ", ".join(matched) if matched else f"배치{batch_idx}({'/'.join(batch)})"

            # 발행일 파싱
            try:
                pub_dt = datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                ).astimezone(KST)
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pub_str = published_at

            desc = snippet.get("description", "")[:1000]

            seen_ids.add(video_id)
            raw_candidates.append({
                "video_id":    video_id,
                "title":       title,
                "channel_id":  channel_id,
                "channel_name": channel_name,
                "published":   pub_str,
                "description": desc,
                "_panelist":   panelist_tag,
            })
            batch_count += 1

        print(f"    배치 {batch_idx}/{len(batches)} [{'/'.join(batch)}]: {batch_count}건 후보")
        time.sleep(0.3)

    print(f"  [패널리스트 검색] 1차 후보 {len(raw_candidates)}건 → 재생시간 필터 시작")

    # 재생시간 배치 조회 (쇼츠 어뷰징 필터: 태그 없이 짧게 올리는 우회 차단)
    dur_map = _fetch_video_durations(youtube, [c["video_id"] for c in raw_candidates])
    short_rejected = 0
    filtered_candidates = []
    for c in raw_candidates:
        dur_sec = dur_map.get(c["video_id"])
        if dur_sec is not None and dur_sec < MIN_VIDEO_DURATION_SECONDS:
            print(f"    [쇼츠 제외-길이] {dur_sec}s: {c['title'][:40]}")
            short_rejected += 1
            continue
        filtered_candidates.append(c)
    if short_rejected:
        print(f"  [패널리스트 검색] 재생시간 필터로 {short_rejected}건 제외 "
              f"→ 남은 후보 {len(filtered_candidates)}건")
    raw_candidates = filtered_candidates

    print(f"  [패널리스트 검색] {len(raw_candidates)}건 → 채널 품질 필터 시작")

    # CHANNEL-FILTER-1: 2단계 — 비등록 채널만 추려서 구독자 수 배치 조회
    unregistered_ids = [
        c["channel_id"] for c in raw_candidates
        if c["channel_id"] and c["channel_id"] not in whitelist_ids
    ]
    unregistered_ids = list(set(unregistered_ids))

    subscriber_map = {}
    if unregistered_ids:
        print(f"  [채널필터] 비등록 채널 {len(unregistered_ids)}개 구독자 수 조회 중... "
              f"(예상 quota: {(len(unregistered_ids) + 49) // 50}유닛)")
        subscriber_map = _batch_fetch_subscriber_counts(youtube, unregistered_ids)

    # CHANNEL-FILTER-1: 3단계 — 필터 적용 및 자막 수집
    filtered_count  = 0
    rejected_count  = 0

    for c in raw_candidates:
        cid  = c["channel_id"]
        name = c["channel_name"]

        # 등록 채널이면 무조건 통과
        if cid in whitelist_ids:
            pass
        else:
            # 비등록 채널: 구독자 수 확인
            subs = subscriber_map.get(cid, -1)
            if subs == -1:
                # 조회 실패 → 보수적으로 통과 (차단하면 정상 채널도 잃을 수 있음)
                print(f"    [채널필터] 구독자 조회 실패 → 통과: {name}")
            elif subs < _PANELIST_MIN_SUBSCRIBERS:
                print(f"    [채널필터] 구독자 부족 ({subs:,}명 < {_PANELIST_MIN_SUBSCRIBERS:,}명) → 제외: {name}")
                rejected_count += 1
                continue
            else:
                print(f"    [채널필터] 비등록 채널 통과 ({subs:,}명): {name}")

        # 자막 수집 (필터 통과 후에 가져옴)
        transcript = get_transcript(c["video_id"])
        summary    = transcript[:500] if transcript else (c["description"][:500] if c["description"] else c["title"])

        # 자막 포함 시 주식 관련성 재확인 (제목에 없던 경우 보완)
        if not is_stock_related(c["title"], transcript):
            continue

        all_items.append({
            "source_type":    "유튜브",
            "source_name":    name,
            "title":          c["title"],
            "summary":        summary,
            "description":    c["description"],
            "link":           f"https://www.youtube.com/watch?v={c['video_id']}",
            "published":      c["published"],
            "_panelist":      c["_panelist"],
            "has_transcript": bool(transcript),
        })
        filtered_count += 1

    print(f"  [패널리스트 검색] 최종 {filtered_count}건 "
          f"(채널 품질 필터로 {rejected_count}건 제외, "
          f"실제 사용 quota: 검색 {len(batches) * 100}유닛 + "
          f"채널조회 {(len(unregistered_ids) + 49) // 50 if unregistered_ids else 0}유닛)")
    return all_items
