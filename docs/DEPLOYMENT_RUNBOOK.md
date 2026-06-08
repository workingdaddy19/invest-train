# invest-train 배포 방법 정리 (Deployment Runbook)

**한 장 요약 런북** — 단일 진실원본(Single Source of Truth). 상세는 `DEPLOY.md`·`INFRA_HANDOFF.md` 참고.

| 항목 | 값 |
|------|-----|
| ECR | `891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train` |
| 리전 / NS | `ap-northeast-2` / `mlops` |
| 워크로드 | Deployment·Service `invest-train` (ClusterIP 8080, **replicas 1**) |
| 외부 진입 | `http://api.mlops.click/train` (공유 ALB `shared-alb`, order 5) |

---

## 0. 배포 흐름 한눈에

```
[선행: 인프라팀]                   [배포]                         [검증]
ECR 생성 ─┐                  ┌─ docker build/push ─┐         ┌─ Pod 1/1 Running
IAM 확인 ─┼─▶ 준비완료 ──▶  ┤  kubectl apply k8s/  ├──▶     ┤─ /train/health 200
Secret ──┤                  └─ rollout status ────┘         ├─ api.mlops.click/train 200
NS 확인 ─┘                                                  └─ 기존 추론 / 정상
```

---

## 1. 선행 조건 (인프라팀, 최초 1회)

```bash
# ① ECR 리포지토리
aws ecr create-repository --repository-name invest-train --region ap-northeast-2

# ② IAM(Athena/Glue/S3) — invest-app 추론과 동일 노드롤/IRSA 에 포함되어 있는지 확인
#    athena:StartQueryExecution/GetQueryExecution/GetQueryResults, glue:GetTable/GetDatabase/GetPartitions,
#    s3:GetObject/PutObject/ListBucket (s3-an2-mlops, s3://s3-an2-mlops/athena/)

# ③ MLflow Secret (invest-app 이 이미 mlops 에 생성했으면 생략)
kubectl create secret generic mlflow-auth \
  --from-literal=username=<id> --from-literal=password=<pw> -n mlops

# ④ Namespace (없을 때만)
kubectl create namespace mlops
```

체크: `aws ecr describe-repositories --repository-names invest-train` / `kubectl get secret mlflow-auth -n mlops`

---

## 2. 배포 (택1)

### 방법 A — 스크립트 (권장)
```bash
cd invest-train
bash deploy.sh              # 빌드 + ECR 푸시 + K8s 배포 일괄
# 부분 실행: bash deploy.sh --build  |  bash deploy.sh --deploy
```

### 방법 B — 수동
```bash
# (1) 이미지
aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train
docker build -t invest-train:latest .
docker tag  invest-train:latest 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train:latest
docker push 891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train:latest

# (2) 매니페스트 (순서 무관, 한 번에 적용 가능)
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/cronjob.yaml

# (3) 롤아웃
kubectl rollout status deployment/invest-train -n mlops --timeout=180s
```

> 특정 태그 배포: `IMAGE_TAG=v1.0.1 bash deploy.sh --build` 후 deployment.yaml 의 image 태그도 맞춰 변경.

---

## 3. ⚠️ 라우팅 핵심 — 기존 추론과 공유 ALB

| Ingress | path | order | 의미 |
|---------|------|:-----:|------|
| invest-train-ingress (신규) | `/train` | **5** | 먼저 평가 |
| invest-inference-ingress (기존) | `/` | 10 | catch-all |

- order **작을수록 우선**. `/train`(5)이 `/`(10)보다 먼저 매칭되어야 정상.
- **기존 invest-app Ingress(order 10)는 건드리지 말 것.** 5 이하로 낮추면 `/train` 가려져 404.
- ALB 경로 미재작성 → 백엔드가 `/train`, `/train/status`, `/train/health` 원형 수신.

---

## 4. 배포 검증

```bash
# Pod
kubectl get pods -n mlops -l app=invest-train                 # 1/1 Running

# 내부 헬스
POD=$(kubectl get pod -n mlops -l app=invest-train -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n mlops $POD -- curl -s http://localhost:8080/train/health
# → {"status":"healthy","running":false}

# 외부 라우팅 (ALB 반영 수 분 소요)
curl -s -H "Host: api.mlops.click" http://api.mlops.click/train/health   # train → 200
curl -s -H "Host: api.mlops.click" http://api.mlops.click/               # inference 정상
```

---

## 5. 학습 실행 / 운영

```bash
# 수동 트리거 (즉시 202)
curl -s -X POST -H "Host: api.mlops.click" http://api.mlops.click/train
# 또는: bash deploy.sh --train

# 진행/결과 폴링
curl -s -H "Host: api.mlops.click" http://api.mlops.click/train/status
# 또는: bash deploy.sh --status

# 자동: CronJob 매일 UTC 19:30 = KST 04:30 (k8s/cronjob.yaml, schedule 변경 가능)
```

학습 후: 지표 확인 → MLflow 에서 champion alias 수동 부여 → invest-app `/model/reload` 로 무재배포 반영.
(완전 자동화 시 `AUTO_PROMOTE_CHAMPION=true` 로 변경 후 재배포)

---

## 6. 롤백 / 트러블슈팅

```bash
# 롤백
kubectl rollout undo deployment/invest-train -n mlops
kubectl rollout history deployment/invest-train -n mlops

# 로그
kubectl logs -n mlops -l app=invest-train --tail=200 -f
```

| 증상 | 원인/확인 |
|------|-----------|
| `/train` 404 | invest-train order(5) < invest-app `/`(10) 인지 |
| 학습 항상 409 | replicas>1 또는 workers>1 (반드시 1) |
| Athena 권한 오류 | Pod IAM(IRSA/노드롤) Athena/Glue/S3 |
| MLflow 401 | `mlflow-auth` Secret(mlops) 존재 |
| 학습 중 OOM | `resources.limits.memory`(6Gi) 상향 검토 |

---

## 7. 배포 체크리스트

- [ ] ECR `invest-train` 생성
- [ ] IAM(Athena/Glue/S3) 확인 (추론과 동일 롤)
- [ ] `mlflow-auth` Secret (mlops)
- [ ] 이미지 빌드·푸시
- [ ] deployment/service/ingress/cronjob apply
- [ ] Pod 1/1 Running, `/train/health` 200
- [ ] 외부 `/train` 200 & 기존 `/` 정상
- [ ] 학습 1회 트리거 → status success → MLflow 신규 버전 + `encoders/` 아티팩트
- [ ] CronJob 등록 확인
