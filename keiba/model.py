"""特徴量から各馬の勝率を出すモデル（条件付きロジット / Plackett-Luce）。

勝率 ∝ exp(Σ 重み × 標準化した特徴量)。重みは backtest.py で過去レースの
1〜3着の並びが最も出やすくなるように学習し、params.json に保存する。
"""
import json
import math
from pathlib import Path

from .features import FEATURES

PARAMS_FILE = Path(__file__).resolve().parent.parent / "params.json"

# 向きが決まっている要素は、プラスの方向にしか効かせない（少ないデータで逆向きの重みが付くのを防ぐ）。
# 例: 騎手の成績が良いほど勝率が下がる、のような不自然な学習結果は採用しない。
POSITIVE_ONLY = {"speed_best", "speed_avg", "level", "course_fit", "going_fit", "jockey", "trainer",
                 "jockey_trainer", "jockey_course", "sire", "damsire", "track_fit", "experience", "market"}


def load_params():
    if PARAMS_FILE.exists():
        p = json.loads(PARAMS_FILE.read_text())
        if "coef" in p:
            return p
    # 未学習時は市場オッズだけ
    return {"mean": {}, "sd": {}, "coef": {"market": 1.0}}


def save_params(p):
    PARAMS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2))


def standardize(feats, params):
    z = {}
    for k in FEATURES:
        v = feats.get(k)
        sd = params["sd"].get(k) or 1.0
        z[k] = 0.0 if v is None else max(-4.0, min(4.0, (v - params["mean"].get(k, 0.0)) / sd))
    return z


def probs(race_feats, params):
    """race_feats: {馬番: 特徴量dict} → {馬番: 勝率}"""
    util = {k: sum(params["coef"].get(f, 0.0) * z for f, z in standardize(v, params).items())
            for k, v in race_feats.items()}
    m = max(util.values())
    ex = {k: math.exp(u - m) for k, u in util.items()}
    s = sum(ex.values())
    return {k: v / s for k, v in ex.items()}


def contributions(feats, params):
    """各特徴量が勝率を押し上げた/下げた量（説明用）"""
    return {f: params["coef"].get(f, 0.0) * z for f, z in standardize(feats, params).items()}


def fit(races, features=None, l2=0.02, iters=400, lr=0.3, top=3):
    """races: [{'feats': {馬番: dict}, 'order': [1着, 2着, 3着 ...]}]"""
    features = features or FEATURES
    vals = {f: [v[f] for r in races for v in r["feats"].values() if v.get(f) is not None] for f in features}
    mean = {f: sum(x) / len(x) if x else 0.0 for f, x in vals.items()}
    sd = {f: (math.sqrt(sum((a - mean[f]) ** 2 for a in x) / len(x)) or 1.0) if x else 1.0 for f, x in vals.items()}
    params = {"mean": mean, "sd": sd, "coef": {f: 0.0 for f in features}}
    # 速度のため、特徴量はリストにして持つ
    data = []
    for r in races:
        zs = {}
        for k, v in r["feats"].items():
            z = standardize(v, params)
            zs[k] = [z[f] for f in features]
        order = [h for h in r["order"][:top] if h in zs]
        if order:
            data.append((list(zs.values()), [list(zs).index(h) for h in order]))
    nf = len(features)
    w = [1.0 if f == "market" else 0.0 for f in features]
    for _ in range(iters):
        grad = [-l2 * w[j] * len(data) for j in range(nf)]
        for xs, order in data:
            util = [sum(wj * xj for wj, xj in zip(w, x)) for x in xs]
            alive = list(range(len(xs)))
            for h in order:
                m = max(util[i] for i in alive)
                ex = {i: math.exp(util[i] - m) for i in alive}
                s = sum(ex.values())
                for j in range(nf):
                    grad[j] += xs[h][j] - sum(ex[i] * xs[i][j] for i in alive) / s
                alive.remove(h)
        for j, f in enumerate(features):
            w[j] += lr * grad[j] / len(data)
            if f in POSITIVE_ONLY:
                w[j] = max(w[j], 0.0)
    coef = dict(zip(features, w))
    params["coef"] = coef
    return params


def loglik(races, params, top=3):
    """1〜top着の並びの平均対数尤度（大きいほど良い）"""
    total = 0.0
    for r in races:
        p = probs(r["feats"], params)
        remaining = dict(p)
        for h in r["order"][:top]:
            if h not in remaining:
                break
            s = sum(remaining.values())
            total += math.log(max(remaining[h] / s, 1e-9))
            del remaining[h]
    return total / len(races)
