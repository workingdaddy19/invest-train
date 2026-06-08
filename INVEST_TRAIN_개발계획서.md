# INVEST 학습(Train) 기능 추가 개발 계획서
**버전:** 1.0.0 | **작성일:** 2026-06-08 | **대상 프로젝트:** `invest-app` (추론 API 컨테이너)
**연관 문서:** `INVEST_API_설계문서.md` (추론 API v1.0.0)

---

## 1. 핵심 질문에 대한 결론

> **"추론 API 이미지에 모델 학습(Train.py) 기능을 추가하는 것은 무리일까?"**

**무리가 아니다. 권장 가능한 구성이다.** 단, 아래 두 가지 전제를 지켜야 한다.

1. **이미지는 공유하되, 실행 경로는 분리한다.** 추론 API와 학습 코드는 *같은 Docker 이미지* 안에 두어 전처리·피처 정의·라이브러리 버전을 100% 일치시킨다. 그러나 학습은 추론 요청 처리와 같은 이벤트 루프에서 동기 실행하지 않고 **백그라운드 비동기 + 동시성 가드**로 분리한다.
2. **시간대가 겹치지 않는다.** 추론은 업무시간(09~18시) 소량 트래픽, 학습은 새벽~09시 이전 1회. 이 스케줄 분리가 단일 Pod 공존을 현실적으로 만든다.

### 1.1 왜 단일 이미지가 유리한가
추론(`inference.py`)과 학습(`invest_train.py`)은 **동일한 `NUMERIC_COLS` / `CAT_COLS` / `FEATURE_COLS`** 와 동일한 전처리 로직(숫자형 변환 → 중앙값 결측대체 → LabelEncoding)을 공유해야 한다. 이미지를 분리하면 한쪽만 컬럼이 바뀌었을 때 *조용한 전처리 불일치(silent skew)* 가 발생해 추론 품질이 무너진다. 같은 이미지·같은 소스 트리는 이 불일치를 구조적으로 차단한다.

### 1.2 단일 Pod /train 방식의 유일한 리스크와 대응
| 리스크 | 원인 | 대응 |
|--------|------|------|
| 학습 중 추론 Pod 메모리 급증 → OOM Kill | XGBoost 학습 + DataFrame 적재가 추론 상시 메모리에 더해짐 | Pod `resources.limits.memory` 상향(아래 §6), 동시성 락으로 학습 1건만 허용, 학습은 새벽에만 트리거 |
| 장시간 요청으로 인한 HTTP 타임아웃 | 학습이 수십 초~분 단위 | `/train` 은 **즉시 202 반환 + 백그라운드 실행**, 상태는 `/train/status` 로 폴링 |
| 학습 중 추론 지연 | CPU 경합 | 학습 시간대(새벽)와 추론 시간대(주간) 분리로 회피, 필요 시 HPA 별도 |

> **운영 성숙 단계 권고:** 트래픽·학습 부하가 커지면 동일 이미지를 **K8s CronJob**(별도 Pod)으로 띄우는 방식으로 무중단 전환할 수 있다. `/train` 엔드포인트 방식과 CronJob 방식 모두 `invest_train.run_training()` 동일 함수를 호출하므로, 코드 변경 없이 트리거만 교체된다.

---

## 2. 목표 산출물

| 산출물 | 상태 | 설명 |
|--------|------|------|
| `invest_train.py` | **본 계획서와 함께 생성됨** | 데이터 로드→전처리→2개 모델 학습→MLflow 등록(+인코더 아티팩트). CLI/엔드포인트 양용 |
| `train_requirements.txt` | **본 계획서와 함께 생성됨** | 학습 추가 패키지(awswrangler, matplotlib 등) |
| `inference.py` `/train` 엔드포인트 추가 | 개발 예정 (§5) | 비동기 학습 트리거 + 상태 조회 |
| `Dockerfile` / `requirements.txt` 갱신 | 개발 예정 (§6) | 학습 패키지·폰트 병합 |
| `k8s/deployment.yaml` 리소스 상향 | 개발 예정 (§6) | 학습 피크 대비 memory limit |

---

## 3. 전체 아키텍처

```
                         ┌──────────────── 동일 Docker 이미지 (1개 ECR) ────────────────┐
   업무시스템            │                                                              │
   (심사화면) ──POST────▶│  inference.py  (FastAPI, 상시 기동)                          │
        09~18시 소량     │    ├─ /predict        : MLflow champion 모델 추론             │
                         │    ├─ /model/reload   : 신규 champion 핫리로드               │
                         │    └─ /train (신규)   : 백그라운드 학습 트리거 ──┐           │
                         │                                                  ▼           │
   스케줄러 ──POST──────▶│                          invest_train.run_training()         │
   (새벽~09시 이전)      │                            ├─ Athena 학습데이터 로드          │
   api.mlops.click/train │                            ├─ 전처리(= 추론과 동일 로직)      │
                         │                            ├─ XGB 분류/회귀 학습              │
                         │                            └─ MLflow 등록 ──────┐            │
                         └──────────────────────────────────────────────────┼───────────┘
                                                                            ▼
                                              MLflow Registry  ◀── champion alias 부여
                                              + Artifact(encoders/, charts)
                                                     │
                                          /model/reload 로 추론에 반영
```

핵심 흐름: **학습이 MLflow Registry에 새 버전을 등록 → (검증 후) champion alias 부여 → `/model/reload` 로 추론 Pod가 무재배포 교체.**

---

## 4. invest_train.py 설계

### 4.1 노트북 → 스크립트 매핑
`invest_crel_datagen_2.ipynb`(합성 데이터)와 `invest_crel_model_2.ipynb`(학습)을 운영 스크립트로 통합했다. 운영에서는 합성 데이터 생성을 쓰지 않고 **Athena 실데이터(`mlops.altinv_crel_train`)** 를 로드한다(데이터 생성은 별도 datagen 배치/노트북 책임).

| 노트북 셀 | invest_train.py 함수 |
|-----------|----------------------|
| 1-3 Athena 로딩 | `load_data()` |
| 1-4 전처리/인코딩 | `preprocess()` → le_dict·le_target·중앙값 **반환** |
| 1-5 분류 학습/등록 | `train_classifier()` |
| 1-6 회귀 학습/등록 | `train_regressor()` |
| 1-7 4종 차트 | `log_charts()` |
| 전체 오케스트레이션 | `run_training()` (엔트리포인트) |

### 4.2 ★ 가장 중요한 변경 — 인코더 아티팩트 저장
현재 `inference.py`의 `ModelStore._load_encoders()`는 MLflow 분류 모델 run에서 다음 경로를 로드하도록 작성돼 있다.

```
encoders/le_dict.pkl     # 범주형 LabelEncoder dict
encoders/le_target.pkl   # 타깃(Y/N) LabelEncoder
```

그런데 **첨부된 학습 노트북은 이 인코더들을 저장하지 않는다.** 이대로 운영하면 추론은 `le_dict` 부재 → `hash(val) % 100` 해시 폴백으로 동작하여 **학습 시 인코딩과 전혀 다른 정수**가 모델에 입력된다. 즉 추론 결과가 사실상 무의미해진다.

`invest_train.py`는 이 갭을 해결한다. `_log_encoders()`가 학습 직후 분류 모델 run에 다음을 저장한다.

```
encoders/le_dict.pkl
encoders/le_target.pkl
encoders/numeric_medians.json   # 결측 대체용 학습셋 중앙값 (추론 고도화용, 신규)
```

> **추론 측 후속 권장 작업:** `inference.preprocess()`의 숫자형 결측 대체를 현재 `fillna(0)` 에서 `numeric_medians.json` 기반으로 교체하면 학습-추론 결측 처리까지 완전 일치한다. (필수는 아니나 권장)

### 4.3 모델 승격(promotion) 정책
- `AUTO_PROMOTE_CHAMPION=false` (기본): 학습은 **새 버전 등록까지만** 수행. 사람이 지표 확인 후 champion alias 수동 부여 → 안전.
- `AUTO_PROMOTE_CHAMPION=true`: 학습 직후 새 버전에 champion 자동 부여. 완전 자동 재학습 파이프라인용. 초기 운영에서는 **false 권장.**

---

## 5. `/train` 엔드포인트 구현 가이드 (inference.py 추가)

추론과 같은 이벤트 루프를 막지 않도록 **FastAPI BackgroundTasks + 전역 락**으로 구현한다. 아래는 `inference.py`에 추가할 코드 스니펫이다.

```python
# inference.py 상단 import 영역에 추가
import threading
from fastapi import BackgroundTasks
import invest_train  # 동일 이미지에 포함

# 학습 동시 실행 방지 + 마지막 결과 보관
_train_lock = threading.Lock()
_train_state = {"running": False, "last_result": None, "started_at": None}


def _run_train_job():
    try:
        result = invest_train.run_training()          # Athena 로드 → 학습 → 등록
        _train_state["last_result"] = result
    except Exception as e:
        _train_state["last_result"] = {"status": "error", "error": str(e)}
        logger.exception("학습 실패")
    finally:
        _train_state["running"] = False
        _train_lock.release()


@app.post("/train")
def train(background_tasks: BackgroundTasks):
    """학습을 백그라운드로 트리거하고 즉시 202 반환 (장시간 요청 방지)."""
    if not _train_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="이미 학습이 실행 중입니다")
    _train_state.update(running=True, started_at=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))
    background_tasks.add_task(_run_train_job)
    return JSONResponse(status_code=202,
                        content={"status": "accepted", "started_at": _train_state["started_at"]})


@app.get("/train/status")
def train_status():
    """학습 진행/결과 폴링."""
    return {"running": _train_state["running"],
            "started_at": _train_state["started_at"],
            "last_result": _train_state["last_result"]}
```

### 5.1 호출 예시
```bash
# 학습 시작 (즉시 202 반환)
curl -s -X POST -H "Host: api.mlops.click" http://api.mlops.click/train

# 진행 상태/결과 확인
curl -s -H "Host: api.mlops.click" http://api.mlops.click/train/status
```

`/train/status` 응답 예:
```json
{
  "running": false,
  "started_at": "2026-06-09 04:30:00",
  "last_result": {
    "status": "success", "rows": 500, "elapsed_sec": 18.7,
    "classification": {"version": 5, "accuracy": 0.70, "roc_auc": 0.68},
    "regression": {"version": 4, "mae": 0.89, "rmse": 1.08, "r2": 0.21},
    "auto_promoted": false
  }
}
```

### 5.2 보안
`/train` 은 외부 노출 시 무단 호출로 자원을 소모할 수 있다. `/model/reload` 와 동일하게 **Ingress 외부 노출에서 제외**하거나, 내부 스케줄러만 접근하는 ClusterIP 경로 또는 헤더 토큰 검증을 둔다.

---

## 6. 패키징 / 인프라 변경

### 6.1 requirements 병합
추론 이미지에서 학습까지 돌리려면 `train_requirements.txt`의 추가분(`awswrangler`, `matplotlib`, `seaborn`)을 추론 `requirements.txt`에 병합한다. `fastapi`/`uvicorn`은 이미 추론에 존재하므로 중복 추가 불필요.

### 6.2 Dockerfile 변경점
- 학습 차트의 한글 폰트를 위해 `fonts-nanum` 설치(또는 `NanumGothic.ttf` COPY).
- `COPY invest_train.py .` 추가.
- 엔트리포인트(`uvicorn inference:app`)는 그대로. 학습은 엔드포인트로 트리거되므로 CMD 변경 없음.

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libgomp1 fonts-nanum && rm -rf /var/lib/apt/lists/*
COPY inference.py invest_train.py requirements.txt .
```

### 6.3 K8s 리소스 (deployment.yaml)
학습 피크를 흡수하도록 memory limit을 상향한다(추론 단독은 1~2Gi로 충분하나 학습 동시 적재 대비).

```yaml
resources:
  requests: { cpu: "500m", memory: "1Gi" }
  limits:   { cpu: "2",    memory: "6Gi" }   # 학습 피크 대비 상향 (기존 4Gi → 6Gi)
```

> 학습 시간대에 추론 readiness가 흔들리지 않도록 `livenessProbe.timeoutSeconds` 를 여유 있게(예: 5초) 둔다.

### 6.4 스케줄링 (새벽 자동 학습)
선택한 `/train` 엔드포인트 방식의 스케줄은 **K8s CronJob이 curl로 엔드포인트를 호출**하는 가벼운 트리거로 구성한다(학습 로직은 추론 Pod 안에서 실행).

```yaml
apiVersion: batch/v1
kind: CronJob
metadata: { name: invest-train-trigger, namespace: invest-inference }
spec:
  schedule: "30 19 * * *"          # UTC 19:30 = KST 04:30 (새벽)
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: curl
            image: curlimages/curl:8.8.0
            args: ["-s","-X","POST",
                   "http://invest-inference.invest-inference.svc.cluster.local:8080/train"]
```

> KST 변환 주의: cron은 UTC 기준이므로 KST 04:30 = UTC 19:30(전일). 사내 정책상 학습을 09시 직전에 끝내려면 데이터 적재 완료 시각 + 학습 소요(수십 초)를 고려해 04~07시 사이로 설정.

---

## 7. 개발 단계 및 체크리스트

| 단계 | 작업 | 산출물 | 검증 |
|------|------|--------|------|
| 1 | `invest_train.py` 통합 (완료) | 학습 스크립트 | `py_compile` 통과, 피처 정의 inference 일치 확인됨 |
| 2 | 로컬/Jupyter에서 `run_training()` 1회 실행 | MLflow 새 버전 + `encoders/` 아티팩트 | Registry에 버전 생성, `encoders/le_dict.pkl` 존재 확인 |
| 3 | `inference.py` 에 `/train`·`/train/status` 추가 | 엔드포인트 | 로컬 202 응답, 동시 호출 시 409 |
| 4 | `requirements.txt`·`Dockerfile` 병합, 이미지 재빌드 | 신규 ECR 이미지 | 컨테이너 내 `python -c "import invest_train"` 성공 |
| 5 | K8s deployment 리소스 상향 + CronJob 트리거 배포 | 매니페스트 | 새벽 트리거 후 `/train/status` success |
| 6 | 학습→`/model/reload`→`/predict` 일관성 회귀 테스트 | 테스트 결과 | 동일 입력에 학습 전후 추론값 합리적 변화 |

### 7.1 노트북 → 운영 연결 체크리스트
- [x] `NUMERIC_COLS`(24) / `CAT_COLS`(8) / `FEATURE_COLS`(32) inference.py와 동일 — **검증 완료**
- [ ] `encoders/le_dict.pkl`, `le_target.pkl` MLflow 아티팩트 등록 확인
- [ ] `encoders/numeric_medians.json` 저장 확인 (추론 결측 대체 고도화 시 사용)
- [ ] champion alias 정책 결정 (`AUTO_PROMOTE_CHAMPION` 기본 false)
- [ ] `/train` Ingress 외부 노출 제외 또는 토큰 보호
- [ ] CronJob 스케줄 KST/UTC 변환 검증

---

## 8. 리스크 및 대응 요약

| 리스크 | 영향 | 대응 |
|--------|------|------|
| 학습-추론 전처리 불일치 | 추론 무의미 | 단일 이미지 + 인코더 아티팩트 공유 (§4.2) |
| 학습 중 추론 Pod OOM | 추론 중단 | memory limit 상향, 동시성 락, 새벽 스케줄 분리 |
| 자동 champion 승격 후 성능 저하 모델 배포 | 추론 품질 저하 | 기본 수동 승격, 지표 게이트 후 alias 부여 |
| `/train` 무단 호출 | 자원 소모 | 내부 노출 한정 / 토큰 검증 |
| 데이터 적재 지연으로 빈 학습 | 잘못된 모델 | `load_data()` 후 행 수·클래스 분포 검증 게이트 추가(후속) |

---

## 9. 결론
추론 이미지에 학습을 더하는 것은 **권장 가능한 구성**이며, 시간대 분리(주간 추론 / 새벽 학습)와 소량 트래픽이라는 현재 조건에서 단일 이미지·단일 Pod로 충분히 운영된다. 반드시 지킬 한 가지는 **인코더 아티팩트 공유를 통한 전처리 일관성**이며, 이는 `invest_train.py`에 이미 반영되어 있다. 향후 부하 증가 시 동일 코드를 CronJob(별도 Pod)으로 무중단 분리할 수 있도록 `run_training()` 함수 경계를 유지했다.
