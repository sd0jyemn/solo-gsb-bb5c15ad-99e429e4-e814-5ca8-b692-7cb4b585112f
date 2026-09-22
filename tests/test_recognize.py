"""POST /v1/recognize 候选字识别测试。"""
from __future__ import annotations

import pytest

from app import recognition

# 参考字（画布坐标；识别按各自外接框归一化，尺寸/位置不影响）
REF_YI = [  # 一：一横
    [[100.0, 200.0], [300.0, 200.0]],
]
REF_ER = [  # 二：两横
    [[100.0, 150.0], [300.0, 150.0]],
    [[120.0, 260.0], [300.0, 260.0]],
]
REF_SHI = [  # 十：横 + 竖
    [[100.0, 200.0], [300.0, 200.0]],
    [[200.0, 100.0], [200.0, 300.0]],
]
REF_CHUAN = [  # 川：三竖
    [[100.0, 100.0], [100.0, 300.0]],
    [[200.0, 120.0], [200.0, 300.0]],
    [[300.0, 100.0], [300.0, 300.0]],
]

REFERENCES = {
    "一": REF_YI,
    "二": REF_ER,
    "十": REF_SHI,
    "川": REF_CHUAN,
}


def _register_all(client):
    for char, strokes in REFERENCES.items():
        r = client.put(
            f"/v1/references/{char}",
            json={"character": char, "strokes": strokes, "source": "test"},
        )
        assert r.status_code == 200


# ------------------------------------------------------------------ 基础

def test_no_reference_returns_empty(client):
    r = client.post(
        "/v1/recognize", json={"strokes": [REF_SHI[0]], "top_k": 5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == []
    assert body["reference_count"] == 0
    assert body["query_stroke_count"] == 1


def test_recognize_perfect_shi(client):
    _register_all(client)
    r = client.post(
        "/v1/recognize", json={"strokes": REF_SHI, "top_k": 3}
    )
    body = r.json()
    assert body["reference_count"] == 4
    assert len(body["candidates"]) == 3
    top = body["candidates"][0]
    assert top["character"] == "十"
    assert top["confidence"] == 1.0
    assert top["total_cost"] == 0.0
    assert top["reference_stroke_count"] == 2
    assert top["user_stroke_count"] == 2
    assert top["penalties"]["missing_stroke_count"] == 0
    assert top["penalties"]["extra_stroke_count"] == 0
    assert top["penalties"]["penalty_total"] == 0.0
    # 笔画对应：横配横(0->0)、竖配竖(1->1)
    pairs = {(m["user_index"], m["reference_index"]) for m in top["stroke_matches"]}
    assert pairs == {(0, 0), (1, 1)}
    for m in top["stroke_matches"]:
        assert m["cost"] == 0.0
        assert set(m["metrics"]) == {
            "start_deviation", "end_deviation",
            "direction_deviation_deg", "shape_deviation",
        }


def test_candidates_sorted_by_confidence_desc(client):
    _register_all(client)
    body = client.post(
        "/v1/recognize", json={"strokes": REF_SHI, "top_k": 4}
    ).json()
    confs = [c["confidence"] for c in body["candidates"]]
    assert confs == sorted(confs, reverse=True)
    assert [c["character"] for c in body["candidates"]][0] == "十"
    # top_k 截断
    assert len(body["candidates"]) == 4

    body3 = client.post(
        "/v1/recognize", json={"strokes": REF_SHI, "top_k": 2}
    ).json()
    assert len(body3["candidates"]) == 2


# ------------------------------------------------------------------ 次序无关

def test_wrong_stroke_order_still_recognized(client):
    _register_all(client)
    swapped = [REF_SHI[1], REF_SHI[0]]  # 先竖后横
    body = client.post(
        "/v1/recognize", json={"strokes": swapped, "top_k": 4}
    ).json()
    top = body["candidates"][0]
    assert top["character"] == "十"
    assert top["confidence"] == 1.0
    pairs = {(m["user_index"], m["reference_index"]) for m in top["stroke_matches"]}
    # 次序交换体现在对应关系上：用户 0 笔(竖) 配参考 1 笔(竖)
    assert pairs == {(0, 1), (1, 0)}


def test_shuffled_chuan_strokes(client):
    _register_all(client)
    shuffled = [REF_CHUAN[2], REF_CHUAN[0], REF_CHUAN[1]]
    body = client.post(
        "/v1/recognize", json={"strokes": shuffled, "top_k": 4}
    ).json()
    assert body["candidates"][0]["character"] == "川"
    assert body["candidates"][0]["confidence"] > 0.95


# ------------------------------------------------------------------ 缺笔/多笔

def test_missing_stroke_penalized(client):
    _register_all(client)
    # 只写了“十”的一横：候选“一”应满分，“十”带缺笔惩罚排其后
    body = client.post(
        "/v1/recognize", json={"strokes": REF_SHI[:1], "top_k": 4}
    ).json()
    by_char = {c["character"]: c for c in body["candidates"]}
    assert body["candidates"][0]["character"] == "一"
    assert by_char["一"]["confidence"] == 1.0

    shi = by_char["十"]
    pen = shi["penalties"]
    assert pen["missing_stroke_count"] == 1
    assert pen["missing_stroke_indices"] == [1]
    assert pen["extra_stroke_count"] == 0
    assert pen["missing_penalty_total"] == pen["missing_penalty_per_stroke"]
    assert shi["total_cost"] == pytest.approx(pen["missing_penalty_total"], abs=1e-3)
    # 总代价 = 2 个笔画槽位的平均，置信度 = 1 - cost/2
    assert shi["confidence"] == pytest.approx(
        1.0 - pen["missing_penalty_total"] / 2, abs=1e-3
    )
    # 配成功的笔画（横）本身代价为 0
    assert len(shi["stroke_matches"]) == 1
    assert shi["stroke_matches"][0]["reference_index"] == 0


def test_extra_stroke_penalized(client):
    _register_all(client)
    # “一”之外多写一竖：完美匹配“十”；对“一”而言竖是多笔
    strokes = [REF_SHI[0], [[50.0, 80.0], [60.0, 320.0]]]
    body = client.post(
        "/v1/recognize", json={"strokes": strokes, "top_k": 4}
    ).json()
    by_char = {c["character"]: c for c in body["candidates"]}
    yi = by_char["一"]
    pen = yi["penalties"]
    assert pen["extra_stroke_count"] == 1
    assert pen["extra_stroke_indices"] == [1]
    assert pen["missing_stroke_count"] == 0
    assert pen["penalty_total"] > 0
    matched_cost = sum(m["cost"] for m in yi["stroke_matches"])
    assert yi["total_cost"] == pytest.approx(
        matched_cost + pen["penalty_total"], abs=1e-3
    )
    # 置信度按每笔画槽位平均代价换算（2 笔输入）
    assert yi["confidence"] == pytest.approx(
        1.0 - yi["total_cost"] / 2, abs=1e-3
    )


def test_both_missing_and_extra_reported(client):
    _register_all(client)
    # 用户 3 笔，“十”2 笔：必然 1 笔多笔；再以低惩罚的引擎级用例
    # 覆盖“缺笔+多笔同时出现”的全局分配（等笔数下优先全部实配，
    # 只有惩罚低于错配代价时才会选择虚配）。
    body = client.post(
        "/v1/recognize", json={"strokes": [*REF_ER, REF_CHUAN[0]], "top_k": 4}
    ).json()
    shi = next(c for c in body["candidates"] if c["character"] == "十")
    pen = shi["penalties"]
    assert pen["extra_stroke_count"] == 1
    assert len(shi["stroke_matches"]) == 2


def test_engine_reports_missing_and_extra_together():
    # 两横输入 vs “十”（一横一竖）：惩罚足够低时，优化器宁可让竖缺笔、
    # 让第二横多笔，也不愿把横错配成竖。
    user = [[tuple(p) for p in s] for s in REF_ER]
    refs = [{"character": "十", "strokes": REF_SHI}]
    results = recognition.recognize(
        user, refs, top_k=1, missing_penalty=0.1, extra_penalty=0.1
    )
    cand = results[0]
    assert cand.character == "十"
    assert list(cand.missing_indices) == [1]
    assert cand.extra_indices  # 有一笔多笔
    assert cand.missing_penalty_total == pytest.approx(0.1)
    assert cand.extra_penalty_total == pytest.approx(0.1)
    # 配上的只有一横
    assert len(cand.matches) == 1
    assert cand.matches[0].reference_index == 0


# ------------------------------------------------------------------ 稳定排序

def test_tie_break_by_character_is_stable(client):
    # 只注册参考字；两条对称结构（一/丨类）可能同分，同分按字符排序
    r1 = client.put("/v1/references/一", json={"character": "一", "strokes": REF_YI})
    assert r1.status_code == 200
    # 二含两横，用完全相同的两横注册另一个字造成同构同分
    same_two = [[[0.0, 0.0], [10.0, 0.0]], [[0.0, 5.0], [10.0, 5.0]]]
    for ch in ("二", "三"):
        client.put(f"/v1/references/{ch}", json={"character": ch, "strokes": same_two})
    body = client.post(
        "/v1/recognize", json={"strokes": same_two, "top_k": 3}
    ).json()
    chars = [c["character"] for c in body["candidates"]]
    # “二”“三”同构同分，必须按字符升序（码位 三 U+4E09 < 二 U+4E8C）
    assert chars.index("三") < chars.index("二")
    assert chars == ["三", "二", "一"]
    # 重复请求结果一致
    body2 = client.post(
        "/v1/recognize", json={"strokes": same_two, "top_k": 3}
    ).json()
    assert [c["character"] for c in body2["candidates"]] == chars


# ------------------------------------------------------------------ 请求校验

def test_invalid_top_k_422(client):
    _register_all(client)
    for bad in (0, -1):
        r = client.post(
            "/v1/recognize", json={"strokes": REF_SHI, "top_k": bad}
        )
        assert r.status_code == 422, bad


def test_top_k_larger_than_references(client):
    _register_all(client)
    r = client.post(
        "/v1/recognize", json={"strokes": REF_SHI, "top_k": 100}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["candidates"]) == 4  # 仅有 4 个已注册参考字
    assert body["top_k"] == 100


def test_empty_strokes_422(client):
    r = client.post("/v1/recognize", json={"strokes": [], "top_k": 3})
    assert r.status_code == 422


def test_missing_top_k_422(client):
    r = client.post("/v1/recognize", json={"strokes": REF_SHI})
    assert r.status_code == 422


def test_degenerate_stroke_422(client):
    r = client.post(
        "/v1/recognize",
        json={"strokes": [[[1.0, 1.0]]], "top_k": 3},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ 归一化无关性

def test_scale_and_translation_invariant(client):
    _register_all(client)
    # “十”放大 2 倍并平移，识别结果应与原轨迹一致
    scaled = [
        [[500.0 + 2 * p[0], 800.0 + 2 * p[1]] for p in s] for s in REF_SHI
    ]
    body = client.post(
        "/v1/recognize", json={"strokes": scaled, "top_k": 4}
    ).json()
    assert body["candidates"][0]["character"] == "十"
    assert body["candidates"][0]["confidence"] == 1.0
