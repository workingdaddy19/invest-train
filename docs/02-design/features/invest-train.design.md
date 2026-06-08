# invest-train 설계서 (Design)

**버전:** 1.0.0 | **작성일:** 2026-06-08 | **프로젝트:** `invest-train`
**연관:** `docs/01-plan/features/invest-train.plan.md`, `INVEST_TRAIN_개발계획서.md`

---

## 1. 설계 개요

학습 전용 컨테이너. 추론(invest-app)과 분리하되 전처리/피처 정의를 공유하여 일관성을 보장한다.
구조는 **얇은 트리거 레이어(`train_server.py`) + 학습 도메인 로직(`invest_train.py`)** 의 2계층으로,
트리거 방식(HTTP `/train` ↔ CronJob 직접 실행)을 코드 변경 없이 교체할 수 있도록 `run_training()` 함수 경계를 유지한다.

```
┌─ Container (ECR: invest-train) ────────────────────────────┐
│  train_server.py  (FastAPI, 단일 워커)                      │
│    POST /train ──┐                                         │
│    GET /train/status                                       │
│    GET /train/health                                       │
│                  │ BackgroundTasks + threading.Lock         │
│                  ▼                                         │
│  invest_train.run_training()                               │
│    load_data() → preprocess() → train_classifier()         │
│                              → train_regressor()           │
│                              → log_charts()                │
│                  │                                         │
│                  ▼ mlflow                                  │
└──────────────────┼─────────────────────────────────────────┘
                   ▼
        MLflow Registry (+ encoders/ artifact)
```

## 2. 컴포넌트 설계

### 2.1 train_server.py (트리거 레이어)

| 요소 | 설계 | 비고 |
|------|------|------|
| `_train_lock` | `threading.Lock()` | 학습 동시 실행 방지. 논블로킹 `acquire(blocking=False)` |
| `_train_state` | dict: `running/started_at/finished_at/last_result` | 프로세스 메모리. **단일 워커 전제** |
| `_run_train_job()` | 백그라운드 스레드 실행체 | `run_training()` 호출, 예외 시 `last_result={status:error}`, finally 에서 lock release |
| `_check_token()` | `TRAIN_TOKEN` 설정 시 `X-Train-Token` 검증 | 미설정이면 통과(개방) |
| `POST /train` | lock 획득 실패→409, 성공→202 + BackgroundTasks 등록 | 즉시 반환(타임아웃 회피) |
| `GET /train/status` | `_train_state` 스냅샷 반환 | 폴링용 |
| `GET /train/health`, `/health` | `{status:healthy, running}` | ALB/probe |
| `GET /train` | 사용법 안내 JSON | |

**상태 전이**
```
idle ──POST /train(202)──▶ running ──run_training 성공──▶ idle(last_result=success)
  │                            │
  └─POST /train 재호출(409)     └─예외──▶ idle(last_result=error)
```

### 2.2 invest_train.py (도메인 로직, 기존 자산)

| 함수 | 입력 | 출력 | 책임 |
|------|------|------|------|
| `load_data()` | env(ATHENA_*) | DataFrame | `awswrangler.athena.read_sql_query` |
| `preprocess(df)` | raw df | `df, le_dict, le_target, numeric_medians` | 숫자형변환→중앙값결측대체→LabelEncoding |
| `_log_encoders(run_id,...)` | 인코더/중앙값 | — | `encoders/` 아티팩트 저장(분류 run) |
| `train_classifier(...)` | df, 인코더 | dict(run_id,version,metrics) | XGBClassifier 학습/등록 |
| `train_regressor(df)` | df | dict | XGBRegressor 학습/등록 |
| `log_charts(...)` | 학습 결과 | — | 4종 차트 PNG 아티팩트 |
| `run_training(df=None)` | — | summary dict | 오케스트레이션 엔트리포인트 |

> 본 단계에서 `invest_train.py` 는 수정하지 않는다(검증된 기존 자산). 트리거 레이어만 신규.

## 3. 인터페이스 명세

### 3.1 POST /train
- 요청 헤더(선택): `X-Train-Token: <token>` (`TRAIN_TOKEN` 설정 시 필수)
- 응답 202: `{"status":"accepted","started_at":"YYYY-MM-DD HH:MM:SS"}`
- 응답 409: `{"detail":"이미 학습이 실행 중입니다"}`
- 응답 401: `{"detail":"유효하지 않은 학습 토큰"}`

### 3.2 GET /train/status
```json
{
  "running": false,
  "started_at": "2026-06-09 04:30:00",
  "finished_at": "2026-06-09 04:30:19",
  "last_result": { "status": "success", "rows": 500, "elapsed_sec": 18.7,
    "classification": {"version": 5, "accuracy": 0.70, "roc_auc": 0.68},
    "regression": {"version": 4, "mae": 0.89, "rmse": 1.08, "r2": 0.21},
    "auto_promoted": false }
}
```

### 3.3 GET /train/health → `{"status":"healthy","running":false}`

## 4. 환경변수 설계

| 변수 | 기본값 | 소비처 | 비고 |
|------|--------|--------|------|
| `MLFLOW_TRACKING_URI` | mlflow.mlflow.svc:80 | invest_train | Registry |
| `MLFLOW_TRACKING_USERNAME/PASSWORD` | (Secret) | mlflow client | `mlflow-auth` |
| `ATHENA_DB` | mlops | load_data | |
| `ATHENA_TABLE` | altinv_crel_train | load_data | |
| `ATHENA_S3_OUTPUT` | s3://s3-an2-mlops/athena/ | load_data | |
| `MLFLOW_EXPERIMENT` | invest-crel-model | run_training | |
| `MODEL_CLS_NAME/REG_NAME` | invest-crel-* | register_model | invest-app 과 동일해야 함 |
| `MODEL_ALIAS` | champion | promotion | |
| `AUTO_PROMOTE_CHAMPION` | false | promotion | 초기 운영 false |
| `S3_BUCKET` | s3-an2-mlops | — | |
| `AWS_REGION/AWS_DEFAULT_REGION` | ap-northeast-2 | boto3/awswrangler | |
| `TRAIN_TOKEN` | "" (개방) | train_server | 선택 보안 |
| `PORT` | 8080 | uvicorn | |

## 5. 배포/인프라 설계

| 리소스 | 핵심 사양 | 근거 |
|--------|-----------|------|
| Deployment `invest-train` | **replicas:1**, container `--workers 1` | 인메모리 락/상태 일관성 |
| 리소스 | req cpu500m/mem1Gi, lim cpu2/**mem6Gi** | 학습 피크 OOM 대비(원설계 §6.3) |
| probe | readiness/liveness `/train/health`, liveness `timeoutSeconds:5` | 학습 중 흔들림 방지 |
| Service | ClusterIP 8080 | |
| Ingress `invest-train-ingress` | shared-alb, **order 5**, host api.mlops.click, path `/train`, healthcheck `/train/health` | catch-all `/`(10)보다 우선 |
| CronJob `invest-train-trigger` | `30 19 * * *`(UTC)=KST04:30, curl POST 내부서비스 | 새벽 자동 학습 |
| Secret `mlflow-auth` | mlops 공유 | invest-app 과 공유 |

### 5.1 라우팅 우선순위 (ALB)
```
api.mlops.click/train*  → invest-train  (order 5, 먼저 평가)
api.mlops.click/*       → invest-app    (order 10, catch-all)
```
ALB는 경로 미재작성 → 백엔드가 `/train`, `/train/status`, `/train/health` 원형 수신.

## 6. 동시성·장애 설계

| 시나리오 | 동작 |
|----------|------|
| 학습 중 추가 POST /train | 409 반환(락 보유) |
| 학습 중 GET /train/status | running:true 즉시 응답(이벤트 루프 비차단) |
| 학습 예외 | last_result={status:error,error}, 락 해제, 서버 생존 |
| Pod 재시작 중 학습 | in-flight 학습 유실(멱등 아님) → 재트리거 필요. MLflow 부분등록 가능성은 수동 정리 |
| 멀티 레플리카 오설정 | 락 분산 → 항상 409 또는 상태 불일치(금지) |

## 7. 보안 설계
- `/train` 외부 노출 시 `TRAIN_TOKEN` 으로 헤더 토큰 검증(선택). CronJob 은 동일 토큰 헤더 추가.
- MLflow 자격증명은 K8s Secret 주입(.env 운영 금지).
- IAM: Athena/Glue/S3 최소권한(IRSA/노드롤) — INFRA_HANDOFF.md.

## 8. 구현 매핑 (설계 ↔ 코드)

| 설계 항목 | 파일 | 상태 |
|-----------|------|------|
| 트리거 레이어 | `train_server.py` | ✅ |
| 도메인 로직 | `invest_train.py` | ✅(기존) |
| 패키지 | `requirements.txt` | ✅ |
| 이미지 | `Dockerfile`(workers 1, fonts-nanum) | ✅ |
| 워크로드 | `k8s/deployment.yaml`(replicas1, mem6Gi, probe) | ✅ |
| 노출 | `k8s/service.yaml`, `k8s/ingress.yaml`(order5) | ✅ |
| 스케줄 | `k8s/cronjob.yaml` | ✅ |
| 시크릿 | `k8s/secret-template.yaml` | ✅ |
| 배포 | `deploy.sh` | ✅ |

## 9. 검증 항목 (Design Acceptance)
- [ ] `/train/health` 200, `running` 필드 반영
- [ ] `POST /train` 202 → 동시 재호출 409
- [ ] 학습 완료 후 `/train/status.last_result.status==success`
- [ ] MLflow 분류 run 에 `encoders/le_dict.pkl·le_target.pkl·numeric_medians.json`
- [ ] `MODEL_CLS_NAME/REG_NAME` 가 invest-app 추론과 동일
- [ ] ALB `/train`→train, `/`→inference 라우팅
- [ ] CronJob 트리거 후 success

## 10. 미해결/후속 (Open Issues)
1. **학습 멱등성**: Pod 재시작 시 in-flight 유실 — 재시도/잠금 영속화는 후속(부하 증가 시 CronJob 직접 실행 전환으로 자연 해소).
2. **데이터 품질 게이트**: `load_data()` 후 행 수/클래스 분포 검증 미포함(원설계 §8 후속).
3. **추론 결측대체 고도화**: invest-app 이 `numeric_medians.json` 사용하도록 교체(타 프로젝트 작업).
4. **TRAIN_TOKEN 운영값**: 외부 노출 정책 확정 시 Secret 추가 및 CronJob 헤더 반영.
