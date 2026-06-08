# invest-train 설계-구현 갭 분석 (Check)

**버전:** 1.0.0 | **작성일:** 2026-06-08 | **방식:** 인라인 교차검증 (설계서 §8/§9 ↔ 실제 파일)
**대상:** `docs/02-design/features/invest-train.design.md` ↔ 구현 코드/매니페스트

---

## 1. 종합 결과

| 지표 | 값 |
|------|-----|
| **Match Rate** | **96%** |
| 검증 항목 | 24 |
| 일치 | 23 |
| 부분/미흡 | 1 (CronJob 토큰 헤더 — 조건부, Low) |
| 치명 갭 | 0 |

> 본 프로젝트는 코드 선행 → 문서화(역설계) 흐름이라 설계-구현 일관성이 높다.

## 2. 항목별 검증

### 2.1 트리거 레이어 (train_server.py)
| 설계 | 구현 | 결과 |
|------|------|------|
| `POST /train` 202/409 | `train_server.py:84` lock 논블로킹+202/409 | ✅ |
| `GET /train/status` | `:107` 상태 스냅샷 | ✅ |
| `GET /train/health`,`/health` | `:118-119` `{status,running}` | ✅ |
| `GET /train` 안내 | `:125` | ✅ |
| `_train_lock`/`_train_state` | threading.Lock + dict | ✅ |
| `_check_token(TRAIN_TOKEN)` | 구현, 미설정 시 개방 | ✅ |
| py_compile | 통과 | ✅ |

### 2.2 이미지/패키지
| 설계 | 구현 | 결과 |
|------|------|------|
| `--workers 1` 강제 | Dockerfile CMD workers 1 | ✅ |
| fonts-nanum, COPY 2소스 | Dockerfile:7,13 | ✅ |
| fastapi/uvicorn + 학습패키지 | requirements.txt:7,8,14 | ✅ |

### 2.3 K8s 매니페스트
| 설계 | 구현 | 결과 |
|------|------|------|
| replicas 1 | deployment:10 | ✅ |
| mem limit 6Gi | deployment:69 | ✅ |
| probe `/train/health`, liveness timeout 5 | deployment:72,79,83 | ✅ |
| Ingress order 5, path /train, healthcheck /train/health | ingress:12,27,13 | ✅ |
| CronJob `30 19 * * *` (KST 04:30) | cronjob:12 | ✅ |
| Service ClusterIP 8080 | service.yaml | ✅ |
| Secret mlflow-auth (mlops 공유) | secret-template + deployment env | ✅ |

### 2.4 환경변수 (설계 §4)
| 설계 | 구현 | 결과 |
|------|------|------|
| MLFLOW URI/USER/PASS | deployment env (Secret 참조) | ✅ |
| ATHENA_DB/TABLE/S3_OUTPUT | deployment env | ✅ |
| MODEL_CLS/REG_NAME, ALIAS | deployment env (invest-app 동일값) | ✅ |
| AUTO_PROMOTE_CHAMPION=false | deployment env | ✅ |
| AWS_REGION/DEFAULT_REGION | deployment env | ✅ |

## 3. 식별된 갭

| # | 갭 | 심각도 | 설명 | 대응 |
|---|-----|--------|------|------|
| G1 | CronJob `X-Train-Token` 미주입 | Low | 설계 §7은 토큰 사용 시 CronJob 헤더 추가를 명시하나 현재 cronjob.yaml 은 주석 안내만. `TRAIN_TOKEN` 기본 미설정(개방)이라 현 시점 기능 영향 없음 | 외부 노출/토큰 운영 확정 시 cronjob args 에 `-H "X-Train-Token: <token>"` 추가 (설계 §10-4 후속) |

### 정보성(갭 아님)
- `PORT` env 미설정 → Dockerfile `ENV PORT=8080` + uvicorn `--port 8080` 고정으로 일치.
- `invest_train.py` 가 MLflow 자격증명을 명시 할당하지 않음 → mlflow client 가 env 자동 인식, deployment 가 주입하므로 정상.
- `serviceAccountName` 미지정 → invest-app 과 동일 노드롤/IRSA 사용 전제(INFRA_HANDOFF.md §1-②).

## 4. 판정
Match Rate 96% (≥90%) → **Check 통과**. 치명 갭 0. G1 은 토큰 운영 정책 확정 시점에 처리하는 후속 항목으로 분류.

다음: 배포 진행 → 배포 후 §9 검증 항목 실측 → `/pdca report invest-train`.
