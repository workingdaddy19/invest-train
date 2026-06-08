# invest-train 개발 계획서 (Plan)

**버전:** 1.0.0 | **작성일:** 2026-06-08 | **프로젝트:** `invest-train` (학습 전용 컨테이너)
**연관 문서:** `INVEST_TRAIN_개발계획서.md`(원 설계), invest-app `INVEST_API_설계문서.md`(추론)

---

## Executive Summary

| 관점 | 내용 |
|------|------|
| **Problem (문제)** | 부동산담보대출 심사모델(분류·회귀)을 주기적으로 재학습해야 하나, 추론 API(invest-app)에 학습을 얹으면 학습 피크 시 추론 Pod OOM·지연 위험이 있다. 또한 학습이 인코더 아티팩트를 저장하지 않으면 추론이 해시 폴백으로 동작해 결과가 무의미해진다. |
| **Solution (해결)** | 학습을 **독립 컨테이너(invest-train)** 로 분리하고, 추론과 동일 피처/전처리 로직을 유지한 `invest_train.py` 를 `/train` API(BackgroundTasks)로 트리거. 학습 직후 `encoders/`(le_dict·le_target·numeric_medians)를 MLflow 아티팩트로 저장해 추론과 전처리 일관성을 보장. |
| **Function·UX Effect (기능·UX 효과)** | 운영자/스케줄러가 `POST http://api.mlops.click/train` 한 번으로 즉시 202 응답을 받고, `GET /train/status` 로 진행/결과를 폴링. 추론 서비스 무중단. 새벽 CronJob 자동 재학습. |
| **Core Value (핵심 가치)** | 추론과 자원·장애 격리 + 학습-추론 전처리 일관성 + 무재배포 모델 교체(champion alias → `/model/reload`)로, 안전하고 반복 가능한 재학습 파이프라인 확보. |

---

## 1. 배경 및 목적

원 설계서(`INVEST_TRAIN_개발계획서.md`)는 "추론 이미지에 `/train` 엔드포인트를 추가하는 단일 이미지" 방식을 권장했다. 그러나 본 구현은 운영 요구에 따라 **invest-app 을 활용하지 않는 신규 분리 프로젝트**로 전환했다.

- **분리 이유**: 추론(주간, 상시 기동)과 학습(새벽, 일시적 고부하)의 자원·장애 격리. 학습 OOM 이 추론 가용성에 영향을 주지 않음.
- **유지 이유(전처리 일관성)**: 분리해도 `invest_train.py` 의 피처 정의(NUMERIC 24 / CAT 8 / FEATURE 32)와 전처리(숫자형 변환→중앙값 결측대체→LabelEncoding)는 invest-app `inference.py` 와 **반드시 동일**해야 한다. 인코더 아티팩트 공유로 silent skew 를 차단한다.

## 2. 목표 산출물

| 산출물 | 상태 | 설명 |
|--------|------|------|
| `train_server.py` | ✅ 완료 | 학습 트리거 FastAPI 서버 (`/train`, `/train/status`, `/train/health`) |
| `invest_train.py` | ✅ 완료(원본) | Athena 로드→전처리→2모델 학습→MLflow 등록 + 인코더 아티팩트 |
| `requirements.txt` | ✅ 완료 | 학습 패키지 + fastapi/uvicorn 통합 |
| `Dockerfile` | ✅ 완료 | python:3.11-slim + xgboost/한글폰트, `uvicorn train_server:app --workers 1` |
| `deploy.sh` | ✅ 완료 | 빌드/ECR 푸시/K8s 배포/학습 트리거 |
| `k8s/*` | ✅ 완료 | deployment, service, ingress(공유 ALB), cronjob, secret-template, namespace |
| 문서 | ✅ 완료 | README.md, DEPLOY.md, 본 Plan, INFRA_HANDOFF.md |

## 3. 아키텍처

```
스케줄러/운영자 ──POST──▶ http://api.mlops.click/train
                                  │ (공유 ALB shared-alb, path=/train, order 5)
                                  ▼
                      invest-train Pod (FastAPI, replicas=1, workers=1)
                                  │  BackgroundTasks + threading.Lock
                                  ▼
                      invest_train.run_training()
                         ├─ Athena 로드 (mlops.altinv_crel_train)
                         ├─ 전처리 (= inference.py 동일 로직)
                         ├─ XGB 분류/회귀 학습
                         └─ MLflow Registry 등록 + encoders/ 아티팩트
                                  │
                                  ▼
                      MLflow Registry ── champion alias(수동/AUTO_PROMOTE)
                                  │
                      invest-app 가 /model/reload 로 무재배포 반영
```

## 4. 핵심 설계 결정

| # | 결정 | 근거 |
|---|------|------|
| 1 | 학습 전용 FastAPI(`train_server.py`) 분리 | 추론 이벤트 루프/자원과 격리. `run_training()` 함수 경계 유지로 CronJob 직접 실행 전환도 용이 |
| 2 | **단일 워커·단일 레플리카 강제** | `_train_lock`/`_train_state` 가 프로세스 메모리에 존재 → 멀티 프로세스 시 락·상태 분산으로 깨짐 |
| 3 | 공유 ALB + 별도 Ingress (`/train` order 5 < `/` order 10) | 프로젝트 결합도 최소화. `api.mlops.click/train` 이 추론 catch-all 보다 우선 매칭 |
| 4 | `AUTO_PROMOTE_CHAMPION=false` 기본 | 초기 운영 안전. 지표 확인 후 사람이 champion 부여 |
| 5 | 인코더 아티팩트 저장(le_dict/le_target/numeric_medians) | 추론 전처리 일관성의 핵심 (원 설계서 §4.2) |
| 6 | `/train` 선택적 토큰 보호(`TRAIN_TOKEN`) | 외부 노출 시 무단 호출 자원 소모 방지 (원 설계서 §5.2) |

## 5. API 명세

| 메서드 | 경로 | 응답 | 설명 |
|--------|------|------|------|
| POST | `/train` | 202 / 409 | 백그라운드 학습 트리거(중복 시 409). `TRAIN_TOKEN` 설정 시 `X-Train-Token` 필요 |
| GET | `/train/status` | 200 | `running`, `started_at`, `finished_at`, `last_result` |
| GET | `/train/health` | 200 | ALB Target Group / K8s probe |

## 6. 범위 (Scope)

**In scope**: 학습 트리거 서버, 컨테이너화, K8s 매니페스트, 공유 Ingress, 새벽 CronJob, 인프라 인계 문서.

**Out of scope (후속/타팀)**: 추론(invest-app) 측 `numeric_medians.json` 기반 결측대체 고도화, 데이터 적재(datagen) 배치, 학습 데이터 품질 게이트(행 수/클래스 분포 검증), 모델 성능 자동 게이트.

## 7. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|--------|------|------|
| 피처 정의가 invest-app 과 어긋남 | 추론 무의미(silent skew) | 피처 변경 시 양 프로젝트 동시 수정 규칙. 인코더 아티팩트 공유 |
| 멀티 레플리카/워커 오설정 | 학습 항상 409, 상태 불일치 | `replicas:1`+`--workers 1` 고정, 코드/매니페스트/문서 명시 |
| Ingress order 역전 | `/train` 404 | `/train`(5) < `/`(10) 관계 유지 (DEPLOY.md §3) |
| Athena/S3 IAM 부재 | 학습 실패 | 인프라팀 IRSA/노드롤 권한 부여 (INFRA_HANDOFF.md) |
| 학습 피크 OOM | Pod 재시작 | memory limit 6Gi, 동시성 락, 새벽 스케줄 분리 |

## 8. 검증 계획 (Acceptance)

| # | 항목 | 기준 |
|---|------|------|
| 1 | 컨테이너 기동 | `GET /train/health` → 200 |
| 2 | 학습 트리거 | `POST /train` → 202, 동시 재호출 → 409 |
| 3 | 학습 완료 | `/train/status.last_result.status == success`, MLflow 신규 버전 생성 |
| 4 | 인코더 아티팩트 | 분류 run 에 `encoders/le_dict.pkl`, `le_target.pkl`, `numeric_medians.json` 존재 |
| 5 | 경로 라우팅 | `api.mlops.click/train` → invest-train, `/` → invest-app(추론) 정상 |
| 6 | 자동 스케줄 | CronJob(UTC 19:30) 트리거 후 status success |

## 9. 마일스톤

| 단계 | 작업 | 상태 |
|------|------|------|
| M1 | 학습 서버/컨테이너/매니페스트 개발 | ✅ 완료 |
| M2 | 인프라 인계(ECR/IAM/Secret) | ⏳ 인프라팀 (INFRA_HANDOFF.md) |
| M3 | 이미지 빌드·푸시·배포 | ⏳ 인프라/배포 |
| M4 | 학습→champion→추론 일관성 회귀 테스트 | ⏳ |
