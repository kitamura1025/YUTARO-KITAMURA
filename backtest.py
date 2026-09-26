"""過去の芝短距離重賞でモデルと買い方を検証し、パラメータを調整する。

使い方:
  python3 backtest.py                # 検証のみ（params.json を使う）
  python3 backtest.py --fit          # パラメータを調整して params.json に保存
  python3 backtest.py --seed 202606040911 --depth 2   # 検証レースの集め方を変える

検証レースは seed レースの出走馬の近走から「国内・芝1000〜1400m・重賞」を辿って集める。
"""
import argparse
import itertools
import json
import math
from dataclasses import replace

from keiba import betting, model, netkeiba

SPRINT_GRADES = {"G1", "G2", "G3"}


def collect_races(seeds, depth):
    seen, frontier, found = set(), list(seeds), []
    for _ in range(depth):
        nxt = []
        for rid in frontier:
            if rid in seen:
                continue
            seen.add(rid)
            try:
                _, entries = netkeiba.race_card(rid)
            except Exception as ex:  # noqa: BLE001
                print("skip", rid, ex)
                continue
            for e in entries:
                for r in e.past:
                    if (not r.overseas and r.surface == "芝" and 1000 <= r.distance <= 1400
                            and r.grade in SPRINT_GRADES and r.race_id not in seen):
                        nxt.append(r.race_id)
                        found.append(r.race_id)
        frontier = nxt
    return sorted(set(found))


def load_race(rid):
    info, entries = netkeiba.race_card(rid)
    res = netkeiba.result(rid)
    if len(res["order"]) < 8 or not res["trifecta"]:
        return None
    odds = {num: od for _, num, od, _ in res["order"]}
    entries = [replace(e, odds=odds.get(e.umaban)) for e in entries if odds.get(e.umaban)]
    finish = [num for rk, num, _, _ in sorted(res["order"], key=lambda x: x[0] or 99) if rk]
    return {"id": rid, "info": info, "entries": entries, "going": res["going"],
            "top3": tuple(finish[:3]), "trifecta": res["trifecta"]}


def loglik(races, params):
    """実際の1-2-3着が出る確率の対数（大きいほど良いモデル）"""
    total = 0.0
    for r in races:
        p, _ = model.win_probs(r["entries"], r["info"], params, r["going"])
        total += math.log(max(betting.trifecta_prob(p, *r["top3"]), 1e-9))
    return total / len(races)


def simulate(races, params, label):
    """北村式フォーメーション48点を自動選択で毎レース買った場合の成績"""
    hits = paid = 0
    cost = 0
    axis_top2 = rival_top3 = 0
    for r in races:
        p, _ = model.win_probs(r["entries"], r["info"], params, r["going"])
        axis, rivals, others = betting.auto_select(p)
        tickets = set(betting.formation(axis, rivals, others))
        cost += len(tickets) * 100
        top3 = r["top3"]
        axis_top2 += axis in top3[:2]
        rival_top3 += any(x in top3 for x in rivals)
        if top3 in tickets:
            hits += 1
            paid += r["trifecta"][1]
    n = len(races)
    print(f"{label:<14} 的中 {hits}/{n} ({hits / n:.0%})  回収率 {paid / cost:.0%}  "
          f"軸2着以内 {axis_top2 / n:.0%}  対抗どちらか3着内 {rival_top3 / n:.0%}")
    return hits, paid / cost


def fit(races, base):
    grid = {
        "temperature": [0.25, 0.35, 0.5, 0.7],
        "market_blend": [0.0, 0.3, 0.5, 0.7, 0.85],
        "best_weight": [0.0, 0.5, 1.0],
        "going_weight": [0.0, 0.15, 0.3],
    }
    best = (-1e9, None)
    keys = list(grid)
    for vals in itertools.product(*grid.values()):
        p = dict(base, **dict(zip(keys, vals)))
        ll = loglik(races, p)
        if ll > best[0]:
            best = (ll, p)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", nargs="*", default=["202606040911", "202506040911"])
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--fit", action="store_true")
    a = ap.parse_args()

    ids = collect_races(a.seed, a.depth)
    races = [r for r in (load_race(i) for i in ids) if r]
    print(f"検証レース数: {len(races)}（国内芝1000〜1400m重賞）")

    params = model.load_params()
    market_only = dict(params, market_blend=1.0)
    print(f"対数尤度  市場のみ {loglik(races, market_only):.3f} / 現モデル {loglik(races, params):.3f}")
    simulate(races, market_only, "市場オッズのみ")
    simulate(races, params, "現モデル")

    if a.fit:
        # 半分で調整して残り半分で確かめる（当てはめすぎの確認）
        train, test = races[::2], races[1::2]
        ll, tuned = fit(train, params)
        print(f"\n調整結果（学習用 {len(train)}R）: 対数尤度 {ll:.3f}")
        print(f"  確認用 {len(test)}R: 市場のみ {loglik(test, market_only):.3f} / 調整後 {loglik(test, tuned):.3f}")
        ll_all, tuned_all = fit(races, params)
        keep = {k: tuned_all[k] for k in ("temperature", "market_blend", "best_weight", "going_weight")}
        print("  全レースで調整したパラメータ:", keep)
        simulate(races, tuned_all, "調整後モデル")
        model.PARAMS_FILE.write_text(json.dumps(keep, ensure_ascii=False, indent=2))
        print("params.json に保存しました")


if __name__ == "__main__":
    main()
