# invest-train 인프라 배포 요청서 (Infra Handoff)

**요청일:** 2026-06-08 | **요청자:** MLOps 개발 | **대상:** 인프라 담당부서
**서비스:** `invest-train` (부동산담보대출 심사모델 **학습 전용** 컨테이너)
**관계:** 추론 서비스 `invest-app`(invest-inference)과 **동일 클러스터·동일 ALB·동일 네임스페이스** 공유, 별도 컨테이너

---

## 0. 한눈에 보기

| 항목 | 값 |
|------|-----|
| ECR 리포지토리 | `891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train` |
| 리전 | `ap-northeast-2` |
| EKS Namespace | `mlops` (invest-app 과 동일) |
| Deployment / Service | `invest-train` (ClusterIP, 8080) |
| 외부 진입 | `http://api.mlops.click/train` (공유 ALB 그룹 `shared-alb`) |
| 레플리카 | **1 고정** (인메모리 학습 락/상태 일관성) |
| 리소스 | requests cpu 500m/mem 1Gi, limits cpu 2/mem **6Gi** |

---

## 1. 인프라팀 선행 작업 (Action Required)

아래 4가지는 **개발팀 권한 밖**이라 인프라팀 처리가 필요합니다.

### ① ECR 리포지토리 생성
```bash
aws ecr create-repository \
  --repository-name invest-train \
  --region ap-northeast-2
```

### ② IAM 권한 — Athena / Glue / S3 (가장 중요)
학습 Pod 는 `awswrangler` 로 Athena 를 조회하고 S3(`s3-an2-mlops`)에 쿼리 결과를 씁니다.
**invest-app(추론) Pod 와 동일한 노드 IAM Role 또는 IRSA** 에 아래 권한이 포함되어야 합니다.

필요 액션(최소):
- `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution`
- `glue:GetTable`, `glue:GetDatabase`, `glue:GetPartitions`
- `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` (버킷 `s3-an2-mlops`, 특히 `s3://s3-an2-mlops/athena/`)

> invest-app 추론 Pod 가 이미 같은 버킷/Athena 권한을 갖고 있다면, invest-train 도 **동일 노드그룹/IRSA 를 사용**하므로 추가 작업이 없을 수 있습니다. 확인만 부탁드립니다.

### ③ MLflow 인증 Secret (이미 있으면 생략)
invest-app 이 `mlops` 네임스페이스에 `mlflow-auth` Secret 을 이미 생성했다면 **그대로 공유**합니다. 없을 경우에만:
```bash
kubectl create secret generic mlflow-auth \
  --from-literal=username=<MLFLOW_USERNAME> \
  --from-literal=password=<MLFLOW_PASSWORD> \
  -n mlops
```

### ④ Namespace (이미 있으면 생략)
`mlops` 네임스페이스가 없으면:
```bash
kubectl create namespace mlops
```

---

## 2. 배포 절차

개발팀이 제공한 번들(`k8s/`, `Dockerfile`, `deploy.sh`) 기준.

### 방법 A — 스크립트 일괄 (권장)
```bash
# 빌드 + ECR 푸시 + K8s 배포
bash deploy.sh
```

### 방법 B — 수동 단계
```bash
# 1) 이미지 빌드 & 푸시
aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train
docker build -t invest-train:latest .
docker tag invest-train:latest 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train:latest
docker push 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train:latest

# 2) 매니페스트 적용
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/cronjob.yaml

# 3) 롤아웃 확인
kubectl rollout status deployment/invest-train -n mlops --timeout=180s
kubectl get pods -n mlops -l app=invest-train
```

---

## 3. ⚠️ Ingress 경로 공유 — 반드시 확인

invest-train 은 invest-app 과 **같은 ALB(`group.name: shared-alb`)** 를 공유합니다.
`aws-load-balancer-controller` 가 같은 group 의 Ingress 들을 하나의 ALB 로 병합합니다.

| Ingress | host | path | `group.order` | 평가 순서 |
|---------|------|------|:-------------:|-----------|
| **invest-train-ingress** (신규) | api.mlops.click | `/train` | **5** | 먼저 (구체적) |
| invest-inference-ingress (기존) | api.mlops.click | `/` | 10 | 나중 (catch-all) |

- `group.order` 가 **작을수록 먼저 평가**됩니다. `/train`(5)이 catch-all `/`(10)보다 우선 매칭되어야 정상 동작합니다.
- **기존 invest-app 의 `/`(order 10)는 변경하지 마세요.** 만약 추론 Ingress order 를 5 이하로 낮추면 `/train` 이 가려져 404 가 됩니다.
- ALB 는 경로를 재작성하지 않으므로 백엔드는 `/train`, `/train/status`, `/train/health` 를 그대로 수신합니다.

---

## 4. 배포 검증 체크리스트

```bash
# 1) Pod Ready
kubectl get pods -n mlops -l app=invest-train          # Running 1/1

# 2) 헬스체크 (Pod 내부)
POD=$(kubectl get pod -n mlops -l app=invest-train -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n mlops $POD -- curl -s http://localhost:8080/train/health
# → {"status":"healthy","running":false}

# 3) 외부 경로 라우팅 (ALB 반영까지 수 분 소요)
curl -s -H "Host: api.mlops.click" http://api.mlops.click/train/health
curl -s -H "Host: api.mlops.click" http://api.mlops.click/             # 기존 추론 정상 동작 확인

# 4) 학습 1회 트리거 (선택)
curl -s -X POST -H "Host: api.mlops.click" http://api.mlops.click/train         # → 202
curl -s      -H "Host: api.mlops.click" http://api.mlops.click/train/status     # → success 확인
```

- [ ] ECR 리포지토리 `invest-train` 생성
- [ ] IAM(Athena/Glue/S3) 권한 확인 — 추론과 동일 노드롤/IRSA
- [ ] `mlflow-auth` Secret 존재 (mlops)
- [ ] 이미지 빌드·푸시 성공
- [ ] Deployment/Service/Ingress/CronJob 적용
- [ ] Pod 1/1 Running, `/train/health` 200
- [ ] 외부 `api.mlops.click/train` 200 & 기존 추론 `/` 정상
- [ ] CronJob(UTC 19:30 = KST 04:30) 등록 확인

---

## 5. 자동 학습 스케줄 (CronJob)

`k8s/cronjob.yaml` 이 매일 **UTC 19:30 (= KST 04:30)** 에 내부 서비스로 `POST /train` 을 호출합니다.

- cron 은 UTC 기준입니다. 사내 정책상 학습을 09시 직전에 끝내려면 데이터 적재 완료 시각을 고려해 04~07시(KST) 사이로 조정하세요. (`schedule` 값 변경)
- 스케줄 변경 시: `k8s/cronjob.yaml` 의 `schedule` 수정 후 `kubectl apply`.

---

## 6. 동봉 파일 목록

| 파일 | 용도 |
|------|------|
| `Dockerfile` | 이미지 빌드 |
| `requirements.txt` | 컨테이너 패키지 |
| `train_server.py`, `invest_train.py` | 애플리케이션 소스 |
| `deploy.sh` | 빌드/배포 스크립트 |
| `k8s/deployment.yaml` | 워크로드 (replicas 1, mem 6Gi) |
| `k8s/service.yaml` | ClusterIP 8080 |
| `k8s/ingress.yaml` | 공유 ALB, `/train` (order 5) |
| `k8s/cronjob.yaml` | 새벽 자동 학습 트리거 |
| `k8s/secret-template.yaml` | mlflow-auth 생성 참고 |
| `k8s/namespace.yaml` | mlops 네임스페이스 |
| `DEPLOY.md` | 상세 배포 가이드 |

---

## 7. 문의

배포 중 이슈는 MLOps 개발팀에 회신 부탁드립니다. 트러블슈팅 요약은 `DEPLOY.md §5` 참고.
