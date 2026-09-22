"""FastAPI 应用入口与路由。"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status

from . import __version__, db as db_module, recognition
from .config import settings
from .models import (
    PenaltyDetail,
    RecognitionCandidate,
    RecognitionStrokeMetrics,
    RecognizeRequest,
    RecognizeResponse,
    ReferenceResponse,
    RegisterReferenceRequest,
    StrokeMatch,
    SubmissionRecord,
    Tolerances,
    ValidateRequest,
    ValidateResponse,
)
from .validation import Tolerances as EngineTolerances
from .validation import validate


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_module.init_db(settings.database_path)
    yield
    db_module.get_db().close()


app = FastAPI(
    title="汉字笔顺轨迹校验 API",
    description=(
        "接收汉字的参考笔画序列与用户书写轨迹，按笔画方向、起止点和书写顺序"
        "做归一化匹配，支持可配置的分叉/转折笔画容差，返回首个不合格笔画、"
        "偏差原因与整体得分。同一 submission_id 重复提交幂等。"
        "另提供 POST /v1/recognize：对未标注字符的轨迹，在全部已注册参考字上"
        "做全局一对一最优分配匹配（容忍书写次序差异，显式惩罚缺笔/多笔），"
        "返回按置信度排序的候选字。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_db() -> db_module.Database:
    return db_module.get_db()


# ------------------------------------------------------------------ 基础

@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/v1/config", tags=["meta"])
def get_config():
    """查看服务端默认容差（请求体 tolerances 可逐字段覆盖）。"""
    return {
        "start_end_tolerance": settings.start_end_tolerance,
        "direction_tolerance_deg": settings.direction_tolerance_deg,
        "shape_tolerance": settings.shape_tolerance,
        "fork_extra_direction_deg": settings.fork_extra_direction_deg,
        "fork_extra_shape": settings.fork_extra_shape,
        "corner_angle_deg": settings.corner_angle_deg,
        "resample_points": settings.resample_points,
        "recognition_missing_penalty": settings.recognition_missing_penalty,
        "recognition_extra_penalty": settings.recognition_extra_penalty,
    }


# ------------------------------------------------------------------ 参考字

@app.put(
    "/v1/references/{character}",
    response_model=ReferenceResponse,
    tags=["references"],
)
def register_reference(
    character: str,
    body: RegisterReferenceRequest,
    db: db_module.Database = Depends(get_db),
):
    """注册或更新某汉字的参考笔画序列（按字符幂等覆盖）。"""
    if character != body.character:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="路径字符与请求体 character 不一致",
        )
    record = db.upsert_reference(
        body.character,
        [[[float(p[0]), float(p[1])] for p in s] for s in body.strokes],
        body.source,
    )
    return record


@app.get(
    "/v1/references/{character}",
    response_model=ReferenceResponse,
    tags=["references"],
)
def get_reference(character: str, db: db_module.Database = Depends(get_db)):
    record = db.get_reference(character)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="参考字未注册")
    return record


# ------------------------------------------------------------------ 识别

@app.post(
    "/v1/recognize",
    response_model=RecognizeResponse,
    tags=["recognition"],
)
def recognize_strokes(
    req: RecognizeRequest, db: db_module.Database = Depends(get_db)
):
    """从未标注字符的笔画轨迹中识别候选字。

    在全部已注册参考字上构建用户/参考笔画代价矩阵，以全局一对一最优分配
    （匈牙利算法）容忍书写次序差异；缺笔/多笔作为显式惩罚计入总分。
    返回按置信度降序的前 top_k 个候选，同分按字符稳定排序。
    无任何已注册参考字时 candidates 为空列表。
    """
    references = db.list_references()

    results = recognition.recognize(
        user_strokes=[[tuple(p) for p in s] for s in req.strokes],
        references=references,
        top_k=req.top_k,
    )

    candidates = [
        RecognitionCandidate(
            character=cand.character,
            confidence=cand.confidence,
            total_cost=round(cand.total_cost, 4),
            reference_stroke_count=(
                len(cand.matches) + len(cand.missing_indices)
            ),
            user_stroke_count=len(cand.matches) + len(cand.extra_indices),
            stroke_matches=[
                StrokeMatch(
                    user_index=m.user_index,
                    reference_index=m.reference_index,
                    cost=m.cost,
                    metrics=RecognitionStrokeMetrics(**m.metrics),
                )
                for m in cand.matches
            ],
            penalties=PenaltyDetail(
                missing_stroke_count=len(cand.missing_indices),
                extra_stroke_count=len(cand.extra_indices),
                missing_stroke_indices=list(cand.missing_indices),
                extra_stroke_indices=list(cand.extra_indices),
                missing_penalty_per_stroke=cand.missing_penalty,
                extra_penalty_per_stroke=cand.extra_penalty,
                missing_penalty_total=round(cand.missing_penalty_total, 4),
                extra_penalty_total=round(cand.extra_penalty_total, 4),
                penalty_total=round(
                    cand.missing_penalty_total + cand.extra_penalty_total, 4
                ),
            ),
        )
        for cand in results
    ]

    return RecognizeResponse(
        query_stroke_count=len(req.strokes),
        reference_count=len(references),
        top_k=req.top_k,
        candidates=candidates,
    )


# ------------------------------------------------------------------ 校验

def _request_hash(req: ValidateRequest) -> str:
    """请求负载的规范化哈希（含全部影响结果的字段）。"""
    canonical = json.dumps(
        req.model_dump(mode="json", exclude={"submission_id"}),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.post(
    "/v1/validate",
    response_model=ValidateResponse,
    responses={409: {"description": "submission_id 已被不同负载占用"}},
    tags=["validation"],
)
def validate_submission(
    req: ValidateRequest, db: db_module.Database = Depends(get_db)
):
    """校验一次书写提交。

    幂等语义：相同 submission_id + 相同负载 → 返回首个结果并标记
    idempotent_replay=true；相同 submission_id + 不同负载 → 409。
    """
    req_hash = _request_hash(req)

    existing = db.get_submission(req.submission_id)
    if existing is not None:
        if existing["request_hash"] != req_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="submission_id 已被不同负载的提交占用",
            )
        replay = dict(existing["response"])
        replay["idempotent_replay"] = True
        return replay

    # 解析参考笔画：内联优先，其次按 reference_id 查库
    if req.reference_strokes is not None:
        reference = req.reference_strokes
    else:
        record = db.get_reference(req.reference_id)
        if record is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"参考字 '{req.reference_id}' 未注册",
            )
        reference = record["strokes"]

    tol = EngineTolerances.from_request(req.tolerances)
    canvas = tuple(req.canvas_size) if req.canvas_size else None
    result = validate(
        reference_strokes=[[tuple(p) for p in s] for s in reference],
        user_strokes=[[tuple(p) for p in s] for s in req.strokes],
        tol=tol,
        canvas_size=canvas,
    )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    response = ValidateResponse(
        submission_id=req.submission_id,
        character=req.character,
        passed=result.passed,
        score=result.score,
        expected_stroke_count=len(reference),
        actual_stroke_count=len(req.strokes),
        first_failed_stroke=result.first_failed_stroke,
        deviation=result.deviation,
        strokes=[
            {
                "index": v.index,
                "passed": v.passed,
                "score": v.score,
                "reasons": v.reasons,
                "metrics": v.metrics,
            }
            for v in result.strokes
        ],
        idempotent_replay=False,
        created_at=now,
    )

    saved, inserted = db.save_submission(
        req.submission_id, req_hash, response.model_dump(mode="json")
    )
    if not inserted:
        # 并发下同键提交：以首个落库结果为准
        if saved["request_hash"] != req_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="submission_id 已被不同负载的提交占用",
            )
        replay = dict(saved["response"])
        replay["idempotent_replay"] = True
        return replay
    return response


@app.get(
    "/v1/submissions/{submission_id}",
    response_model=SubmissionRecord,
    tags=["validation"],
)
def get_submission(submission_id: str, db: db_module.Database = Depends(get_db)):
    """按幂等键查询历史提交结果。"""
    record = db.get_submission(submission_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="提交不存在")
    return {
        "submission_id": record["submission_id"],
        "request_hash": record["request_hash"],
        "result": record["response"],
        "created_at": record["created_at"],
    }
