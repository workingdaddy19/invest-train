# INVEST Train 배포 가이드

invest-train(학습 서비스)을 EKS `mlops` 네임스페이스에 배포한다.
invest-app(추론)과 **동일 네임스페이스·동일 ALB**를 공유하되 별도 컨테이너다.

| 항목 | 값 |
|------|-----|
| ECR | `891376975666.dkr.ecr.ap-northeast-2.amazonaws.com/invest-train` |
| Namespace | `mlops` |
| Deployment / Service | `invest-train` |
| 외부 경로 | `http://api.mlops.click/train` (공유 ALB, group `shared-alb`) |

---

## 0. 사전 준비

```bash
# (최초 1회) ECR 리포지토리 생성
aws ecr create-repository --repository-name invest-train --region ap-northeast-2

# mlflow-auth Secret — invest-app 이 이미 mlops 에 만들었다면 생략 (공유)
kubectl create secret generic mlflow-auth \
  --from-literal=username=<id> --from-literal=password=<pw> \
  -n mlops
```

> **IAM(Athena/S3) 권한**: 학습 Pod 는 `awswrangler`로 Athena를 조회하고 S3
> (`s3-an2-mlops`)에 결과를 쓴다. invest-app 추론 Pod 와 동일한 노드 IAM Role
> 또는 IRSA 에 Athena/Glue/S3 권한이 포함돼 있어야 한다.

---

## 1. 전체 배포 (빌드 + 푸시 + 배포)

```bash
bash deploy.sh
```

개별 단계:

```bash
bash deploy.sh --build    # Docker 빌드 + ECR 푸시
bash deploy.sh --deploy   # K8s 매니페스트만 적용
```

적용되는 매니페스트: `deployment.yaml`, `service.yaml`, `ingress.yaml`

---

## 2. 학습 실행 / 상태 확인

```bash
# Pod 내부에서 학습 트리거
bash deploy.sh --train

# 진행/결과 확인
bash deploy.sh --status

# 또는 외부(ALB)에서 직접
curl -s -X POST -H "Host: api.mlops.click" http://api.mlops.click/train
curl -s      -H "Host: api.mlops.click" http://api.mlops.click/train/status
```

새벽 자동 학습은 **사내 표준 스케줄러**가 `POST /train`(외부 `api.mlops.click/train`
또는 내부 `invest-train.mlops.svc.cluster.local:8080/train`)을 호출하는 방식으로 트리거한다.
(클러스터 내 K8s CronJob 미사용 — 사내 정책)

---

## 3. Ingress 경로 공유 동작 방식

invest-app 의 `invest-inference-ingress`와 invest-train 의 `invest-train-ingress`는
같은 `alb.ingress.kubernetes.io/group.name: shared-alb`로 **하나의 ALB에 병합**된다.

| Ingress | path | group.order | 우선순위 |
|---------|------|-------------|----------|
| invest-train-ingress | `/train` | 5 | 먼저 평가 (구체적) |
| invest-inference-ingress | `/` | 10 | 나중 평가 (catch-all) |

`group.order`가 작을수록 먼저 평가되므로 `/train`이 catch-all `/`보다 우선
매칭된다. ALB는 경로를 재작성하지 않으므로 백엔드는 `/train`, `/train/status`,
`/train/health`를 그대로 수신한다.

> 만약 invest-app 의 catch-all `/` order 가 5 이하로 바뀌면 `/train`이 가려질 수
> 있다. 두 Ingress 의 order 관계(`/train` < `/`)를 유지할 것.

---

## 4. 모델 승격(promotion)

- `AUTO_PROMOTE_CHAMPION=false`(기본): 새 버전 등록까지만. 지표 확인 후 사람이
  champion alias 수동 부여 → 추론(invest-app)이 `/model/reload`로 무재배포 교체.
- `AUTO_PROMOTE_CHAMPION=true`: 학습 직후 자동 champion 부여 (완전 자동 파이프라인).
  `k8s/deployment.yaml`의 env 값을 변경 후 재배포.

---

## 5. 트러블슈팅

| 증상 | 확인 |
|------|------|
| `/train` 404 | invest-train-ingress order 가 invest-app `/`(10)보다 낮은지 |
| 학습 항상 409 | 워커/레플리카가 1 초과인지 (`replicas:1`, `--workers 1`) |
| Athena 권한 오류 | Pod IAM(IRSA/노드롤)에 Athena/Glue/S3 권한 |
| MLflow 401 | `mlflow-auth` Secret 이 mlops 에 존재하는지 |
| 학습 중 OOM | `resources.limits.memory`(6Gi) 상향 검토 |
