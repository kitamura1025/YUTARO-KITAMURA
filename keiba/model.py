"""特徴量から各馬の勝率を出すモデル（条件付きロジット / Plackett-Luce）。

勝率 ∝ exp(Σ 重み × 標準化した特徴量)。重みは backtest.py で過去レースの
1〜3着の並びが最も出やすくなるように学習し、params.json に保存する。
"""
import json
import math
from pathlib import Path

from .features import FEATURES

PARAMS_FILE = Path(__file__).resolve().parent.parent / "params.json"


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
    data = []
    for r in races:
        zs = {k: standardize(v, params) for k, v in r["feats"].items()}
        data.append((zs, [h for h in r["order"][:top] if h in zs]))
    coef = {f: (1.0 if f == "market" else 0.0) for f in features}
    for _ in range(iters):
        grad = {f: -l2 * coef[f] * len(data) for f in features}
        for zs, order in data:
            remaining = set(zs)
            for h in order:
                u = {k: sum(coef[f] * zs[k][f] for f in features) for k in remaining}
                m = max(u.values())
                ex = {k: math.exp(u[k] - m) for k in remaining}
                s = sum(ex.values())
                for f in features:
                    grad[f] += zs[h][f] - sum(ex[k] * zs[k][f] for k in remaining) / s
                remaining.discard(h)
        for f in features:
            coef[f] += lr * grad[f] / len(data)
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
