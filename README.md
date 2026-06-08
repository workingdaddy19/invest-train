# INVEST Train — 부동산담보대출 심사모델 학습 서비스

invest-app(추론 API)과 **분리된 독립 컨테이너**로, 학습(`invest_train.py`)을
백그라운드로 트리거하는 얇은 FastAPI 서버(`train_server.py`)다.
추론과 같은 ALB(`api.mlops.click`)를 공유하되 **`/train` 경로**로 호출한다.

```
스케줄러 / 운영자 ──POST──▶ http://api.mlops.click/train
                                      │ (공유 ALB, path=/train)
                                      ▼
                         invest-train Pod (FastAPI, replicas=1, workers=1)
                                      │  BackgroundTasks + Lock
                                      ▼
                         invest_train.run_training()
                            ├─ Athena 학습데이터 로드 (mlops.altinv_crel_train)
                            ├─ 전처리 (= 추론과 동일 피처/로직)
                            ├─ XGB 분류/회귀 학습
                            └─ MLflow Registry 등록 (+ encoders/ 아티팩트)
```

> 핵심: 학습이 `encoders/le_dict.pkl`·`le_target.pkl`·`numeric_medians.json`을
> MLflow 아티팩트로 저장하여 **추론(invest-app)과 전처리 일관성**을 보장한다.

---

## 프로젝트 구성

| 파일 | 설명 |
|------|------|
| `train_server.py` | 학습 트리거 FastAPI 서버 (`/train`, `/train/status`, `/train/health`) |
| `invest_train.py` | 학습 파이프라인 (Athena 로드 → 전처리 → 2개 모델 학습 → MLflow 등록) |
| `requirements.txt` | 학습 + API 서버 통합 패키지 |
| `Dockerfile` | python:3.11-slim + xgboost/한글폰트, `uvicorn train_server:app` |
| `deploy.sh` | 빌드/ECR 푸시/K8s 배포/학습 트리거 |
| `k8s/` | Deployment, Service, Ingress(공유 ALB), Secret 템플릿 |

---

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/train` | 학습 백그라운드 트리거 → 즉시 `202` (중복 호출 시 `409`) |
| GET | `/train/status` | 진행/결과 폴링 |
| GET | `/train/health` | 헬스체크 (ALB Target Group / K8s probe) |

### 호출 예시

```bash
# 학습 시작 (즉시 202)
curl -s -X POST -H "Host: api.mlops.click" http://api.mlops.click/train

# 진행 상태/결과 확인
curl -s -H "Host: api.mlops.click" http://api.mlops.click/train/status
```

`/train/status` 응답 예:
```json
{
  "running": false,
  "started_at": "2026-06-09 04:30:00",
  "finished_at": "2026-06-09 04:30:19",
  "last_result": {
    "status": "success", "rows": 500, "elapsed_sec": 18.7,
    "classification": {"version": 5, "accuracy": 0.70, "roc_auc": 0.68},
    "regression": {"version": 4, "mae": 0.89, "rmse": 1.08, "r2": 0.21},
    "auto_promoted": false
  }
}
```

---

## ⚠️ 운영 제약 — 단일 워커/단일 레플리카

학습 동시성 락(`_train_lock`)과 진행상태(`_train_state`)는 **프로세스 메모리**에
존재한다. 여러 워커/레플리카로 띄우면 `POST /train`과 `GET /train/status`가
서로 다른 프로세스로 분산되어 락·상태가 깨진다.

- Dockerfile CMD: `--workers 1`
- Deployment: `replicas: 1`

학습 스케줄링은 컨테이너에 내장하지 않고 **사내 표준 스케줄러가 `POST /train`을 호출**한다
(클러스터 내 K8s CronJob 미사용 — 사내 정책). 부하가 커지면 동일 이미지를 배치 Job(별도 Pod)에서
`python invest_train.py`로 직접 실행하는 방식으로도 전환 가능하다(`run_training()` 함수 경계 유지).

---

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env      # MLflow/AWS 값 채우기
uvicorn train_server:app --host 0.0.0.0 --port 8080 --workers 1
# 학습 스크립트 단독 실행도 가능: python invest_train.py
```

배포는 [DEPLOY.md](DEPLOY.md) 참고.
