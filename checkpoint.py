# checkpoint.py
"""
단계별 체크포인트 저장/복원.

GitHub Actions 러너는 실행마다 저장소를 새로 체크아웃하므로, main.py가
중간에 죽으면(타임아웃/취소 — 2026-07-23 아침 실행이 이렇게 됐다) 그 시점까지
모은 데이터는 커밋해두지 않는 한 사라진다. 각 수집 단계가 끝날 때마다 결과를
즉시 커밋해두면, 코드 수정 후 재실행할 때 이미 끝난 단계는 다시 하지 않고
남은 단계만 이어서 진행할 수 있다.

체크포인트는 오늘(KST) 날짜 단위로 data/_checkpoint/YYYYMMDD/ 에 쌓인다.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_ROOT = "data/_checkpoint"


def _today_str() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _checkpoint_dir() -> str:
    d = f"{_ROOT}/{_today_str()}"
    os.makedirs(d, exist_ok=True)
    return d


def prune_old() -> None:
    """오늘 날짜가 아닌 체크포인트 디렉터리는 정리한다(저장소 비대화 방지)."""
    if not os.path.isdir(_ROOT):
        return
    today = _today_str()
    for name in os.listdir(_ROOT):
        if name != today:
            shutil.rmtree(f"{_ROOT}/{name}", ignore_errors=True)


def load_stage(name: str):
    """오늘자 체크포인트가 있으면 로드해서 반환, 없으면 None."""
    path = f"{_checkpoint_dir()}/{name}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [체크포인트] {name} 로드 실패({e}) → 재수집")
        return None


def save_stage(name: str, data) -> None:
    """체크포인트 저장 + 즉시 커밋·푸시 (중간에 죽어도 진행 상황이 남도록)."""
    path = f"{_checkpoint_dir()}/{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    _commit_and_push([path], f"🧩 체크포인트: {name} ({_today_str()})")


def is_done() -> bool:
    return os.path.exists(f"{_checkpoint_dir()}/DONE")


def mark_done(extra_paths: list) -> None:
    """
    최종 산출물(briefing_data.json, raw_*.json, docs/index.html)과 DONE
    마커를 한 커밋으로 묶어 저장한다. 이후 워크플로우의 마지막 커밋 스텝은
    변경사항이 없어 스킵되는 게 정상이며, 여기서 실패한 부분에 대한 백업
    역할만 한다.
    """
    done_path = f"{_checkpoint_dir()}/DONE"
    with open(done_path, "w", encoding="utf-8") as f:
        f.write(datetime.now(KST).isoformat())
    _commit_and_push(
        [done_path, *extra_paths],
        f"✅ V3_1 morning_core 데이터 갱신: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST",
    )


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _commit_and_push(paths: list, message: str) -> None:
    _git("config", "--local", "user.email", "action@github.com")
    _git("config", "--local", "user.name", "GitHub Action")
    _git("add", *paths)
    if _git("diff", "--cached", "--quiet").returncode == 0:
        return  # 변경사항 없음

    commit = _git("commit", "-m", message)
    if commit.returncode != 0:
        print(f"  [체크포인트] commit 실패: {commit.stderr.strip()}")
        return

    for attempt in range(1, 4):
        push = _git("push")
        if push.returncode == 0:
            print(f"  [체크포인트] 저장·커밋 완료: {', '.join(paths)}")
            return
        print(f"  [체크포인트] push 실패({attempt}/3) → rebase 후 재시도")
        _git("pull", "--rebase", "origin", "main")
    print(f"  [체크포인트] push 최종 실패 — 로컬에는 저장됨: {', '.join(paths)}")
