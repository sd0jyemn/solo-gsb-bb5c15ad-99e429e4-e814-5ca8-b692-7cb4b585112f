"""候选字识别引擎：代价矩阵 + 全局一对一最优分配。

与 validation.validate 按书写顺序逐笔比对不同，识别面对的是**未标注字符**：

- 用户轨迹与每条参考字分别按**各自外接框**归一化，做平移/缩放无关的形状比对；
- 用户笔画 i 与参考笔画 j 的代价由起点、终点、方向、形状四项归一化偏差
  加权得到（权重与校验引擎一致：20/20/30/30）；
- 代价矩阵用虚行/虚列补齐为方阵：参考笔画配到虚行 = **缺笔**，
  用户笔画配到虚列 = **多笔**，二者均产生显式惩罚代价；
- 在方阵上用匈牙利算法求最小权完美匹配（全局一对一最优分配），
  因而书写次序不同只会体现在笔画对应关系上，不影响匹配代价。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import geometry as geo
from .config import settings

# 单项偏差截断上限：归一化坐标落在 0..1，超出按 1 计。
_COST_CAP = 1.0
_W_START = 0.20
_W_END = 0.20
_W_DIRECTION = 0.30
_W_SHAPE = 0.30


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class StrokeMatch:
    """一对配成功的（用户笔画, 参考笔画）。"""

    user_index: int
    reference_index: int
    cost: float
    metrics: dict


@dataclass(frozen=True)
class CandidateResult:
    """单个候选字的识别结果。"""

    character: str
    total_cost: float
    confidence: float
    matches: tuple[StrokeMatch, ...]
    # 未配上的参考笔画下标（缺笔）/ 用户笔画下标（多笔）
    missing_indices: tuple[int, ...]
    extra_indices: tuple[int, ...]
    missing_penalty: float
    extra_penalty: float

    @property
    def missing_penalty_total(self) -> float:
        return len(self.missing_indices) * self.missing_penalty

    @property
    def extra_penalty_total(self) -> float:
        return len(self.extra_indices) * self.extra_penalty


# ---------------------------------------------------------------- 单笔代价

def _stroke_metrics(
    ref: geo.Polyline, usr: geo.Polyline, resample_n: int
) -> dict:
    """两条已归一化笔画的四项偏差（指标定义与校验引擎一致）。"""
    ref_rs = geo.resample(ref, resample_n)
    usr_rs = geo.resample(usr, resample_n)

    start_dev = geo._dist(usr_rs[0], ref_rs[0])
    end_dev = geo._dist(usr_rs[-1], ref_rs[-1])
    shape_dev = geo.mean_pointwise_deviation(ref_rs, usr_rs)

    ra = geo.displacement_angle(ref_rs)
    ua = geo.displacement_angle(usr_rs)
    if ra is None and ua is None:  # 两者都退化为点
        dir_dev = 0.0
    elif ra is None or ua is None:  # 一点对一笔，方向完全不可比
        dir_dev = 180.0
    else:
        dir_dev = geo.angle_diff(ra, ua)

    return {
        "start_deviation": round(start_dev, 4),
        "end_deviation": round(end_dev, 4),
        "direction_deviation_deg": round(dir_dev, 2),
        "shape_deviation": round(shape_dev, 4),
    }


def _stroke_cost(metrics: dict) -> float:
    """四项偏差的加权代价，取值 0..1。"""
    return (
        _W_START * min(metrics["start_deviation"], _COST_CAP)
        + _W_END * min(metrics["end_deviation"], _COST_CAP)
        + _W_DIRECTION * (metrics["direction_deviation_deg"] / 180.0)
        + _W_SHAPE * min(metrics["shape_deviation"], _COST_CAP)
    )


# ---------------------------------------------------------------- 匈牙利算法

def _optimal_assignment(cost: list[list[float]]) -> list[int]:
    """方阵最小权完美匹配（Kuhn-Munkres / 匈牙利算法，O(n³)）。

    返回 assignment，满足 assignment[row] = col。代价允许任意非负浮点数。
    笔画数通常 < 30，纯 Python 实现足够快，避免引入 scipy 依赖。
    """
    n = len(cost)
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)      # p[j]：当前与列 j 配对的行（1 起，0 为虚节点）
    way = [0] * (n + 1)    # way[j]：增广路径上 j 的前一列
    inf = float("inf")

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [0] * n
    for j in range(1, n + 1):
        assignment[p[j] - 1] = j - 1
    return assignment


# ---------------------------------------------------------------- 单候选匹配

def _match_one(
    character: str,
    user_norm: list[geo.Polyline],
    ref_norm: list[geo.Polyline],
    resample_n: int,
    missing_penalty: float,
    extra_penalty: float,
) -> CandidateResult:
    m = len(user_norm)
    nr = len(ref_norm)
    size = m + nr  # 行：m 个用户笔 + nr 个虚行；列：nr 个参考笔 + m 个虚列

    # 真实笔画两两代价与指标
    pair_metrics: list[list[dict]] = [
        [_stroke_metrics(ref_norm[j], user_norm[i], resample_n)
         for j in range(nr)]
        for i in range(m)
    ]
    pair_cost = [[_stroke_cost(pair_metrics[i][j]) for j in range(nr)]
                 for i in range(m)]

    # 方阵布局：
    #   用户笔行 × 参考笔列 = 实配代价
    #   用户笔行 × 虚列     = 多笔惩罚
    #   虚行     × 参考笔列 = 缺笔惩罚
    #   虚行     × 虚列     = 0
    square = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if i < m and j < nr:
                square[i][j] = pair_cost[i][j]
            elif i < m:  # 用户笔画落到虚列：多笔
                square[i][j] = extra_penalty
            elif j < nr:  # 参考笔画落到虚行：缺笔
                square[i][j] = missing_penalty

    assignment = _optimal_assignment(square)

    matches: list[StrokeMatch] = []
    missing: list[int] = []
    extra: list[int] = []
    total_cost = 0.0
    for i in range(size):
        j = assignment[i]
        if i < m and j < nr:
            total_cost += pair_cost[i][j]
            matches.append(
                StrokeMatch(i, j, round(pair_cost[i][j], 4), pair_metrics[i][j])
            )
        elif i < m:  # j >= nr：用户笔画无对应参考笔画
            extra.append(i)
            total_cost += extra_penalty
        elif j < nr:  # i >= m：参考笔画无人书写
            missing.append(j)
            total_cost += missing_penalty
        # 虚行-虚列：总分为 0，忽略

    matches.sort(key=lambda x: x.user_index)
    missing.sort()
    extra.sort()

    # 每个笔画槽位（含缺/多笔）的平均代价映射为 0..1 置信度
    denom = max(m, nr, 1)
    confidence = max(0.0, 1.0 - total_cost / denom)
    return CandidateResult(
        character=character,
        total_cost=total_cost,
        confidence=round(confidence, 4),
        matches=tuple(matches),
        missing_indices=tuple(missing),
        extra_indices=tuple(extra),
        missing_penalty=missing_penalty,
        extra_penalty=extra_penalty,
    )


# ---------------------------------------------------------------- 主入口

def recognize(
    user_strokes: list[list[tuple[float, float]]],
    references: list[dict],
    top_k: int,
    resample_n: int | None = None,
    missing_penalty: float | None = None,
    extra_penalty: float | None = None,
) -> list[CandidateResult]:
    """对全部已注册参考字打分，返回置信度最高的前 top_k 个候选。

    references 为 db.list_references() 返回的记录列表
    （含 character 与 strokes 字段）。
    """
    n = resample_n or settings.resample_points
    miss_p = (
        settings.recognition_missing_penalty
        if missing_penalty is None
        else missing_penalty
    )
    extra_p = (
        settings.recognition_extra_penalty
        if extra_penalty is None
        else extra_penalty
    )

    # 用户轨迹按自身外接框归一化（识别时字符未知，不能套用某条参考的变换）
    u_origin, u_scale = geo.make_normalization(user_strokes)
    user_norm = geo.apply_normalization(user_strokes, u_origin, u_scale)

    results: list[CandidateResult] = []
    for ref in references:
        ref_strokes = [[tuple(p) for p in s] for s in ref["strokes"]]
        r_origin, r_scale = geo.make_normalization(ref_strokes)
        ref_norm = geo.apply_normalization(ref_strokes, r_origin, r_scale)
        results.append(
            _match_one(
                ref["character"], user_norm, ref_norm, n, miss_p, extra_p
            )
        )

    # 总分升序（置信度降序）；同分时按字符排序，保证结果稳定
    results.sort(key=lambda c: (c.total_cost, c.character))
    return results[:top_k]
