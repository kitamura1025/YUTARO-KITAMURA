"""netkeiba の公開ページからデータを取得・解析する。

取得したHTML/JSONは data/cache/ に保存し、同じURLは再取得しない
（オッズなど変化するものは refresh=True、馬の成績のように増えていくものは max_age_days で取り直す）。
"""
import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
UA = "Mozilla/5.0 (keiba-research; personal use)"
RACE = "https://race.netkeiba.com"


DB = "https://db.netkeiba.com"
WAIT = 0.7


class NotCached(Exception):
    pass


def fetch(url, refresh=False, max_age_days=None, wait=None, cached_only=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[1])[:200]
    path = CACHE / key
    if cached_only:
        if not path.exists():
            raise NotCached(url)
        return path.read_text(encoding="utf-8")
    fresh = path.exists() and not refresh and (
        max_age_days is None or time.time() - path.stat().st_mtime < max_age_days * 86400)
    if fresh:
        return path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(5):  # 連続取得中に一時的な 400/429/5xx が返ることがあるので間隔を伸ばして取り直す
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if attempt == 4 or code in (403, 404):
                raise
            time.sleep(3 * 2 ** attempt)
    enc = "euc-jp" if b"EUC-JP" in raw[:2000].upper() else "utf-8"
    body = raw.decode(enc, errors="ignore")
    path.write_text(body, encoding="utf-8")
    time.sleep(WAIT if wait is None else wait)  # サイトに負荷をかけない
    return body


def _text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


GRADE = {"GI": "G1", "JpnI": "G1", "GII": "G2", "JpnII": "G2", "GIII": "G3", "JpnIII": "G3"}


@dataclass
class PastRun:
    race_id: str
    date: str
    place: str
    name: str
    grade: str  # G1/G2/G3/OP/L/3勝/2勝/1勝/...
    surface: str  # 芝/ダ/障
    distance: int
    outer: bool
    going: str  # 良/稍/重/不
    rank: int | None
    field_size: int | None
    number: int | None
    popularity: int | None
    jockey: str
    weight: float | None
    passage: list
    last3f: float | None
    diff: float | None  # 勝ち馬との差（勝った場合は2着との差をマイナスで）
    overseas: bool


@dataclass
class Entry:
    horse_id: str
    waku: int
    umaban: int
    name: str
    sex: str
    age: int
    jockey: str
    weight: float
    trainer: str
    style: str  # 逃/先/差/追/大(大逃げ)
    past: list = field(default_factory=list)
    odds: float | None = None
    popularity: int | None = None
    training: str | None = None  # A/B/C/D
    training_note: str = ""
    jockey_id: str = ""
    trainer_id: str = ""


def _parse_past_cell(td):
    rid = re.search(r'id="myhorse_(\d+)"', td)
    d01 = re.search(r'Data01">\s*<span>(.*?)</span>\s*<span class="Num">(.*?)</span>', td, re.S)
    if not (rid and d01):
        return None
    date_place = _text(d01.group(1)).split(" ")
    rank_s = _text(d01.group(2))
    d02 = re.search(r'Data02">(.*?)</div>', td, re.S)
    name = _text(re.sub(r"<span.*?</span>", "", d02.group(1), flags=re.S)) if d02 else ""
    g = re.search(r"Icon_GradeType\d*\">(.*?)<", td)
    grade = GRADE.get(g.group(1), g.group(1)) if g else ""
    d05 = _text(re.search(r'Data05">(.*?)</div>', td, re.S).group(1))
    m = re.match(r"(芝|ダ|障)(\d+)(\(外\))?.*?(良|稍|重|不)?$", d05)
    surface, dist, outer, going = (m.group(1), int(m.group(2)), bool(m.group(3)), m.group(4) or "") if m else ("", 0, False, "")
    if not going:
        gm = re.search(r"(良|稍|重|不)", d05)
        going = gm.group(1) if gm else ""
    d03 = _text(re.search(r'Data03">(.*?)</div>', td, re.S).group(1))
    m3 = re.match(r"(\d+)頭 (\d+)番 (\d+)人 (.*?) ([\d.]+)$", d03)
    d06 = _text(re.search(r'Data06">(.*?)</div>', td, re.S).group(1))
    passage = [int(x) for x in re.findall(r"\d+", d06.split(" ")[0])] if re.match(r"[\d-]+", d06) else []
    l3 = re.search(r"\(([\d.]+)\)", d06)
    d07 = re.search(r'Data07">.*?\(([-\d.]+)\)', td, re.S)
    diff = float(d07.group(1)) if d07 else None
    rid = rid.group(1)
    overseas = not rid[4:6].isdigit() or not (1 <= int(rid[4:6]) <= 10)
    if overseas and diff == 0.0:
        diff = None  # 海外レースは着差が入らない
    return PastRun(
        race_id=rid, date=date_place[0], place=date_place[1] if len(date_place) > 1 else "",
        name=name, grade=grade, surface=surface, distance=dist, outer=outer, going=going,
        rank=int(rank_s) if rank_s.isdigit() else None,
        field_size=int(m3.group(1)) if m3 else None, number=int(m3.group(2)) if m3 else None,
        popularity=int(m3.group(3)) if m3 else None, jockey=m3.group(4) if m3 else "",
        weight=float(m3.group(5)) if m3 else None, passage=passage,
        last3f=float(l3.group(1)) if l3 and float(l3.group(1)) > 0 else None,
        diff=diff, overseas=overseas,
    )


def race_card(race_id, refresh=False):
    """出馬表（5走表示）を解析して (レース情報, [Entry]) を返す。"""
    t = fetch(f"{RACE}/race/shutuba_past.html?race_id={race_id}", refresh=refresh)
    title = _text(re.search(r"<title>(.*?)</title>", t, re.S).group(1))
    rd = _text(re.search(r'RaceData01">(.*?)</div>', t, re.S).group(1))
    m = re.search(r"(芝|ダ)(\d+)m", rd)
    info = {"race_id": race_id, "title": title, "surface": m.group(1) if m else "",
            "distance": int(m.group(2)) if m else 0, "outer": "外" in rd}
    entries = []
    for row in re.findall(r'<tr class="HorseList.*?</tr>', t, re.S):
        tds = re.findall(r"<td class=\"(.*?)\"[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 5 or not tds[0][0].startswith("Waku"):
            continue
        info_td = row
        name = _text(re.search(r'Horse02">\s*<a[^>]*>(.*?)</a>', info_td, re.S).group(1))
        trainer = _text(re.search(r'Horse05">(.*?)</div>', info_td, re.S).group(1))
        style = re.search(r'kyakusitu">(.*?)<', info_td)
        barei = re.search(r'Barei">(.*?)<', row).group(1)
        jk = re.search(r'class="Jockey">.*?<a[^>]*>(.*?)</a>.*?<span>([\d.]+)</span>', row, re.S)
        past = [p for p in (_parse_past_cell(td) for td in re.findall(r'<td class="Past.*?</td>', row, re.S)) if p]
        hid = re.search(r'db\.netkeiba\.com/horse/(\w+)', info_td)
        jid = re.search(r'class="Jockey">.*?/jockey/(?:result/recent/)?(\w+)', row, re.S)
        tid = re.search(r'/trainer/(?:result/recent/)?(\w+)', info_td)
        entries.append(Entry(
            horse_id=hid.group(1) if hid else "", waku=int(_text(tds[0][1])), umaban=int(_text(tds[1][1])), name=name,
            sex=barei[0], age=int(re.search(r"\d+", barei).group(0)),
            jockey=_text(jk.group(1)) if jk else "", weight=float(jk.group(2)) if jk else 0.0,
            trainer=trainer, style=style.group(1) if style else "", past=past,
            jockey_id=jid.group(1) if jid else "", trainer_id=tid.group(1) if tid else "",
        ))
    return info, entries


def odds(race_id, kind=1, refresh=True):
    """kind=1 単勝 {馬番: (odds, 人気)} / kind=8 3連単 {(a,b,c): odds}"""
    t = fetch(f"{RACE}/api/api_get_jra_odds.html?race_id={race_id}&type={kind}&action=update", refresh=refresh)
    d = json.loads(t).get("data") or {}
    o = (d.get("odds") or {}).get(str(kind)) or {}
    out = {}
    for k, v in o.items():
        try:
            val = float(v[0])
        except ValueError:
            continue
        if kind == 1:
            out[int(k)] = (val, int(v[2]) if v[2].isdigit() else None)
        else:
            out[tuple(int(k[i:i + 2]) for i in range(0, len(k), 2))] = val
    return out, d.get("update_datetime", "")


def training(race_id, refresh=False):
    """調教の短評と評価（A〜D）。{馬番: (評価, 短評)}"""
    t = fetch(f"{RACE}/race/oikiri.html?race_id={race_id}", refresh=refresh)
    out = {}
    for row in re.findall(r'<tr class = "OikiriDataHead\d+ HorseList".*?</tr>', t, re.S):
        num = re.search(r'class="Umaban">(\d+)</td>', row)
        crit = re.search(r'Training_Critic">(.*?)</td>', row, re.S)
        rank = re.search(r'class="Rank_[^"]*">([A-D])</td>', row)
        if num and rank:
            out[int(num.group(1))] = (rank.group(1), _text(crit.group(1)) if crit else "")
    return out


def result(race_id):
    """確定結果。{'order': [(着順, 馬番, 単勝オッズ, 人気)], 'going': 馬場, 'trifecta': (組, 払戻)}"""
    t = fetch(f"{RACE}/race/result.html?race_id={race_id}")
    order = []
    for row in re.findall(r'<tr\s+class="(?:FirstDisplay )?HorseList"[^>]*>\s*<td class="Result_Num">.*?</tr>', t, re.S):
        rk = re.search(r'class="Rank">(.*?)<', row)
        num = re.findall(r'<td class="Num[^"]*">\s*<div>(\d+)</div>', row)
        od = re.search(r'<td class="Odds Txt_R">\s*<span[^>]*>([\d.]+)</span>', row)
        pp = re.search(r'OddsPeople">(\d+)<', row)
        if rk and len(num) >= 2:
            r = _text(rk.group(1))
            order.append((int(r) if r.isdigit() else None, int(num[1]),
                          float(od.group(1)) if od else None, int(pp.group(1)) if pp else None))
    rd = re.search(r'RaceData01">(.*?)</div>', t, re.S)
    gm = re.search(r"馬場:(良|稍|重|不)", _text(rd.group(1))) if rd else None
    tri = re.search(r'<tr class="Tan3">.*?<td class="Result">(.*?)</td>\s*<td class="Payout">(.*?)</td>', t, re.S)
    trifecta = None
    if tri:
        combo = tuple(int(x) for x in re.findall(r"<span>(\d+)</span>", tri.group(1)))
        pay = re.search(r"([\d,]+)円", _text(tri.group(2)))
        trifecta = (combo, int(pay.group(1).replace(",", "")) if pay else None)
    return {"order": order, "going": gm.group(1) if gm else "", "trifecta": trifecta}


# ---- db.netkeiba.com: 馬の全成績とレースの全出走馬 ----

PLACES = {"札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
          "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10"}


def race_class(name):
    """レース名からクラスを判定（G1/G2/G3/L/OP/3勝/2勝/1勝/未勝利/新馬）"""
    m = re.search(r"\((G|Jpn)(I{1,3})\)", name)
    if m:
        return "G" + str(len(m.group(2)))
    for key, val in (("(L)", "L"), ("(OP)", "OP"), ("3勝", "3勝"), ("1600万", "3勝"), ("2勝", "2勝"),
                     ("1000万", "2勝"), ("1勝", "1勝"), ("500万", "1勝"), ("未勝利", "未勝利"), ("新馬", "新馬")):
        if key in name:
            return val
    return "OP"  # 表記のない特別戦・海外など


def _time_sec(s):
    m = re.match(r"(\d+):(\d+\.\d)", s)
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def _num(s, cast=float):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


@dataclass
class Run:
    """馬の1走分の成績（db.netkeiba の成績表から）"""
    date: str  # YYYY/MM/DD
    place: str  # 中山 など（海外・地方は名前そのまま）
    race_id: str
    name: str
    cls: str
    field_size: int | None
    waku: int | None
    umaban: int | None
    odds: float | None
    popularity: int | None
    rank: int | None
    jockey: str
    weight: float | None
    surface: str
    distance: int
    going: str
    time: float | None
    diff: float | None  # 勝ち馬との差（勝った場合は2着との差をマイナスで）
    passage: list
    pace: tuple | None  # (前半3F, 後半3F) レース全体
    last3f: float | None
    body_weight: int | None
    other_id: str  # 勝ち馬（自分が1着なら2着馬）のID
    jockey_id: str = ""

    @property
    def jra(self):
        return self.place in PLACES

    @property
    def early(self):
        """最初のコーナーの位置取り（0=先頭 〜 1=最後方）"""
        if not self.passage or not self.field_size or self.field_size < 2:
            return None
        return (self.passage[0] - 1) / (self.field_size - 1)


def horse_history(horse_id, max_age_days=3):
    t = fetch(f"{DB}/horse/result/{horse_id}/", max_age_days=max_age_days)
    m = re.search(r"<table[^>]*db_h_race_results[^>]*>.*?</table>", t, re.S)
    runs = []
    if not m:
        return runs
    for row in re.findall(r"<tr.*?</tr>", m.group(0), re.S)[1:]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 32:
            continue
        g = lambda i: _text(tds[i])  # noqa: E731
        rid = re.search(r"/race/(\w+)/", tds[4])
        place = re.sub(r"\d", "", g(1))
        sd = re.match(r"(芝|ダ|障)(\d+)", g(14))
        pace = re.match(r"([\d.]+)-([\d.]+)", g(26))
        other = re.search(r"/horse/(\w+)/", tds[31])
        bw = re.match(r"(\d+)", g(28))
        runs.append(Run(
            date=g(0), place=place, race_id=rid.group(1) if rid else "", name=g(4), cls=race_class(g(4)),
            field_size=_num(g(6), int), waku=_num(g(7), int), umaban=_num(g(8), int), odds=_num(g(9)),
            popularity=_num(g(10), int), rank=_num(g(11), int), jockey=g(12), weight=_num(g(13)),
            surface=sd.group(1) if sd else "", distance=int(sd.group(2)) if sd else 0,
            going=g(16)[:1], time=_time_sec(g(18)), diff=_num(g(19)),
            passage=[int(x) for x in re.findall(r"\d+", g(25))],
            pace=(float(pace.group(1)), float(pace.group(2))) if pace else None,
            last3f=_num(g(27)), body_weight=int(bw.group(1)) if bw else None,
            other_id=other.group(1) if other else "",
            jockey_id=(re.search(r"/jockey/(?:result/recent/)?(\w+)", tds[12]) or [None, ""])[1],
        ))
    return runs


def _uncache(url):
    (CACHE / re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[1])[:200]).unlink(missing_ok=True)


def race_members_live(race_id):
    """当日・前日のレース（db.netkeiba にまだ載っていない）を速報の結果ページから。
    通過順は「コーナー通過順位」の最初のコーナーから作る。"""
    url = f"{RACE}/race/result.html?race_id={race_id}"
    t = fetch(url)
    rows = re.findall(r'<tr\s+class="(?:FirstDisplay )?HorseList"[^>]*>\s*<td class="Result_Num">.*?</tr>', t, re.S)
    if not rows:
        _uncache(url)  # まだ結果が出ていない
        return {"race_id": race_id, "surface": "", "distance": 0, "going": "", "place": "", "field_size": 0}, []
    rd = _text(re.search(r'RaceData01">(.*?)</div>', t, re.S).group(1))
    sd = re.search(r"(芝|ダ|障)(\d+)m", rd)
    gm = re.search(r"馬場:(良|稍|重|不)", rd)
    corner = {}
    ct = re.search(r'Corner_Num">(.*?)</table>', t, re.S)
    if ct:
        for cell in re.findall(r"<td>(.*?)</td>", ct.group(1), re.S):
            seq = [int(x) for x in re.findall(r"\d+", _text(cell))]
            if seq:
                corner = {num: i + 1 for i, num in enumerate(seq)}
                break
    runners = []
    for row in rows:
        rk = _text(re.search(r'class="Rank">(.*?)<', row).group(1))
        nums = re.findall(r'<td class="Num[^"]*">\s*<div>(\d+)</div>', row)
        hid = re.search(r"/horse/(\w+)", row)
        tm = re.search(r'RaceTime">(.*?)<', row)
        od = re.search(r'<td class="Odds Txt_R">\s*<span[^>]*>([\d.]+)</span>', row)
        um = int(nums[1]) if len(nums) > 1 else None
        runners.append({"rank": int(rk) if rk.isdigit() else None, "waku": int(nums[0]) if nums else None,
                        "umaban": um, "horse_id": hid.group(1) if hid else "", "name": "",
                        "time": _time_sec(tm.group(1)) if tm else None,
                        "passage": [corner[um]] if um in corner else [], "odds": _num(od.group(1)) if od else None})
    info = {"race_id": race_id, "surface": sd.group(1) if sd else "", "distance": int(sd.group(2)) if sd else 0,
            "going": gm.group(1) if gm else "", "field_size": len(runners),
            "place": next((k for k, v in PLACES.items() if race_id[4:6] == v), "")}
    return info, runners


def race_list(yyyymmdd, refresh=False, cached_only=False):
    """その日の全レース {race_id: (芝/ダ/障, 距離)}"""
    t = fetch(f"{RACE}/top/race_list_sub.html?kaisai_date={yyyymmdd}", refresh=refresh, cached_only=cached_only)
    out = {}
    for it in re.findall(r'<li class="RaceList_DataItem.*?</li>', t, re.S):
        rid = re.search(r"race_id=(\d{12})", it)
        sd = re.search(r"(芝|ダ|障)(\d+)m", _text(it))
        if rid and sd:
            out[rid.group(1)] = (sd.group(1), int(sd.group(2)))
    return out


def race_members(race_id, cached_only=False):
    """確定済みレースの全出走馬。db.netkeiba のレースページから。"""
    url = f"{DB}/race/{race_id}/"
    t = fetch(url, cached_only=cached_only)
    if "race_table_01" not in t:  # まだ行われていないレースはキャッシュしない
        _uncache(url)
    head = _text((re.search(r'racedata fc">(.*?)</dl>', t, re.S) or re.search(r"racedata.*?</p>", t, re.S)).group(0))
    sd = re.search(r"(芝|ダ|障)[右左直外内 ]*(\d+)m", head)
    gm = re.search(r"(?:芝|ダート) : (良|稍重|重|不良)", head)
    dm = re.search(r"(\d{4})年(\d{2})月(\d{2})日", t)
    title = _text(re.search(r"<title>(.*?)</title>", t, re.S).group(1)).split("｜")[0]
    info = {"race_id": race_id, "name": title, "cls": race_class(title),
            "surface": sd.group(1) if sd else "", "distance": int(sd.group(2)) if sd else 0,
            "outer": "外" in head[:40], "going": gm.group(1)[:1] if gm else "",
            "date": f"{dm.group(1)}/{dm.group(2)}/{dm.group(3)}" if dm else "",
            "place": next((k for k, v in PLACES.items() if race_id[4:6] == v), "")}
    runners = []
    m = re.search(r"<table[^>]*race_table_01[^>]*>.*?</table>", t, re.S)
    for row in re.findall(r"<tr.*?</tr>", m.group(0) if m else "", re.S)[1:]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 19:
            continue
        g = lambda i: _text(tds[i])  # noqa: E731
        hid = re.search(r"/horse/(\w+)/", tds[3])
        sa = re.match(r"(\D)(\d+)", g(4))
        runners.append({
            "rank": _num(g(0), int), "waku": _num(g(1), int), "umaban": _num(g(2), int),
            "horse_id": hid.group(1) if hid else "", "name": g(3),
            "sex": sa.group(1) if sa else "", "age": int(sa.group(2)) if sa else 0,
            "weight": _num(g(5)), "jockey": g(6), "time": _time_sec(g(7)),
            "passage": [int(x) for x in re.findall(r"\d+", g(14))], "last3f": _num(g(15)),
            "odds": _num(g(16)), "popularity": _num(g(17), int),
            "jockey_id": (re.search(r"/jockey/(?:result/recent/)?(\w+)", tds[6]) or [None, ""])[1],
            "trainer_id": (re.search(r"/trainer/(?:result/recent/)?(\w+)", tds[22]) or [None, ""])[1],
        })
    info["field_size"] = len(runners)
    tri = re.search(r"三連単</th>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", t, re.S)
    info["trifecta"] = None
    if tri:
        combo = tuple(int(x) for x in re.findall(r"\d+", _text(tri.group(1))))
        pay = re.search(r"[\d,]+", _text(tri.group(2)))
        if len(combo) == 3 and pay:
            info["trifecta"] = (combo, int(pay.group(0).replace(",", "")))
    return info, runners


def pedigree(horse_id, cached_only=False):
    """父・母・母父。{'sire_id','sire','dam','damsire_id','damsire'}"""
    t = fetch(f"{DB}/horse/ped/{horse_id}/", cached_only=cached_only)
    m = re.search(r"<table[^>]*blood_table[^>]*>.*?</table>", t, re.S)
    out = {"sire_id": "", "sire": "", "dam": "", "damsire_id": "", "damsire": ""}
    if not m:
        return out
    tops = re.findall(r'rowspan="16"[^>]*>.*?/horse/(\w+)/"[^>]*>(.*?)</a>', m.group(0), re.S)
    if tops:
        out["sire_id"], out["sire"] = tops[0][0], _text(tops[0][1])
    if len(tops) > 1:
        out["dam"] = _text(tops[1][1])
        dam_row = m.group(0).split(tops[1][0], 1)[1]
        ds = re.search(r'rowspan="8"[^>]*>.*?/horse/(\w+)/"[^>]*>(.*?)</a>', dam_row, re.S)
        if ds:
            out["damsire_id"], out["damsire"] = ds.group(1), _text(ds.group(2))
    return out
