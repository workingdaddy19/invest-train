# ============================================================
# INVEST 부동산담보대출 투자적격 심사 - 모델 학습(Train) API 서버
# - invest-app(추론)과 분리된 독립 컨테이너 (별도 ECR: invest-train)
# - invest_train.run_training() 을 백그라운드로 트리거하는 얇은 FastAPI 래퍼
# - 호출 경로: http://api.mlops.click/train  (ALB Ingress 공유, path=/train)
#
# ⚠️ 운영 제약 (반드시 준수)
#   학습 동시성 락(_train_lock)과 진행상태(_train_state)는 프로세스 메모리에
#   존재한다. 여러 워커/레플리카로 띄우면 POST /train 과 GET /train/status 가
#   서로 다른 프로세스에 분산되어 락·상태가 깨진다.
#   → 반드시 단일 워커(--workers 1) · 단일 레플리카(replicas: 1) 로 운영한다.
#
# 엔드포인트 (ALB 는 경로를 그대로 전달하므로 /train 접두 포함)
#   POST /train          : 학습 백그라운드 트리거, 즉시 202 (중복 시 409)
#   GET  /train/status   : 진행/결과 폴링
#   GET  /train/health   : ALB Target Group / K8s probe 헬스체크
#   GET  /health         : Pod 직접 헬스체크 (편의)
#   GET  /train          : 간단한 안내 페이지
# ============================================================

import os
import threading
import logging
from datetime import datetime

import pytz
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from fastapi.responses import JSONResponse
from typing import Optional

import invest_train  # 동일 디렉터리에 포함된 학습 스크립트

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_server")

KST = pytz.timezone("Asia/Seoul")

# ── 보안: TRAIN_TOKEN 이 설정되면 X-Train-Token 헤더 일치를 요구 (선택)
#    내부 스케줄러(CronJob)만 호출하도록 토큰 보호 (개발계획서 §5.2)
TRAIN_TOKEN = os.getenv("TRAIN_TOKEN", "").strip()

# ── 학습 동시 실행 방지 + 마지막 결과 보관
_train_lock = threading.Lock()
_train_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
}


def _check_token(token: Optional[str]):
    """TRAIN_TOKEN 설정 시 헤더 검증. 미설정이면 통과."""
    if TRAIN_TOKEN and token != TRAIN_TOKEN:
        raise HTTPException(status_code=401, detail="유효하지 않은 학습 토큰")


def _run_train_job():
    """백그라운드 스레드에서 실제 학습 수행."""
    try:
        result = invest_train.run_training()           # Athena 로드 → 학습 → MLflow 등록
        _train_state["last_result"] = result
        logger.info("학습 성공")
    except Exception as e:                              # noqa: BLE001
        _train_state["last_result"] = {"status": "error", "error": str(e)}
        logger.exception("학습 실패")
    finally:
        _train_state["running"] = False
        _train_state["finished_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        _train_lock.release()


app = FastAPI(
    title="INVEST Train API",
    description="부동산담보대출 심사모델 학습 트리거 서버",
    version="1.0.0",
)


@app.post("/train")
def train(background_tasks: BackgroundTasks,
          x_train_token: Optional[str] = Header(default=None)):
    """학습을 백그라운드로 트리거하고 즉시 202 반환 (장시간 요청/타임아웃 방지)."""
    _check_token(x_train_token)

    # 논블로킹 락 획득 — 실패 시 이미 학습 중
    if not _train_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="이미 학습이 실행 중입니다")

    _train_state.update(
        running=True,
        started_at=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=None,
    )
    background_tasks.add_task(_run_train_job)
    logger.info("학습 요청 수락 → 백그라운드 실행")
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "started_at": _train_state["started_at"]},
    )


@app.get("/train/status")
def train_status():
    """학습 진행/결과 폴링."""
    return {
        "running": _train_state["running"],
        "started_at": _train_state["started_at"],
        "finished_at": _train_state["finished_at"],
        "last_result": _train_state["last_result"],
    }


@app.get("/train/health")
@app.get("/health")
def health():
    """헬스체크 — 학습이 백그라운드 스레드에서 돌아도 이벤트 루프는 응답 가능."""
    return {"status": "healthy", "running": _train_state["running"]}


@app.get("/train")
def index():
    """간단한 안내."""
    return {
        "service": "invest-train",
        "usage": {
            "trigger": "POST /train",
            "status": "GET /train/status",
            "health": "GET /train/health",
        },
        "running": _train_state["running"],
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    # 반드시 단일 워커 — 인메모리 락/상태 일관성 보장
    uvicorn.run("train_server:app", host="0.0.0.0", port=port, workers=1)
