"""騎手・調教師・血統・開催中の馬場傾向の集計。

■ 回数の少ないデータを信用しないルール（予想全体の共通ルール）
  - 成績は「人気から期待される3着内率」との差（上振れ・下振れ）で測る。
    人気馬ばかりに乗る騎手の勝率が高いのは当たり前なので、人気の分を差し引く。
  - 回数 n が少ないほど 0（=平均並み）に引き戻す:  推定値 = 差の合計 / (n + K)
    例: K=50 なら、10回で+30%でも推定は+5%。1回だけの100%勝率は意味を持たない。
  - n が MIN_N 未満なら「参考外」として使わない（None）。
"""
import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from . import netkeiba
from . import features
from .features import history, parse_date, speed_figure

# (引き戻しの強さ K, 最低回数 MIN_N, 集計期間[日])
RULES = {
    "jockey": (100, 50, 730),
    "trainer": (100, 50, 1095),
    "jockey_trainer": (30, 15, 1095),
    "jockey_course": (50, 30, 1095),
    "sire": (60, 30, 3650),
    "damsire": (80, 40, 3650),
    "meet": (4, 3, None),  # 開催中の傾向（レース数）
}


def dist_band(d):
    return "短" if d <= 1400 else "マ" if d <= 1800 else "中" if d <= 2200 else "長"


def going_band(g):
    """良 / 稍重 / 重・不良 の3区分"""
    return {"良": "良", "稍": "稍", "重": "重不", "不": "重不"}.get(g, "")


@dataclass
class Stat:
    value: float | None  # 引き戻し後の上振れ幅（3着内率の差）。参考外なら None
    n: int
    raw: float | None  # 引き戻し前の平均の差

    def text(self):
        if self.n == 0:
            return "データなし"
        tag = "" if self.value is not None else "・参考外"
        return f"{self.raw:+.0%}（{self.n}回{tag}）"


class RunTable:
    """キャッシュ済みデータを、騎手・調教師・血統などのキーで引けるようにしたもの。

    - 騎手・調教師: レース結果ページ（全出走馬の騎手・調教師・人気・着順）から
    - 血統: 馬の成績表 × 血統ページ（父・母父）から
    """

    def __init__(self):
        rides = []  # (日付, オッズ, 着順, キー群)
        seen = set()
        for path in netkeiba.CACHE.glob("db_netkeiba_com_race_[0-9]*"):
            rid = path.name.rsplit("_", 2)[-2]
            try:
                info, members = netkeiba.race_members(rid, cached_only=True)
            except Exception:  # noqa: BLE001
                continue
            if not info.get("date") or info["surface"] not in ("芝", "ダ"):
                continue
            d = parse_date(info["date"])
            seen.add(rid)
            for m in members:
                if m["odds"] and m["rank"]:
                    j, t = m.get("jockey_id", ""), m.get("trainer_id", "")
                    rides.append((d, m["odds"], m["rank"], [("jockey", j), ("trainer", t), ("jockey_trainer", (j, t)),
                                                           ("jockey_course", (j, info["place"], info["surface"]))]))
        for path in netkeiba.CACHE.glob("db_netkeiba_com_horse_result_*"):
            hid = path.name.rsplit("_", 2)[-2]
            try:
                ped = netkeiba.pedigree(hid, cached_only=True)
            except netkeiba.NotCached:
                continue
            for r in history(hid, max_age_days=None):
                if r.jra and r.odds and r.rank and r.surface in ("芝", "ダ"):
                    band = (r.surface, dist_band(r.distance), going_band(r.going))
                    rides.append((parse_date(r.date), r.odds, r.rank,
                                  [("sire", (ped.get("sire_id", ""),) + band),
                                   ("damsire", (ped.get("damsire_id", ""),) + band)]))
        # 人気（オッズ）から期待される3着内率を、データ自体から作る
        buckets = defaultdict(lambda: [0, 0])
        for _, odds, rank, _ in rides:
            b = self._bucket(odds)
            buckets[b][0] += rank <= 3
            buckets[b][1] += 1
        self.expect = {b: a / n for b, (a, n) in buckets.items() if n}
        self.index = defaultdict(list)
        for d, odds, rank, keys in rides:
            resid = (rank <= 3) - self.expect[self._bucket(odds)]
            for kind, key in keys:
                if all(key if isinstance(key, tuple) else [key]):
                    self.index[(kind, key)].append((d, resid, 1))
        for v in self.index.values():
            v.sort()
        self.n_rows = len(rides)
        self.n_races = len(seen)

    @staticmethod
    def _bucket(odds):
        edges = [1.5, 2, 3, 4, 5, 7, 10, 15, 20, 30, 50, 100]
        return bisect.bisect_left(edges, odds)

    def stat(self, kind, key, before):
        k, min_n, window = RULES[kind]
        lst = self.index.get((kind, key), [])
        lo = bisect.bisect_left(lst, (before - timedelta(days=window),)) if window else 0
        hi = bisect.bisect_left(lst, (before,))
        n = sum(c for _, _, c in lst[lo:hi])
        if n == 0:
            return Stat(None, 0, None)
        s = sum(x for _, x, _ in lst[lo:hi])
        return Stat(s / (n + k) if n >= min_n else None, n, s / n)

    # ---- スナップショット（集めたデータの要約をリポジトリに保存して、新しい環境でも使う） ----

    def to_snapshot(self):
        """キーごとに月単位で (回数, 上振れの合計) にまとめる"""
        out = {}
        for (kind, key), lst in self.index.items():
            months = defaultdict(lambda: [0, 0.0])
            for d, x, c in lst:
                m = months[d.strftime("%Y-%m-01")]
                m[0] += c
                m[1] += x
            k = kind + "|" + ("|".join(key) if isinstance(key, tuple) else key)
            out[k] = [[m, n, round(sx, 3)] for m, (n, sx) in sorted(months.items())]
        return {"n_rows": self.n_rows, "index": out}

    @classmethod
    def from_snapshot(cls, snap):
        self = cls.__new__(cls)
        self.index = defaultdict(list)
        for k, rows in snap["index"].items():
            kind, *key = k.split("|")
            key = tuple(key) if len(key) > 1 else key[0]
            self.index[(kind, key)] = [(parse_date(m.replace("-", "/")), sx, n) for m, n, sx in rows]
        self.n_rows, self.n_races, self.expect = snap["n_rows"], 0, {}
        return self


SNAPSHOT = netkeiba.CACHE.parent / "snapshot.json"


@lru_cache(maxsize=1)
def table():
    """キャッシュから作った集計と、保存済みスナップショットのうち、データが多い方を使う"""
    live = RunTable()
    if SNAPSHOT.exists() and not features.OFFLINE:
        import json
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        if snap["stats"]["n_rows"] > live.n_rows:
            return RunTable.from_snapshot(snap["stats"])
    return live


def save_snapshot():
    """基準タイムと騎手・調教師・血統の集計を data/snapshot.json に保存（git管理）"""
    import json
    std, going_off = features.standards()
    snap = {"standards": {"|".join(map(str, k)): round(v, 2) for k, v in std.items()},
            "going_off": {"|".join(k): round(v, 3) for k, v in going_off.items()},
            "stats": table().to_snapshot()}
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return SNAPSHOT.stat().st_size


def connections(entry, target, before):
    """騎手・調教師・騎手×調教師・騎手×コース・父・母父の Stat をまとめて返す"""
    t = table()
    try:
        ped = netkeiba.pedigree(entry.horse_id, cached_only=features.OFFLINE)
    except Exception:  # noqa: BLE001
        ped = {}
    tid = getattr(entry, "trainer_id", "") or ""
    jid = getattr(entry, "jockey_id", "") or ""
    band = (target["surface"], dist_band(target["distance"]), going_band(target.get("going", "")))
    return {
        "jockey": t.stat("jockey", jid, before),
        "trainer": t.stat("trainer", tid, before),
        "jockey_trainer": t.stat("jockey_trainer", (jid, tid), before),
        "jockey_course": t.stat("jockey_course", (jid, target.get("place", ""), target["surface"]), before),
        "sire": t.stat("sire", (ped.get("sire_id", ""),) + band, before),
        "damsire": t.stat("damsire", (ped.get("damsire_id", ""),) + band, before),
    }, ped


# ---- 開催中の馬場傾向 ----

@dataclass
class MeetBias:
    front: float | None  # +なら前に行った馬が有利（引き戻し後）
    inner: float | None  # +なら内枠が有利
    speed: float | None  # 勝ち時計の指数（+なら時計が速い馬場）
    n: int
    used: list  # 使ったレース
    skipped: list  # 馬場状態が違うので使わなかったレース

    def text(self):
        if not self.n:
            return "同じ条件のレースがまだない"
        tag = "" if self.front is not None else "（レース数が少なく参考外）"
        return (f"{self.n}R: 前有利度 {self.front_raw:+.2f} / 内有利度 {self.inner_raw:+.2f} / "
                f"時計 {self.speed:+.2f}{tag}") if self.speed is not None else f"{self.n}R{tag}"


def meet_race_ids(race_id, race_date, surface):
    """同じ開催の前日（前の開催日）と当日の、対象レースより前の同じ芝ダのレースID"""
    day, rno = int(race_id[8:10]), int(race_id[10:12])
    ids = []
    for d in (day - 1, day):
        if d < 1:
            continue
        key = f"{race_id[:8]}{d:02d}"
        # 前の開催日は1〜8日前のどこか（連続開催・中1週など）
        for back in ([0] if d == day else range(1, 9)):
            try:
                day_list = netkeiba.race_list((race_date - timedelta(days=back)).strftime("%Y%m%d"),
                                              cached_only=features.OFFLINE)
            except netkeiba.NotCached:
                continue
            found = {k: v for k, v in day_list.items() if k.startswith(key)}
            if found:
                ids += [k for k, (sf, _) in sorted(found.items())
                        if sf == surface and (d < day or int(k[10:12]) < rno)]
                break
    return ids


def meet_bias(race_id, surface, going, race_date=None):
    """同じ開催の前日と当日の前のレースから、同じ芝ダ・同じ馬場状態のものだけで傾向を出す。
    馬場状態が違う日のレースは使わない（例: 土曜が重で日曜が良なら土曜は除外）。"""
    used, skipped = [], []
    fronts, inners, speeds = [], [], []
    if race_date is None:
        ids = [f"{race_id[:8]}{d:02d}{r:02d}" for d in (int(race_id[8:10]) - 1, int(race_id[8:10])) if d >= 1
               for r in range(1, 13 if d < int(race_id[8:10]) else int(race_id[10:12]))]
    else:
        ids = meet_race_ids(race_id, race_date, surface)
    for rid in ids:
        d, r = int(rid[8:10]), int(rid[10:12])
        try:
            info, members = netkeiba.race_members(rid, cached_only=features.OFFLINE)
            if not members and not features.OFFLINE:  # db.netkeiba に載る前（当日・前日）は速報ページから
                info, members = netkeiba.race_members_live(rid)
        except Exception:  # noqa: BLE001
            continue
        if info["surface"] != surface or len(members) < 6:
            continue
        if going_band(info["going"]) != going_band(going):
            skipped.append(f"{d}日目{r}R({info['going']})")
            continue
        ms = [m for m in members if m["rank"] and m["passage"]]
        top = [m for m in ms if m["rank"] <= 3]
        if len(top) < 3:
            continue
        fs = len(ms)
        early = lambda m: (m["passage"][0] - 1) / max(fs - 1, 1)  # noqa: E731
        fronts.append(0.5 - sum(early(m) for m in top) / len(top))
        inners.append(sum(m["waku"] for m in ms) / fs - sum(m["waku"] for m in top) / len(top))
        win = min(ms, key=lambda m: m["rank"])
        run = SimpleRun(place=info["place"], surface=surface, distance=info["distance"],
                        going=info["going"], time=win["time"])
        fig = speed_figure(run)
        if fig is not None:
            speeds.append(fig)
        used.append(f"{d}日目{r}R")
    k, min_n, _ = RULES["meet"]
    n = len(fronts)
    mb = MeetBias(
        front=sum(fronts) / (n + k) if n >= min_n else None,
        inner=sum(inners) / (n + k) if n >= min_n else None,
        speed=sum(speeds) / len(speeds) if speeds else None,
        n=n, used=used, skipped=skipped)
    mb.front_raw = sum(fronts) / n if n else 0.0
    mb.inner_raw = sum(inners) / n if n else 0.0
    return mb


@dataclass
class SimpleRun:
    place: str
    surface: str
    distance: int
    going: str
    time: float | None


def track_fit(bias, early, waku):
    """開催傾向とその馬の脚質・枠の相性。傾向が参考外なら None"""
    if bias.front is None or early is None:
        return None
    return bias.front * (0.5 - early) * 4 + bias.inner * (4.5 - waku) / 3.5
