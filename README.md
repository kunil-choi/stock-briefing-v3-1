# stock-briefing-v3-1

`stock-briefing-video`의 **morning_core**(장전) 영상 파이프라인이 소비하는 데이터 전용
백엔드 레포입니다. 사람이 보는 공개 브리핑 사이트는 여전히
[`stock-briefing-v3`](https://github.com/kunil-choi/stock-briefing-v3)이며, 이 레포는
그것과 **완전히 독립적으로** 매일 아침 별도 수집·분석을 수행합니다.

## 왜 필요한가

`stock-briefing-v3`는 애널리스트 리포트 수집(08:00 KST부터 대기, 08:30 강제진행)까지
끝난 뒤 단 한 번만 데이터를 발행합니다. morning_core 영상(07:10~08:20 KST, 증권사
리포트 제외)이 참조할 "이른 시각의 리포트 제외 스냅샷"이 v3에는 존재하지 않기 때문에,
이 레포가 별도로 시장데이터/뉴스/유튜브 수집 + Claude 분석을 수행해 그 스냅샷을
만듭니다.

**트레이드오프**: v3와 이 레포가 매일 아침 market/news/youtube/Gemini 수집과 Claude
분석을 각각 독립적으로 수행하므로, API 사용량(YouTube/Gemini/Claude)이 v3 단독
운영 대비 대략 2배가 됩니다.

## 파이프라인

`main.py`는 `stock-briefing-v3`의 `main.py`에서 **애널리스트 리포트 수집 단계를
제거한** 버전입니다:

1. 시장 데이터 (`collectors/market_collector.py`)
2. 뉴스 RSS (`collectors/news_collector.py`)
3. 유튜브 수집 + Gemini 영상 분석 (`collectors/youtube_collector.py`,
   `collectors/gemini_youtube_analyzer.py`)
4. Claude 분석 (`analyzer/ai_analyzer.py`, v3와 동일 모듈 — 애널리스트 데이터가
   없으므로 `brokerage_reports`는 자연히 빈 값으로 생성됨)

## 산출물

- `data/briefing_data.json` — v3의 `briefing_data.json`과 동일 스키마이나
  `brokerage_reports`가 비어있음. `stock-briefing-step1`이
  `raw.githubusercontent.com/kunil-choi/stock-briefing-v3-1/main/data/briefing_data.json`
  으로 직접 소비.
- `data/raw_YYYYMMDD.json` — 수집 원본(`all_data`) 전체. **v3와 달리 이 레포에서는
  `.gitignore`에서 제외하지 않고 커밋됩니다** — `stock-briefing-v3-2`가 뉴스/유튜브/
  Gemini 수집을 반복하지 않고 이 파일을 재사용해 애널리스트 리포트만 추가 수집하는
  구조이기 때문입니다.
- `docs/index.html` — GitHub Pages 프리뷰 페이지. `analyzer/html_generator.py`는
  v3/v3-2와 바이트 단위로 동일하게 유지한 채(수정 없음), `main.py`가 반환된 HTML
  문자열에 프리뷰 배너만 얹어 저장합니다(`_label_preview_html()`). **이건 정식
  `stock-briefing-v3` 공개 사이트를 대체하는 게 아닙니다** — v3는 지금 자동 실행이
  중단된 상태로 그대로 유지되며, step1/step2 영상이 실제로 업로드되기 시작하면
  v3가 다시 정식 공개 사이트 역할을 맡을 계획입니다. 그 전까지 이 페이지는 사람이
  step1 영상 제작에 쓰인 데이터를 눈으로 확인하기 위한 용도입니다.

## GitHub Pages 활성화 (최초 1회, 수동)

레포 Settings → Pages → Build and deployment → Source: **Deploy from a branch**
→ Branch: `main` / `/docs` 선택 후 Save. 이후 워크플로우가 `docs/index.html`을
커밋할 때마다 자동 반영되며, 다음 주소에서 확인할 수 있습니다:
`https://kunil-choi.github.io/stock-briefing-v3-1/`

## 트리거 체인

```
cron 07:00 KST (월~금)
  → main.py 실행 (목표 완료 ~07:30~07:45)
  → data/ 커밋·푸시
  → workflow_dispatch: stock-briefing-step1 (morning_core.yml)
  → workflow_dispatch: stock-briefing-v3-2 (main.yml)
```

## 필요 Secrets (레포 Settings → Secrets and variables → Actions)

| Secret | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude 분석 |
| `YOUTUBE_API_KEY` | YouTube 수집 |
| `GEMINI_API_KEY` | 유튜브 영상 분석(선택 — 없으면 스킵) |
| `GH_TOKEN` | `contents:write` + step1/v3-2 워크플로우 dispatch 권한 필요 |

기존 v3 레포에 등록된 값과 동일한 값을 재사용할 수 있습니다.

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
python main.py
```

## 다음 단계 (이 레포 범위 아님)

- 개체명 추출/scene_plan.json, 미디어 검색, 방송형 렌더러, 내러티브 플롯 알고리즘,
  TTS 고도화는 `stock-briefing-step1`/`stock-briefing-step2`에서 후속 단계로 다룹니다.
