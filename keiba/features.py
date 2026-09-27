"""予想に使う特徴量を作る。

すべて「対象レースの日付より前」の成績だけを使う（バックテストで未来の情報を使わないため）。

- スピード指数 : 基準タイム（コース・距離・馬場）との差を 1000m あたりの秒に直したもの
- レースレベル : 勝ち馬（自分が勝った時は2着馬）のその後のスピード指数
- 参考度       : 過去の1走が今回の条件（芝ダ・距離・馬場・コース・回り）にどれだけ近いか
- 展開         : 各馬の位置取り傾向から今回のペースを予想し、脚質との相性を出す
"""
import math
import statistics
from datetime import date
from functools import lru_cache

from . import netkeiba

# 右回り/左回り
TURN = {"札幌": "R", "函館": "R", "福島": "R", "新潟": "L", "東京": "L", "中山": "R",
        "中京": "L", "京都": "R", "阪神": "R", "小倉": "R"}
GOING_GROUP = {"良": 0, "稍": 1, "重": 2, "不": 3}
RECENCY = [1.0, 0.85, 0.7, 0.55, 0.45, 0.35, 0.3, 0.25]

FEATURES = [
    "speed_best",    # 条件の近い過去走のベストスピード指数
    "speed_avg",     # 参考度×近さで重み付けした平均スピード指数
    "level",         # 相手関係（レースレベル）込みの評価
    "course_fit",    # 同じコース・距離帯での成績（自身の平均との差）
    "going_fit",     # 今回と同じ馬場状態グループでの成績（自身の平均との差）
    "front",         # 位置取り（前に行ける馬ほど大きい）
    "pace_fit",      # 予想ペースと脚質の相性（ハイペース×差し馬 ほど大きい）
    "layoff",        # 休養明けの度合い log(日数)
    "weight_diff",   # 前走からの斤量増減
    "age",           # 年齢
    "inner",         # 内枠(1〜4枠)
    "experience",    # 同じ芝ダ・距離帯の出走数 log(1+n)
    "jockey",        # 騎手（人気以上に3着内に来ているか。直近2年）
    "trainer",       # 調教師（同上。直近3年）
    "jockey_trainer",  # 騎手×調教師の相性
    "jockey_course", # 騎手×競馬場（芝ダ別）
    "sire",          # 父の産駒が 同じ芝ダ・距離帯・馬場 で人気以上に走っているか
    "damsire",       # 母父（同上）
    "track_fit",     # 開催中の馬場傾向（前有利/内有利）と脚質・枠の相性
    "market",        # 単勝オッズから見た勝率の対数
]

# 馬自身の適性（コース・馬場）の回数ルール: 引き戻しの強さと最低回数
APT_K, APT_MIN_N = 3.0, 2


def parse_date(s):
    y, m, d = (int(x) for x in s.replace("-", "/").split("/"))
    return date(y, m, d)


# True にするとキャッシュにある成績だけを使い、サイトへは取りに行かない（バックテスト用）
OFFLINE = False


@lru_cache(maxsize=None)
def history(horse_id, max_age_days=3):
    if not horse_id:
        return ()
    if OFFLINE:
        try:
            netkeiba.fetch(f"{netkeiba.DB}/horse/result/{horse_id}/", cached_only=True)
        except netkeiba.NotCached:
            return ()
        max_age_days = None
    return tuple(netkeiba.horse_history(horse_id, max_age_days=max_age_days))


# ---- 基準タイム ----

@lru_cache(maxsize=1)
def standards():
    """キャッシュ済みの全成績から、コース・距離別の良馬場の勝ちタイム中央値と、馬場による差を作る。"""
    winners = {}
    for path in netkeiba.CACHE.glob("db_netkeiba_com_horse_result_*"):
        hid = path.name.rsplit("_", 2)[-2] if path.name.endswith("_") else path.name.rsplit("_", 1)[-1]
        for r in history(hid, max_age_days=None):
            if r.jra and r.time and r.diff is not None and r.race_id and r.surface in ("芝", "ダ"):
                winners[r.race_id] = (r.place, r.surface, r.distance, r.going, r.time - max(r.diff, 0.0))
    good = {}
    for place, surf, dist, going, t in winners.values():
        if going == "良":
            good.setdefault((place, surf, dist), []).append(t)
    std = {k: statistics.median(v) for k, v in good.items() if len(v) >= 3}
    offs = {}
    for place, surf, dist, going, t in winners.values():
        base = std.get((place, surf, dist))
        if base and going in GOING_GROUP:
            offs.setdefault((surf, going), []).append((t - base) / (dist / 1000))
    going_off = {k: statistics.median(v) for k, v in offs.items() if len(v) >= 5}
    snap_path = netkeiba.CACHE.parent / "snapshot.json"
    if snap_path.exists() and not OFFLINE:
        # 新しい環境などでキャッシュが少ないときは、保存済みの基準タイムを使う
        import json
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        if len(snap["standards"]) > len(std):
            std = {}
            for k, v in snap["standards"].items():
                place, surf, dist = k.split("|")
                std[(place, surf, int(dist))] = v
            going_off = {tuple(k.split("|")): v for k, v in snap["going_off"].items()}
    return std, going_off


def speed_figure(run):
    """1000mあたり基準より何秒速いか（大きいほど優秀）。基準がない条件は None。"""
    std, going_off = standards()
    base = std.get((run.place, run.surface, run.distance))
    if base is None or run.time is None:
        return None
    km = run.distance / 1000
    adj = going_off.get((run.surface, run.going), 0.0) * km
    return (base + adj - run.time) / km


# ---- 参考度 ----

def relevance(run, target):
    """過去の1走が今回の条件にどれだけ近いか（0〜1）"""
    if run.surface != target["surface"]:
        return 0.15
    rel = max(0.15, 1 - abs(run.distance - target["distance"]) / 600)
    if run.jra and target.get("place") in TURN and TURN.get(run.place) != TURN[target["place"]]:
        rel *= 0.85
    if run.place == target.get("place"):
        rel = min(1.0, rel * 1.15)
    g1, g2 = GOING_GROUP.get(run.going), GOING_GROUP.get(target.get("going", ""))
    if g1 is not None and g2 is not None:
        rel *= 1 - 0.1 * abs(g1 - g2)
    if not run.jra:
        rel *= 0.6  # 海外・地方は指数が出せないので参考程度
    return rel


def run_value(run):
    """スピード指数が出せない走（海外など）は着差から近似する"""
    fig = speed_figure(run)
    if fig is not None:
        return fig
    if run.diff is not None and run.distance:
        return -max(min(run.diff, 2.5), -0.5) / (run.distance / 1000)
    if run.rank:
        return -0.08 * (run.rank - 1)
    return None


# ---- レースレベル ----

def race_level(run, before):
    """その走の勝ち馬（自分が勝った時は2着馬）が、その後 before までに出したベストのスピード指数"""
    if not run.other_id:
        return None
    d0 = parse_date(run.date)
    figs = [speed_figure(r) for r in history(run.other_id)
            if d0 < parse_date(r.date) < before and r.surface == run.surface]
    figs = [f for f in figs if f is not None]
    return max(figs) if figs else None


# ---- 展開 ----

def early_position(runs):
    vals = [r.early for r in runs[:5] if r.early is not None]
    return sum(vals) / len(vals) if vals else None


def pace_forecast(early_by_horse):
    """前に行きたい馬の数から今回のペースを予想。
    返り値: (ペース指数, 説明)  ペース指数 >0 でハイペース寄り"""
    es = [e for e in early_by_horse.values() if e is not None]
    if not es:
        return 0.0, "不明"
    n_front = sum(e <= 0.15 for e in es)
    n_lead = sum(e <= 0.05 for e in es)
    idx = (n_front - 0.15 * len(es)) / max(1.0, math.sqrt(len(es)) * 0.5) + 0.5 * max(0, n_lead - 1)
    label = "ハイペース" if idx > 1.0 else "やや速い" if idx > 0.3 else "スロー" if idx < -0.7 else "平均"
    return idx, label


# ---- まとめ ----

def horse_features(entry, target, before, pace_idx, market_p=None, bias=None):
    """entry: netkeiba.Entry か同じ属性を持つもの / target: 今回の条件 / before: 今回の日付
    bias: stats.meet_bias の結果（開催中の傾向）"""
    from . import stats
    runs = [r for r in history(entry.horse_id) if parse_date(r.date) < before and r.surface in ("芝", "ダ")]
    f = dict.fromkeys(FEATURES)
    detail = []
    wsum = vsum = 0.0
    best = None
    allv, course_v, going_v, lvl = [], [], [], []
    exp_n = 0
    tg = GOING_GROUP.get(target.get("going", ""))
    for i, r in enumerate(runs[:8]):
        v = run_value(r)
        rel = relevance(r, target)
        lv = race_level(r, before) if i < 4 else None
        detail.append({"run": r, "value": v, "relevance": rel, "level": lv})
        if v is None:
            continue
        w = RECENCY[i] * rel
        wsum += w
        vsum += w * v
        allv.append(v)
        if rel >= 0.6:
            best = v if best is None else max(best, v)
        if r.surface == target["surface"] and abs(r.distance - target["distance"]) <= 200:
            exp_n += 1
            if r.place == target.get("place"):
                course_v.append(v)
        if tg is not None and r.surface == target["surface"] and GOING_GROUP.get(r.going) is not None \
                and abs(GOING_GROUP[r.going] - tg) <= (0 if tg == 0 else 1) and (tg >= 1) == (GOING_GROUP[r.going] >= 1):
            going_v.append(v)
        if lv is not None and r.diff is not None and r.distance:
            lvl.append((RECENCY[i], lv - max(r.diff, 0.0) / (r.distance / 1000)))
    mean_all = sum(allv) / len(allv) if allv else None

    def shrunk(vals):
        """回数が少ないほど 0 に引き戻す。最低回数未満は使わない"""
        if len(vals) < APT_MIN_N or mean_all is None:
            return None
        return (sum(vals) / len(vals) - mean_all) * len(vals) / (len(vals) + APT_K)

    f["speed_avg"] = vsum / wsum if wsum else None
    f["speed_best"] = best
    f["level"] = sum(w * x for w, x in lvl) / sum(w for w, _ in lvl) if lvl else None
    f["course_fit"] = shrunk(course_v)
    f["going_fit"] = shrunk(going_v) if tg else None
    e = early_position(runs)
    f["front"] = None if e is None else 0.5 - e
    f["pace_fit"] = None if e is None else pace_idx * (e - 0.5)
    if runs:
        f["layoff"] = math.log1p((before - parse_date(runs[0].date)).days)
        f["weight_diff"] = (entry.weight - runs[0].weight) if runs[0].weight and entry.weight else None
    f["age"] = entry.age
    f["inner"] = 1.0 if entry.waku <= 4 else 0.0
    f["experience"] = math.log1p(exp_n)
    f["market"] = math.log(market_p) if market_p else None
    conn, ped = stats.connections(entry, target, before)
    for k, st in conn.items():
        f[k] = st.value
    f["track_fit"] = stats.track_fit(bias, e, entry.waku) if bias else None
    return f, {"runs": detail, "early": e, "course_n": len(course_v), "going_n": len(going_v),
               "conn": conn, "ped": ped}
