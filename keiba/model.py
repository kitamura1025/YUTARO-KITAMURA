"""近走成績から能力スコアを作り、単勝オッズ（市場）と混ぜて各馬の勝率を出す。

スコアは「秒」単位のイメージ（大きいほど強い）。
パラメータは backtest.py で過去レースに当てはめて調整し、params.json に保存する。
"""
import json
import math
from pathlib import Path

PARAMS_FILE = Path(__file__).resolve().parent.parent / "params.json"

DEFAULT = {
    "recency": [1.0, 0.8, 0.65, 0.5, 0.4],  # 前走→5走前の重み
    "best_weight": 0.5,       # 近5走のベストパフォーマンスをどれだけ足すか
    "going_weight": 0.15,     # 道悪（重・不良）実績の重み（当日が重・不良の時だけ効く）
    "training_weight": 0.08,  # 調教評価 A:+ / C,D:-
    "age_weight": 0.05,       # 3-4歳:+ / 7歳以上:-
    "inner_weight": 0.03,     # 1-4枠:+（中山外1200などの内枠有利コース）
    "temperature": 0.35,      # スコア→確率の鋭さ（小さいほど差が開く）
    "market_blend": 0.5,      # 1.0=市場オッズだけ / 0.0=モデルだけ
}

# 着差にクラスの格を足して「G1で走っていたら何秒差か」に揃える
CLASS_GAP = {"G1": 0.0, "G2": 0.15, "G3": 0.3, "OP": 0.5, "L": 0.45, "3勝": 0.8, "2勝": 1.1, "1勝": 1.4}


def load_params():
    p = dict(DEFAULT)
    if PARAMS_FILE.exists():
        p.update(json.loads(PARAMS_FILE.read_text()))
    return p


def run_performance(run, target_distance, target_surface="芝"):
    """1走ぶんの評価値と、その走りの参考度（0〜1）。"""
    diff = run.diff
    if diff is None:  # 海外など着差なし → 着順から大まかに推定
        if run.rank is None:
            return None, 0.0
        diff = min(0.1 * (run.rank - 1), 1.5)
    elif run.rank is None:
        return None, 0.0
    diff = max(min(diff, 2.5), -0.4)  # 大敗・大差勝ちは頭打ち
    perf = -(diff + CLASS_GAP.get(run.grade, 1.6))
    rel = max(0.2, 1 - abs(run.distance - target_distance) / 800)
    if run.surface != target_surface:
        rel *= 0.35
    return perf, rel


def horse_score(entry, race, params, going=""):
    w = params["recency"]
    num = den = 0.0
    best = None
    heavy = []
    for i, run in enumerate(entry.past[:5]):
        perf, rel = run_performance(run, race["distance"], race["surface"])
        if perf is None:
            continue
        num += w[i] * rel * perf
        den += w[i] * rel
        if rel >= 0.7:
            best = perf if best is None else max(best, perf)
        if run.going in ("重", "不") and run.surface == race["surface"]:
            heavy.append(perf)
    if den == 0:
        base = -2.0  # データがない馬は低めに
    else:
        base = num / den + params["best_weight"] * ((best if best is not None else num / den) - num / den)
    extra = 0.0
    if going in ("重", "不") and heavy:
        extra += params["going_weight"] * (sum(heavy) / len(heavy) - num / den if den else 0) + params["going_weight"] * 0.5
    if entry.training in ("A",):
        extra += params["training_weight"]
    elif entry.training in ("C", "D"):
        extra -= params["training_weight"]
    if entry.age <= 4:
        extra += params["age_weight"]
    elif entry.age >= 7:
        extra -= params["age_weight"]
    if entry.waku <= 4:
        extra += params["inner_weight"]
    return base + extra


def softmax(scores, temperature):
    m = max(scores.values())
    ex = {k: math.exp((v - m) / temperature) for k, v in scores.items()}
    s = sum(ex.values())
    return {k: v / s for k, v in ex.items()}


def market_probs(entries):
    raw = {e.umaban: 1 / e.odds for e in entries if e.odds}
    s = sum(raw.values())
    return {k: v / s for k, v in raw.items()} if s else {}


def win_probs(entries, race, params=None, going=""):
    """{馬番: 勝率}。モデルと市場を対数空間で混ぜる。"""
    params = params or load_params()
    scores = {e.umaban: horse_score(e, race, params, going) for e in entries}
    pm = softmax(scores, params["temperature"])
    mk = market_probs(entries)
    a = params["market_blend"] if mk else 0.0
    mixed = {k: math.exp((1 - a) * math.log(pm[k]) + a * math.log(mk.get(k, 1e-4))) for k in pm}
    s = sum(mixed.values())
    return {k: v / s for k, v in mixed.items()}, scores
