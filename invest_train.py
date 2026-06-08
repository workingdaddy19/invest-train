# ============================================================
# INVEST 부동산담보대출 투자적격 심사 - 모델 학습 스크립트
# - 데이터 로드(Athena) → 전처리 → 2개 모델 학습 → MLflow 등록
# - inference.py 와 전처리/피처 정의 100% 일치 (동일 이미지 공유)
# - 핵심: LabelEncoder + 학습셋 중앙값을 MLflow Artifact 로 저장
#         (inference.py 의 ModelStore._load_encoders 가 로드)
#
# 실행 방식
#   1) CLI / K8s CronJob :  python invest_train.py
#   2) /train 엔드포인트 :  from invest_train import run_training; run_training(...)
# ============================================================

import os
import json
import time
import tempfile
import logging
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
import joblib
import pytz

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import (
    classification_report, roc_auc_score, roc_curve,
    mean_absolute_error, mean_squared_error, r2_score,
)
import xgboost as xgb

import mlflow
import mlflow.xgboost

warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("invest_train")

KST = pytz.timezone("Asia/Seoul")

# ── 환경 변수 (inference.py 와 동일 키 사용) ─────────────────
MLFLOW_URI      = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.mlflow.svc.cluster.local:80")
ATHENA_DB       = os.getenv("ATHENA_DB", "mlops")
ATHENA_TABLE    = os.getenv("ATHENA_TABLE", "altinv_crel_train")
S3_OUTPUT       = os.getenv("ATHENA_S3_OUTPUT", "s3://s3-an2-mlops/athena/")
S3_BUCKET       = os.getenv("S3_BUCKET", "s3-an2-mlops")
EXPERIMENT_NAME = os.getenv("MLFLOW_EXPERIMENT", "invest-crel-model")
MODEL_CLS_NAME  = os.getenv("MODEL_CLS_NAME", "invest-crel-classification")
MODEL_REG_NAME  = os.getenv("MODEL_REG_NAME", "invest-crel-regression")
MODEL_ALIAS     = os.getenv("MODEL_ALIAS", "champion")
# True 면 학습 후 신규 버전에 champion alias 자동 부여 (운영 정책에 맞게 사용)
AUTO_PROMOTE    = os.getenv("AUTO_PROMOTE_CHAMPION", "false").lower() == "true"

# ── 피처 정의 (inference.py 의 정의와 반드시 동일해야 함) ─────
NUMERIC_COLS = [
    "gpt_ivt_trc_pi_rk", "gpt_ivt_dlb_rqt_amt", "ln_pd",
    "ltv_rte", "gpt_ivt_cpt_ern_rte", "gpt_ivt_cpt_ern_pd",
    "gpt_ivt_all_pcm_amt", "gpt_ivt_bdg_scl_txt", "gpt_ivt_nwk_ot_scl_txt",
    "gpt_ivt_cmpi_yr", "dbt_rpy_coef_rte", "gpt_ivt_rmd_lsg_ycn",
    "gpt_ivt_etrm_rte", "gpt_ivt_mkt_avg_etrm_rt",
    "gpt_ivt_ppo_re_amt", "gpt_ivt_mkt_ppo_re_amt",
    "gpt_ivt_mkt_avg_cpt_rte", "gpt_ivt_mkt_avg_dln_amt",
    "gpt_ivt_ln_pfat_txt", "gpt_ivt_te_ppo_amt",
    "gpt_ivt_cpt_reim", "gpt_ivt_appr_evl_ppo_amt",
    "gpt_ivt_rpy_rte", "bs_itt",
]
CAT_COLS = [
    "gpt_ivt_mth_cd", "gpt_ivt_ser_dv_cd", "gpt_ivt_tp_cd",
    "gpt_ivt_str_dv_cd", "gpt_ivt_kd_cd", "gpt_ivt_ara_dv_cd",
    "gpt_ivt_crd_rinf_txt", "gpt_ivt_ecfr_gd_txt",
]
FEATURE_COLS = NUMERIC_COLS + CAT_COLS

TARGET_CLS = "ivt_jg_cm_cd"   # 투자적격판단 (분류)
TARGET_REG = "ln_itt"          # 적정금리 (회귀)

CLS_PARAMS = {
    "n_estimators": 200, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "eval_metric": "logloss", "random_state": 42,
}
REG_PARAMS = {
    "n_estimators": 200, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8, "random_state": 42,
}


# ============================================================
# 1. 데이터 로드
# ============================================================
def load_data() -> pd.DataFrame:
    """Athena 학습 테이블 로드. SageMaker / Redshift 미사용."""
    import awswrangler as wr

    query = f"SELECT * FROM {ATHENA_DB}.{ATHENA_TABLE}"
    logger.info(f"Athena 로드: {ATHENA_DB}.{ATHENA_TABLE}")
    df = wr.athena.read_sql_query(sql=query, database=ATHENA_DB, s3_output=S3_OUTPUT)
    logger.info(f"학습 데이터 shape: {df.shape}")
    return df


# ============================================================
# 2. 전처리 (inference.preprocess 와 동일 로직)
#    - 학습 시점에 le_dict / le_target / 중앙값을 산출하여 아티팩트로 저장
# ============================================================
def preprocess(df_raw: pd.DataFrame):
    df = df_raw.copy()

    # 숫자형 변환
    for col in NUMERIC_COLS + [TARGET_REG]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 학습셋 중앙값 산출 → 결측 대체 + 추론용 아티팩트로 저장
    numeric_medians = df[NUMERIC_COLS].median().to_dict()
    df[NUMERIC_COLS] = df[NUMERIC_COLS].fillna(numeric_medians)

    # 범주형 Label Encoding
    le_dict = {}
    for col in CAT_COLS:
        df[col] = df[col].fillna("UNKNOWN").astype(str)
        le = LabelEncoder()
        df[col] = le.fit_transform(df[col])
        le_dict[col] = le

    # 분류 타깃
    df[TARGET_CLS] = df[TARGET_CLS].fillna("N").astype(str)
    le_target = LabelEncoder()
    df[TARGET_CLS] = le_target.fit_transform(df[TARGET_CLS])
    logger.info(f"Target classes: {list(le_target.classes_)}")

    # 회귀 타깃 결측 제거
    df = df.dropna(subset=[TARGET_REG])

    logger.info(f"전처리 완료 - features: {len(FEATURE_COLS)}, rows: {len(df)}")
    return df, le_dict, le_target, numeric_medians


# ============================================================
# 3. 인코더/중앙값 아티팩트 저장 (★ 추론 일관성 핵심)
#    inference.py ModelStore._load_encoders() 가 기대하는 구조:
#      encoders/le_dict.pkl, encoders/le_target.pkl
#    + numeric_medians.json (결측 대체 고도화용)
# ============================================================
def _log_encoders(run_id, le_dict, le_target, numeric_medians):
    with tempfile.TemporaryDirectory() as tmp:
        enc_dir = os.path.join(tmp, "encoders")
        os.makedirs(enc_dir, exist_ok=True)
        joblib.dump(le_dict, os.path.join(enc_dir, "le_dict.pkl"))
        joblib.dump(le_target, os.path.join(enc_dir, "le_target.pkl"))
        with open(os.path.join(enc_dir, "numeric_medians.json"), "w", encoding="utf-8") as f:
            json.dump(numeric_medians, f, ensure_ascii=False, indent=2)
        with mlflow.start_run(run_id=run_id):
            mlflow.log_artifacts(enc_dir, artifact_path="encoders")
    logger.info(f"인코더 아티팩트 저장 완료 (run_id={run_id[:8]})")


# ============================================================
# 4. Model 1 - 투자적격판단 (XGBoost Classification)
# ============================================================
def train_classifier(df, le_dict, le_target, numeric_medians):
    X = df[FEATURE_COLS]
    y = df[TARGET_CLS]

    class_counts = y.value_counts()
    n = len(df)
    min_class = class_counts.min()
    use_split = (n >= 20) and (min_class >= 2)

    if use_split:
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )
        eval_set = [(X_te, y_te)] if set(y_tr.unique()) == set(y_te.unique()) else None
    else:
        logger.warning(f"데이터 {n}건 → 전체 데이터 학습")
        X_tr, y_tr = X, y
        X_te, y_te = X, y
        eval_set = None

    n_classes = len(np.unique(y))

    with mlflow.start_run(run_name="crel-invest-classification") as run:
        clf = xgb.XGBClassifier(**CLS_PARAMS)
        clf.fit(X_tr, y_tr, eval_set=eval_set, verbose=False)

        y_pred = clf.predict(X_te)
        y_prob = clf.predict_proba(X_te)
        acc = float((y_pred == y_te).mean())
        try:
            auc = (roc_auc_score(y_te, y_prob[:, 1]) if n_classes == 2
                   else roc_auc_score(y_te, y_prob, multi_class="ovr", average="macro"))
        except ValueError as e:
            logger.warning(f"ROC-AUC 계산 불가: {e}")
            auc = 0.0

        mlflow.log_params(CLS_PARAMS)
        mlflow.log_param("model_type", "XGBClassifier")
        mlflow.log_param("target", TARGET_CLS)
        mlflow.log_param("feature_count", len(FEATURE_COLS))
        mlflow.log_metric("accuracy", acc)
        mlflow.log_metric("roc_auc", auc)
        mlflow.log_metric("train_rows", len(X_tr))
        mlflow.log_metric("test_rows", len(X_te))

        # log_model 대신 save_model → log_artifacts (MLflow 서버 중복키 버그 회피)
        with tempfile.TemporaryDirectory() as tmp:
            mlflow.xgboost.save_model(clf, os.path.join(tmp, "model"))
            mlflow.log_artifacts(os.path.join(tmp, "model"), artifact_path="model")

        run_id = run.info.run_id

    # 인코더/중앙값을 분류 run 에 저장 (inference 는 분류 모델 run 에서 로드)
    _log_encoders(run_id, le_dict, le_target, numeric_medians)

    mv = mlflow.register_model(f"runs:/{run_id}/model", MODEL_CLS_NAME)
    if AUTO_PROMOTE:
        mlflow.tracking.MlflowClient().set_registered_model_alias(
            MODEL_CLS_NAME, MODEL_ALIAS, mv.version
        )
        logger.info(f"{MODEL_CLS_NAME} v{mv.version} → @{MODEL_ALIAS}")

    logger.info(f"[분류] acc={acc:.4f} auc={auc:.4f} run={run_id[:8]} ver={mv.version}")
    logger.info("\n" + classification_report(y_te, y_pred, target_names=le_target.classes_))

    return {
        "run_id": run_id, "version": mv.version, "model": clf,
        "accuracy": acc, "roc_auc": auc,
        "y_test": y_te, "y_prob": y_prob, "n_classes": n_classes,
        "le_target": le_target,
    }


# ============================================================
# 5. Model 2 - 적정금리평가 (XGBoost Regression)
# ============================================================
def train_regressor(df):
    X = df[FEATURE_COLS]
    y = df[TARGET_REG]

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42)

    with mlflow.start_run(run_name="crel-invest-regression") as run:
        reg = xgb.XGBRegressor(**REG_PARAMS)
        reg.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

        y_pred = reg.predict(X_te)
        mae = float(mean_absolute_error(y_te, y_pred))
        rmse = float(mean_squared_error(y_te, y_pred) ** 0.5)
        r2 = float(r2_score(y_te, y_pred))

        mlflow.log_params(REG_PARAMS)
        mlflow.log_param("model_type", "XGBRegressor")
        mlflow.log_param("target", TARGET_REG)
        mlflow.log_param("feature_count", len(FEATURE_COLS))
        mlflow.log_metric("mae", mae)
        mlflow.log_metric("rmse", rmse)
        mlflow.log_metric("r2", r2)
        mlflow.log_metric("train_rows", len(X_tr))
        mlflow.log_metric("test_rows", len(X_te))

        with tempfile.TemporaryDirectory() as tmp:
            mlflow.xgboost.save_model(reg, os.path.join(tmp, "model"))
            mlflow.log_artifacts(os.path.join(tmp, "model"), artifact_path="model")

        run_id = run.info.run_id

    mv = mlflow.register_model(f"runs:/{run_id}/model", MODEL_REG_NAME)
    if AUTO_PROMOTE:
        mlflow.tracking.MlflowClient().set_registered_model_alias(
            MODEL_REG_NAME, MODEL_ALIAS, mv.version
        )
        logger.info(f"{MODEL_REG_NAME} v{mv.version} → @{MODEL_ALIAS}")

    logger.info(f"[회귀] mae={mae:.4f} rmse={rmse:.4f} r2={r2:.4f} run={run_id[:8]} ver={mv.version}")

    return {
        "run_id": run_id, "version": mv.version, "model": reg,
        "mae": mae, "rmse": rmse, "r2": r2,
        "y_test": y_te, "y_pred": y_pred,
    }


# ============================================================
# 6. 결과 시각화 (4개 차트) → 분류 run 에 아티팩트 저장
# ============================================================
def log_charts(cls_res, reg_res, run_id):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from pathlib import Path
        import matplotlib.font_manager as fm

        for fp in ["./NanumGothic.ttf", "/usr/share/fonts/nanum/NanumGothic.ttf"]:
            if Path(fp).exists():
                fm.fontManager.addfont(fp)
                plt.rcParams["font.family"] = fm.FontProperties(fname=fp).get_name()
                break
        plt.rcParams["axes.unicode_minus"] = False

        clf = cls_res["model"]; reg = reg_res["model"]
        le_target = cls_res["le_target"]
        y_te_c = cls_res["y_test"]; y_prob_c = cls_res["y_prob"]; n_classes = cls_res["n_classes"]
        y_te_r = reg_res["y_test"]; y_pred_r = reg_res["y_pred"]
        auc = cls_res["roc_auc"]; r2 = reg_res["r2"]; rmse = reg_res["rmse"]

        fig, axes = plt.subplots(2, 2, figsize=(16, 12))
        fig.suptitle("INVEST 부동산담보대출 심사모델 분석 결과", fontsize=16, fontweight="bold")

        # Chart 1: 투자적격판단 ROC
        ax1 = axes[0, 0]
        if n_classes == 2:
            fpr, tpr, _ = roc_curve(y_te_c, y_prob_c[:, 1])
            ax1.plot(fpr, tpr, color="steelblue", lw=2, label=f"ROC (AUC={auc:.3f})")
            ax1.plot([0, 1], [0, 1], "k--", lw=1)
            ax1.set_xlabel("False Positive Rate"); ax1.set_ylabel("True Positive Rate")
            ax1.legend(loc="lower right")
        ax1.set_title(f"[Model 1] 투자적격판단 - ROC Curve (AUC={auc:.3f})")
        ax1.grid(alpha=0.3)

        # Chart 2: 적정금리 실제 vs 예측
        ax2 = axes[0, 1]
        ax2.scatter(y_te_r, y_pred_r, alpha=0.5, color="coral", edgecolors="none", s=30)
        lims = [min(y_te_r.min(), y_pred_r.min()), max(y_te_r.max(), y_pred_r.max())]
        ax2.plot(lims, lims, "k--", lw=1)
        ax2.set_xlabel("실제 금리 (ln_itt)"); ax2.set_ylabel("예측 금리")
        ax2.set_title(f"[Model 2] 적정금리평가 - 실제 vs 예측\nR²={r2:.3f}, RMSE={rmse:.3f}")
        ax2.grid(alpha=0.3)

        # Chart 3: Model 1 변수 중요도
        ax3 = axes[1, 0]
        pd.Series(clf.feature_importances_, index=FEATURE_COLS).nlargest(15).sort_values().plot(
            kind="barh", ax=ax3, color="steelblue", alpha=0.8)
        ax3.set_title("[Model 1] 투자적격판단 - 변수 중요도 Top 15")
        ax3.set_xlabel("Feature Importance"); ax3.grid(axis="x", alpha=0.3)

        # Chart 4: Model 2 변수 중요도
        ax4 = axes[1, 1]
        pd.Series(reg.feature_importances_, index=FEATURE_COLS).nlargest(15).sort_values().plot(
            kind="barh", ax=ax4, color="coral", alpha=0.8)
        ax4.set_title("[Model 2] 적정금리평가 - 변수 중요도 Top 15")
        ax4.set_xlabel("Feature Importance"); ax4.grid(axis="x", alpha=0.3)

        plt.tight_layout()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "invest_crel_results.png")
            plt.savefig(out, dpi=150, bbox_inches="tight")
            with mlflow.start_run(run_id=run_id):
                mlflow.log_artifact(out)
        plt.close(fig)
        logger.info("차트 4종 저장 완료")
    except Exception as e:
        logger.warning(f"차트 생성 건너뜀: {e}")


# ============================================================
# 7. 학습 엔트리포인트
# ============================================================
def run_training(df_input: pd.DataFrame = None) -> dict:
    """
    전체 학습 파이프라인 실행.
    df_input 미지정 시 Athena 에서 로드.
    반환: 두 모델의 run_id / version / 핵심 지표 요약
    """
    start = time.time()
    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment(EXPERIMENT_NAME)

    df_raw = df_input if df_input is not None else load_data()
    df, le_dict, le_target, medians = preprocess(df_raw)

    cls_res = train_classifier(df, le_dict, le_target, medians)
    reg_res = train_regressor(df)
    log_charts(cls_res, reg_res, cls_res["run_id"])

    elapsed = round(time.time() - start, 2)
    summary = {
        "status": "success",
        "trained_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": elapsed,
        "rows": int(len(df)),
        "classification": {
            "model": MODEL_CLS_NAME, "version": cls_res["version"],
            "run_id": cls_res["run_id"],
            "accuracy": round(cls_res["accuracy"], 4), "roc_auc": round(cls_res["roc_auc"], 4),
        },
        "regression": {
            "model": MODEL_REG_NAME, "version": reg_res["version"],
            "run_id": reg_res["run_id"],
            "mae": round(reg_res["mae"], 4), "rmse": round(reg_res["rmse"], 4),
            "r2": round(reg_res["r2"], 4),
        },
        "auto_promoted": AUTO_PROMOTE,
    }
    logger.info(f"학습 완료 ({elapsed}s): {json.dumps(summary, ensure_ascii=False)}")
    return summary


if __name__ == "__main__":
    run_training()
