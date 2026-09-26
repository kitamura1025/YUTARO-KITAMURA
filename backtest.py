"""過去の重賞でモデルを学習・検証し、北村式フォーメーションの成績を出す。

使い方:
  python3 backtest.py --collect 200     # 重賞を辿って検証レースを集める（初回は時間がかかる。キャッシュされる）
  python3 backtest.py --all             # キャッシュ済みの全クラスのレースも検証に使う（出走馬の成績・血統を取得）
  python3 backtest.py                   # 学習（古い7割）→ 検証（新しい3割）→ 全レースで再学習して params.json 保存

検証レースは「今回の出走馬 → その過去の重賞 → その出走馬の過去の重賞…」と辿って集める。
特徴量はすべてそのレースの日付より前の情報だけで作る。
"""
import argparse
import json
import math
import pickle
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from keiba import betting, features, model, netkeiba, stats

DATA = Path(__file__).resolve().parent / "data"
RACES_FILE = DATA / "backtest_races.json"
DATASET_FILE = DATA / "dataset.pkl"
GRADED = {"G1", "G2", "G3"}


def collect(seed_race, limit, since):
    _, entries = netkeiba.race_card(seed_race)
    queue = [e.horse_id for e in entries]
    seen_h, races = set(), {}
    while queue and len(races) < limit:
        hid = queue.pop(0)
        if hid in seen_h:
            continue
        seen_h.add(hid)
        for r in features.history(hid):
            if r.jra and r.cls in GRADED and r.date >= since and r.race_id not in races and r.surface in ("芝", "ダ"):
                races[r.race_id] = r.date
                try:
                    _, members = netkeiba.race_members(r.race_id)
                except Exception as ex:  # noqa: BLE001
                    print("skip", r.race_id, ex)
                    continue
                queue += [m["horse_id"] for m in members]
                print(f"[{len(races)}/{limit}] {r.date} {r.name}", flush=True)
                if len(races) >= limit:
                    break
    ids = sorted(races, key=races.get)
    RACES_FILE.write_text(json.dumps(ids))
    return ids


def build_race(rid):
    info, members = netkeiba.race_members(rid)
    members = [m for m in members if m["odds"] and m["horse_id"]]
    if len(members) < 8 or not info["date"]:
        return None
    before = features.parse_date(info["date"])
    total = sum(1 / m["odds"] for m in members)
    entries = {m["umaban"]: SimpleNamespace(**m) for m in members}
    early = {k: features.early_position([r for r in features.history(e.horse_id)
                                         if features.parse_date(r.date) < before]) for k, e in entries.items()}
    pace_idx, _ = features.pace_forecast(early)
    bias = stats.meet_bias(rid, info["surface"], info["going"], before)
    feats = {k: features.horse_features(e, info, before, pace_idx, (1 / e.odds) / total, bias)[0]
             for k, e in entries.items()}
    order = [m["umaban"] for m in sorted(members, key=lambda m: m["rank"] or 99) if m["rank"]]
    return {"id": rid, "date": info["date"], "name": info["name"], "cls": info["cls"], "feats": feats,
            "order": order, "trifecta": info.get("trifecta")}


def cached_races():
    """キャッシュ済みのレース結果のうち、検証に使えるもの（JRA芝ダ・8頭以上・オッズあり）"""
    ids = []
    for path in netkeiba.CACHE.glob("db_netkeiba_com_race_[0-9]*"):
        rid = path.name.rsplit("_", 2)[-2]
        info, members = netkeiba.race_members(rid, cached_only=True)
        if info["surface"] in ("芝", "ダ") and sum(1 for m in members if m["odds"]) >= 8:
            ids.append(rid)
    return sorted(ids)


def prefetch(ids, workers=2):
    """出走馬の成績と血統を先に全部取っておく（2本並行）。
    基準タイムはキャッシュ済みの全成績から作るので、特徴量を作る前に済ませる。"""
    horses = set()
    for rid in ids:
        _, members = netkeiba.race_members(rid)
        horses |= {m["horse_id"] for m in members if m["horse_id"]}
    todo = sorted(horses)
    print(f"  成績・血統の取得 {len(todo)}頭", flush=True)

    def one(hid):
        try:
            netkeiba.horse_history(hid, max_age_days=None)
            netkeiba.pedigree(hid)
        except Exception as ex:  # noqa: BLE001
            print("skip", hid, ex, flush=True)

    with ThreadPoolExecutor(workers) as ex:
        for i, _ in enumerate(ex.map(one, todo)):
            if i % 500 == 0:
                print(f"  成績・血統 {i}/{len(todo)}", flush=True)


def load_dataset(ids, rebuild=False):
    cached = pickle.loads(DATASET_FILE.read_bytes()) if DATASET_FILE.exists() and not rebuild else {}
    if any(rid not in cached for rid in ids):
        prefetch([rid for rid in ids if rid not in cached])
    features.OFFLINE = True  # ここから先はキャッシュだけで計算する
    features.standards.cache_clear()
    features.history.cache_clear()
    stats.table.cache_clear()
    out = []
    for i, rid in enumerate(ids):
        if rid not in cached:
            try:
                cached[rid] = build_race(rid)
            except Exception as ex:  # noqa: BLE001
                print("skip", rid, ex)
                cached[rid] = None
            if i % 20 == 0:
                print(f"  特徴量作成 {i}/{len(ids)}", flush=True)
        if cached[rid]:
            out.append(cached[rid])
    DATASET_FILE.write_bytes(pickle.dumps(cached))
    return out


def simulate(races, params, label):
    """毎レース北村式48点を買った場合の成績を、軸・対抗・相手の選び方ごとに出す"""
    for strategy in betting.STRATEGIES:
        hits = paid = cost = 0
        for r in races:
            p = model.probs(r["feats"], params)
            mk = {k: math.exp(v["market"]) for k, v in r["feats"].items() if v.get("market") is not None}
            tickets = set(betting.formation(*betting.select(p, mk, strategy)))
            cost += len(tickets) * 100
            if tuple(r["order"][:3]) in tickets:
                hits += 1
                paid += r["trifecta"][1] if r.get("trifecta") else 0
        print(f"  {label:<10} {strategy:<6} 的中 {hits:>2}/{len(races)} ({hits / len(races):.0%})  回収率 {paid / cost:.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, help="集めるレース数")
    ap.add_argument("--seed", default="202606040911")
    ap.add_argument("--since", default="2023/01/01")
    ap.add_argument("--rebuild", action="store_true", help="特徴量を作り直す")
    ap.add_argument("--all", action="store_true", help="キャッシュ済みの全クラスのレースも使う")
    a = ap.parse_args()

    ids = collect(a.seed, a.collect, a.since) if a.collect else json.loads(RACES_FILE.read_text())
    if a.all:
        ids = sorted(set(ids) | set(cached_races()))
        RACES_FILE.write_text(json.dumps(ids))
    races = sorted(load_dataset(ids, a.rebuild), key=lambda r: r["date"])
    cut = int(len(races) * 0.7)
    train, test = races[:cut], races[cut:]
    print(f"\n検証レース {len(races)}（学習 {len(train)} : {train[0]['date']}〜 / 確認 {len(test)} : {test[0]['date']}〜）")

    market = {"mean": {}, "sd": {}, "coef": {"market": 1.0}}
    market = model.fit(train, ["market"])
    tuned = model.fit(train)
    print("\n■ 確認用レース（学習に使っていない新しいレース）での成績")
    print(f"  1〜3着の並びの対数尤度  市場オッズのみ {model.loglik(test, market):.3f} / モデル {model.loglik(test, tuned):.3f}"
          "（大きいほど良い）")
    simulate(test, market, "市場オッズのみ")
    simulate(test, tuned, "モデル")

    print("\n■ 要素グループごとの効果（確認用レース。市場オッズだけの値より大きければ効果あり）")
    groups = {"タイム・相手関係": ["speed_best", "speed_avg", "level"], "適性": ["course_fit", "going_fit", "experience"],
              "展開": ["front", "pace_fit"], "開催傾向": ["track_fit"],
              "騎手・調教師": ["jockey", "trainer", "jockey_trainer", "jockey_course"], "血統": ["sire", "damsire"],
              "ローテ・その他": ["layoff", "weight_diff", "age", "inner"]}
    print(f"  市場オッズのみ {model.loglik(test, market):.4f}")
    for g, fs in groups.items():
        print(f"  市場＋{g:<10} {model.loglik(test, model.fit(train, ['market'] + fs)):.4f}")
    graded = [r for r in test if r.get("cls") in GRADED]
    other = [r for r in test if r.get("cls") not in GRADED]
    for label, rs in (("重賞", graded), ("重賞以外", other)):
        if len(rs) >= 20:
            print(f"\n■ 確認用レースの内訳: {label} {len(rs)}R  市場 {model.loglik(rs, market):.4f} / モデル {model.loglik(rs, tuned):.4f}")
            simulate(rs, tuned, "モデル")

    final = model.fit(races)
    print("\n■ 全レースで学習した重み（標準化後。+は勝率を上げる方向）")
    for f, c in sorted(final["coef"].items(), key=lambda x: -abs(x[1])):
        print(f"  {f:<12} {c:+.3f}")
    model.save_params(final)
    print("params.json に保存しました")


if __name__ == "__main__":
    main()
