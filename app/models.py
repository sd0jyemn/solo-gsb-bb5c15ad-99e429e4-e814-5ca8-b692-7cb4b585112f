"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 平面坐标点 [x, y]，坐标系以画布左上角为原点、y 轴向下
Point = Annotated[list[float], Field(min_length=2, max_length=2)]
# 单笔画：至少 2 个采样点的折线
Stroke = Annotated[list[Point], Field(min_length=2)]


class Tolerances(BaseModel):
    """匹配容差。所有字段缺省时回落到服务端默认值（见 GET /v1/config）。

    距离类阈值基于归一化坐标（参考字外接正方形映射到 0..1）。
    """

    start_end: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="起止点允许偏差（归一化距离）",
    )
    direction_deg: Optional[float] = Field(
        default=None, ge=0.0, le=180.0,
        description="笔画方向允许偏差（度）",
    )
    shape: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="形状平均偏差上限",
    )
    fork_extra_direction_deg: Optional[float] = Field(
        default=None, ge=0.0, le=180.0,
        description="分叉/转折笔画在拐角分段上额外放宽的方向角（度）",
    )
    fork_extra_shape: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="分叉/转折笔画额外放宽的形状偏差",
    )
    corner_angle_deg: Optional[float] = Field(
        default=None, ge=10.0, le=170.0,
        description="判定分叉拐点的最小转角（度）",
    )


class ValidateRequest(BaseModel):
    """笔顺校验请求。同一 submission_id 重复提交返回首个结果（幂等）。"""

    submission_id: str = Field(
        min_length=1, max_length=128,
        description="客户端生成的幂等键；相同键+相同负载返回首个结果",
    )
    character: str = Field(min_length=1, max_length=1, description="目标汉字")
    reference_strokes: Optional[list[Stroke]] = Field(
        default=None, description="内联参考笔画序列（与 reference_id 二选一）",
    )
    reference_id: Optional[str] = Field(
        default=None, description="已注册的参考字 ID（即字符本身）",
    )
    strokes: list[Stroke] = Field(description="用户书写轨迹，按书写顺序")
    canvas_size: Optional[Point] = Field(
        default=None,
        description="画布宽高 [w, h]；缺省时以参考字外接框归一化",
    )
    tolerances: Tolerances = Field(default_factory=Tolerances)

    @model_validator(mode="after")
    def _check_reference(self) -> "ValidateRequest":
        if self.reference_strokes is None and self.reference_id is None:
            raise ValueError("必须提供 reference_strokes 或 reference_id 之一")
        if self.reference_strokes is not None and self.reference_id is not None:
            raise ValueError("reference_strokes 与 reference_id 只能提供一个")
        if self.reference_strokes is not None and not self.reference_strokes:
            raise ValueError("reference_strokes 不能为空列表")
        return self


class RegisterReferenceRequest(BaseModel):
    """注册/更新某汉字的参考笔画序列。"""

    character: str = Field(min_length=1, max_length=1)
    strokes: list[Stroke] = Field(min_length=1)
    source: Optional[str] = Field(default=None, description="来源标注，如字体/字帖")


# ------------------------------------------------------------------ 识别

class RecognizeRequest(BaseModel):
    """候选字识别请求：输入未标注字符的笔画轨迹。"""

    strokes: list[Stroke] = Field(
        min_length=1, description="用户书写轨迹（可按任意笔画次序书写）"
    )
    top_k: int = Field(
        ge=1, description="返回候选字的最大数量（按置信度降序）"
    )


class RecognitionStrokeMetrics(BaseModel):
    """识别中一对笔画的归一化偏差明细。"""

    start_deviation: float
    end_deviation: float
    direction_deviation_deg: float
    shape_deviation: float


class StrokeMatch(BaseModel):
    """识别出的单对笔画对应关系（全局一对一最优分配的结果）。"""

    user_index: int = Field(description="用户笔画下标（0 起，按书写顺序）")
    reference_index: int = Field(description="对应参考笔画下标（0 起）")
    cost: float = Field(description="该笔匹配代价，0..1，越小越相似")
    metrics: RecognitionStrokeMetrics


class PenaltyDetail(BaseModel):
    """缺笔/多笔的显式惩罚明细。"""

    missing_stroke_count: int = Field(description="未书写的参考笔画数")
    extra_stroke_count: int = Field(description="参考字中不存在的多余用户笔画数")
    missing_stroke_indices: list[int] = Field(
        default_factory=list, description="缺笔对应的参考笔画下标（0 起）"
    )
    extra_stroke_indices: list[int] = Field(
        default_factory=list, description="多笔对应的用户笔画下标（0 起）"
    )
    missing_penalty_per_stroke: float = Field(description="每笔缺笔的惩罚代价")
    extra_penalty_per_stroke: float = Field(description="每笔多笔的惩罚代价")
    missing_penalty_total: float = Field(description="缺笔惩罚合计")
    extra_penalty_total: float = Field(description="多笔惩罚合计")
    penalty_total: float = Field(description="缺笔+多笔惩罚总计")


class RecognitionCandidate(BaseModel):
    """单个候选字。"""

    character: str
    confidence: float = Field(ge=0.0, le=1.0, description="置信度，越大越匹配")
    total_cost: float = Field(description="全局最优分配的总代价（含缺/多笔惩罚）")
    reference_stroke_count: int
    user_stroke_count: int
    stroke_matches: list[StrokeMatch] = Field(
        description="配成功的笔画对（按用户笔画下标升序）"
    )
    penalties: PenaltyDetail


class RecognizeResponse(BaseModel):
    query_stroke_count: int
    reference_count: int = Field(description="参与比对的已注册参考字数量")
    top_k: int
    candidates: list[RecognitionCandidate] = Field(
        description="候选字列表，按置信度降序；同分按字符稳定排序"
    )


class StrokeMetrics(BaseModel):
    start_deviation: float
    end_deviation: float
    direction_deviation_deg: float
    shape_deviation: float
    is_forked: bool = Field(description="参考笔画是否含分叉/转折拐点")


class StrokeReport(BaseModel):
    index: int
    passed: bool
    score: float = Field(ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)
    metrics: Optional[StrokeMetrics] = None


DeviationCode = Literal[
    "STROKE_COUNT_MISMATCH",
    "WRONG_ORDER",
    "START_POINT_DEVIATION",
    "END_POINT_DEVIATION",
    "DIRECTION_DEVIATION",
    "SHAPE_DEVIATION",
]


class Deviation(BaseModel):
    code: DeviationCode
    stroke_index: Optional[int] = None
    message: str
    metrics: Optional[StrokeMetrics] = None


class ValidateResponse(BaseModel):
    submission_id: str
    character: str
    passed: bool
    score: float = Field(ge=0.0, le=100.0, description="整体得分 0-100")
    expected_stroke_count: int
    actual_stroke_count: int
    first_failed_stroke: Optional[int] = Field(
        default=None, description="首个不合格笔画的下标（0 起），全部合格为 null"
    )
    deviation: Optional[Deviation] = Field(
        default=None, description="首个不合格笔画的偏差原因"
    )
    strokes: list[StrokeReport]
    idempotent_replay: bool = Field(
        description="true 表示本次为幂等重放，返回的是首个提交的结果"
    )
    created_at: str


class ReferenceResponse(BaseModel):
    character: str
    stroke_count: int
    strokes: list[Stroke]
    source: Optional[str] = None
    updated_at: str


class SubmissionRecord(BaseModel):
    submission_id: str
    request_hash: str
    result: ValidateResponse
    created_at: str
