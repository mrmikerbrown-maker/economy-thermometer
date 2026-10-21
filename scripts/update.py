#!/usr/bin/env python3
"""
Economy thermometer: daily data job.

Pulls inflation, unemployment, growth and policy rates for the G10 economies from free public
sources, works out where each one sits (cold / cool / normal / warm / hot) and writes data.json.
The page (index.html) only draws what is in data.json.

Sources
  Inflation (price index, we compute year-on-year) and policy rates: Bank for International Settlements
  Unemployment and GDP growth: FRED (St. Louis Fed) CSV downloads; euro area from Eurostat
Standard library only. Run with --demo to test everything on made-up data (no internet needed).
"""
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
OUT = os.path.join(ROOT, "data.json")
UA = "Mozilla/5.0 (compatible; economy-thermometer/1.0)"
SCHEMA = 3  # bump when the meaning of a stored figure changes, so old values are never reused
DEMO = "--demo" in sys.argv
TODAY = dt.date.today()
HIST_YEARS = CFG.get("history_years", 10)
COMPASS_YEARS = CFG.get("compass_years", 20)
FETCH_YEARS = max(HIST_YEARS, COMPASS_YEARS) + 2  # extra years so the first year-on-year figures exist
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SCORE = {"cold": -2, "cool": -1, "normal": 0, "warm": 1, "hot": 2}

# ----------------------------------------------------------------------------- fetching


def http_get(url):
    last = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("could not fetch %s (%s)" % (url, last))


def get(url):
    if DEMO:
        import demo_data  # noqa: made-up responses in the real formats, for testing
        return demo_data.respond(url, TODAY)
    return http_get(url)


# ----------------------------------------------------------------------------- period helpers


def mkey(y, m):
    return "%04d-%02d" % (y, m)


def qkey(y, q):
    return "%04d-Q%d" % (y, q)


def month_add(key, n):
    y, m = int(key[:4]), int(key[5:7])
    t = y * 12 + (m - 1) + n
    return mkey(t // 12, t % 12 + 1)


def label(key):
    if "Q" in key:
        return "Q%s %s" % (key[-1], key[:4])
    return "%s %s" % (MONTHS[int(key[5:7]) - 1], key[:4])


def months_old(key):
    y, m = int(key[:4]), (int(key[5:7]) if "Q" not in key else int(key[-1]) * 3)
    return (TODAY.year * 12 + TODAY.month) - (y * 12 + m)


def date_to_key(d, freq):
    """'2026-04-01' -> '2026-04' (monthly) or '2026-Q2' (quarterly)."""
    y, m = int(d[:4]), int(d[5:7])
    return mkey(y, m) if freq == "M" else qkey(y, (m - 1) // 3 + 1)


# ----------------------------------------------------------------------------- source readers


def fred_csv(series_id, freq):
    start = "%d-01-01" % (TODAY.year - FETCH_YEARS)
    text = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series_id, start))
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        try:
            value = float(r[1])
            if math.isfinite(value):
                out.append((date_to_key(r[0], freq), value))
        except ValueError:
            continue
    if not out:
        raise RuntimeError("FRED %s returned no data" % series_id)
    return sorted(out)


def bis_csv(flow, key, start):
    text = get("https://stats.bis.org/api/v2/data/dataflow/BIS/%s/1.0/%s?startPeriod=%s&format=csv" % (flow, key, start))
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        try:
            value = float(r["OBS_VALUE"])
            if math.isfinite(value):
                out.setdefault(r["REF_AREA"], []).append((r["TIME_PERIOD"][:7], value))
        except (KeyError, ValueError):
            continue
    if not out:
        raise RuntimeError("BIS %s returned no data" % flow)
    return {k: sorted(v) for k, v in out.items()}


def eurostat(dataset, params, since):
    text = get("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/%s?%s&sinceTimePeriod=%s" % (dataset, params, since))
    j = json.loads(text)
    idx = j["dimension"]["time"]["category"]["index"]
    if isinstance(idx, list):
        idx = {p: i for i, p in enumerate(idx)}
    vals = j.get("value", {})
    if isinstance(vals, list):
        vals = {str(i): v for i, v in enumerate(vals) if v is not None}
    out = [(p, float(vals[str(i)])) for p, i in idx.items() if str(i) in vals and vals[str(i)] is not None and math.isfinite(float(vals[str(i)]))]
    if not out:
        raise RuntimeError("Eurostat %s returned no observations" % dataset)
    return sorted(out)


# ----------------------------------------------------------------------------- transformations


def cpi_yoy(index_series):
    d = dict(index_series)
    out = []
    for k, v in sorted(d.items()):
        prev = d.get(month_add(k, -12))
        if prev:
            out.append((k, (v / prev - 1) * 100))
    return out


def level_to_qoq(level):
    """Real GDP level -> growth on the previous quarter, in percent (not annualised)."""
    d = dict(level)
    out = []
    for k in sorted(d):
        y, q = int(k[:4]), int(k[-1])
        pk = qkey(y, q - 1) if q > 1 else qkey(y - 1, 4)
        prev = d.get(pk)
        if prev:
            out.append((k, (d[k] / prev - 1) * 100))
    return out


def q_add(key, n):
    t = int(key[:4]) * 4 + (int(key[-1]) - 1) + n
    return qkey(t // 4, t % 4 + 1)


def trail_of(series, freq, back):
    """Earlier readings, oldest first, for the faint dots: the `back` periods before the latest one."""
    d = dict(series)
    key = series[-1][0]
    step = month_add if freq == "M" else q_add
    out = [(step(key, -i), d[step(key, -i)]) for i in range(back, 0, -1) if step(key, -i) in d]
    ago = None
    if out and out[0][0] == step(key, -back):
        ago = {"label": label(out[0][0]), "value": round(out[0][1], 2) + 0.0}
    return out, ago


def trail_json(trail, ago, pos):
    return {"trail": [{"k": label(k), "v": round(x, 2), "pos": round(pos(x), 1)} for k, x in trail], "ago": ago}


def pct(sorted_vals, p):
    if not sorted_vals:
        raise ValueError("empty")
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return sorted_vals[f] if f == c else sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ----------------------------------------------------------------------------- the boundaries


def prices_state(v, lo, hi):
    r = round(v, 1)
    if lo <= r <= hi:
        return "normal"
    if r > hi:
        return "warm" if r <= hi + 1 else "hot"
    return "cool" if r >= lo - 1 else "cold"


def window(series, freq):
    n = HIST_YEARS * (12 if freq == "M" else 4)
    return [v for _, v in series[-n:]]


def spread_guard(q25, q75, med, floor):
    """If a series barely moves, widen the typical range so tiny wobbles don't flip the label."""
    if q75 - q25 < floor:
        return med - floor / 2, med + floor / 2
    return q25, q75


def activity_axis(vals, v, extra=()):
    """Axis for the strip: ignore extreme one-offs (e.g. 2020) so the typical range stays readable."""
    p5, p95 = pct(vals, 0.05), pct(vals, 0.95)
    pad = (p95 - p5) * 0.2 or 1.0
    every = [v] + list(extra)
    return min(p5 - pad, min(every) - pad * 0.3), max(p95 + pad, max(every) + pad * 0.3)


def jobs_block(series, freq):
    """Unemployment: LOW is hot (plentiful jobs). Strip runs scarce (left) to plentiful (right)."""
    key, v = series[-1]
    vals = sorted(window(series, freq))
    q10, q25, med, q75, q90 = [pct(vals, p) for p in (0.10, 0.25, 0.5, 0.75, 0.90)]
    q25, q75 = spread_guard(q25, q75, med, 0.4)
    if v <= q10:
        st = "hot"
    elif v < q25:
        st = "warm"
    elif v <= q75:
        st = "normal"
    elif v <= q90:
        st = "cool"
    else:
        st = "cold"
    tr, ago = trail_of(series, freq, 12 if freq == "M" else 4)
    lo, hi = activity_axis(vals, v, [x for _, x in tr])
    pos = lambda x: (hi - x) / (hi - lo) * 100
    return dict({"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
                 "range_lo": round(q25, 2), "range_hi": round(q75, 2),
                 "pos": round(pos(v), 1), "zone_l": round(pos(q75), 1), "zone_r": round(pos(q25), 1)},
                **trail_json(tr, ago, pos))


def growth_block(series, freq):
    key, v = series[-1]
    vals = sorted(window(series, freq))
    q10, q25, med, q75, q90 = [pct(vals, p) for p in (0.10, 0.25, 0.5, 0.75, 0.90)]
    q25, q75 = spread_guard(q25, q75, med, 0.15)
    if v >= q90:
        st = "hot"
    elif v > q75:
        st = "warm"
    elif v >= q25:
        st = "normal"
    elif v >= q10:
        st = "cool"
    else:
        st = "cold"
    tr, ago = trail_of(series, "Q", 4)
    lo, hi = activity_axis(vals, v, [x for _, x in tr])
    pos = lambda x: (x - lo) / (hi - lo) * 100
    return dict({"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
                 "range_lo": round(q25, 2), "range_hi": round(q75, 2),
                 "pos": round(pos(v), 1), "zone_l": round(pos(q25), 1), "zone_r": round(pos(q75), 1)},
                **trail_json(tr, ago, pos))


def prices_block(series, tgt):
    key, v = series[-1]
    st = prices_state(v, tgt["lo"], tgt["hi"])
    tr, ago = trail_of(series, "M", 12)
    every = [v] + [x for _, x in tr]
    lo, hi = min(-1.0, math.floor(min(every) - 0.5)), max(7.0, math.ceil(max(every) + 0.5))
    pos = lambda x: (x - lo) / (hi - lo) * 100
    return dict({"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
                 "target": tgt["label"], "range_lo": tgt["lo"], "range_hi": tgt["hi"],
                 "pos": round(pos(v), 1), "zone_l": round(pos(tgt["lo"]), 1), "zone_r": round(pos(tgt["hi"]), 1)},
                **trail_json(tr, ago, pos))


def rate_block(series, adjust=0.0):
    """Policy rate. `adjust` shifts every value (the US feed is the middle of the Fed's range; +0.125 gives the upper bound)."""
    series = [(k, v + adjust) for k, v in series]
    key, v = series[-1]
    change = None
    for i in range(len(series) - 1, 0, -1):
        bp = round((series[i][1] - series[i - 1][1]) * 100)
        if bp != 0:
            change = {"asof": series[i][0], "asof_label": label(series[i][0]), "bp": bp}
            break
    return {"value": round(v, 3), "asof": key, "asof_label": label(key), "last_change": change,
            "since_label": label(series[0][0])}


# ----------------------------------------------------------------------------- the compass ("Over time" view)


def m_to_q(key):
    return qkey(int(key[:4]), (int(key[5:7]) - 1) // 3 + 1)


def quarterly_mean(monthly):
    """Monthly readings -> the average of each complete quarter."""
    acc = {}
    for k, v in monthly:
        acc.setdefault(m_to_q(k), []).append(v)
    return sorted((q, sum(v) / len(v)) for q, v in acc.items() if len(v) == 3)


def yoy(series, step):
    """Year-on-year % change of a level series (monthly: step 12, quarterly: step 4)."""
    d = dict(series)
    add = month_add if step == 12 else q_add
    return [(k, (v / d[add(k, -step)] - 1) * 100) for k, v in sorted(d.items()) if d.get(add(k, -step))]


def qoq_to_yoy(qoq):
    """Growth on the previous quarter -> growth on the same quarter a year earlier, by compounding four quarters."""
    d = dict(qoq)
    out = []
    for k in sorted(d):
        ks = [q_add(k, -i) for i in range(4)]
        if all(x in d for x in ks):
            f = 1.0
            for x in ks:
                f *= 1 + d[x] / 100
            out.append((k, (f - 1) * 100))
    return out


def recession_quarters(qoq):
    """Two or more quarters in a row of falling GDP (the usual rule of thumb)."""
    keys = [k for k, v in qoq if v < 0]
    neg = set(keys)
    return {k for k in keys if q_add(k, -1) in neg or q_add(k, 1) in neg}


def cycles(keys, rec, step, min_len):
    """Split the timeline into business cycles. Each cycle starts at the end of a recession (the trough)
    and runs to the end of the next one. Cycles shorter than min_len periods are merged into the one before."""
    troughs = [i for i, k in enumerate(keys) if k in rec and (i + 1 == len(keys) or keys[i + 1] not in rec)]
    bounds = [0] + [t for t in troughs if 0 < t < len(keys) - 1] + [len(keys) - 1]
    segs = [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]
    merged = []
    for a, b in segs:
        if merged and (b - a < min_len or merged[-1][1] - merged[-1][0] < min_len):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    if len(merged) > 1 and merged[-1][1] - merged[-1][0] < min_len:
        merged[-2][1] = merged.pop()[1]
    return merged


def compass_view(xs, ys, rec, freq, x_center):
    """Pair two series period by period and split them into cycles."""
    yd = dict(ys)
    first = q_add(m_to_q(TODAY.strftime("%Y-%m")), -4 * COMPASS_YEARS) if freq == "Q" else month_add(TODAY.strftime("%Y-%m"), -12 * COMPASS_YEARS)
    pts = [(k, x, yd[k]) for k, x in xs if k in yd and k >= first]
    if len(pts) < (12 if freq == "Q" else 36):
        raise RuntimeError("not enough history for the compass")
    keys = [k for k, _, _ in pts]
    segs = cycles(keys, rec, 1, 8 if freq == "Q" else 24)
    cyc = [{"from": a, "to": b, "label": "%s–%s" % (keys[a][:4], "now" if b == len(keys) - 1 else keys[b][2:4])} for a, b in segs]
    return {"freq": freq, "x_center": round(x_center, 2),
            "pts": [[label(k), round(x, 2), round(y, 2), 1 if k in rec else 0] for k, x, y in pts], "cycles": cyc}


def median(vals):
    return pct(sorted(vals), 0.5)


# ----------------------------------------------------------------------------- the run


def load_previous():
    try:
        j = json.load(open(OUT, encoding="utf-8"))
        if j.get("schema") != SCHEMA:
            return {}
        return {c["code"]: c for c in j.get("countries", [])}
    except (OSError, ValueError, KeyError):
        return {}


def stale(block, freq):
    return months_old(block["asof"]) > (6 if freq == "M" else 9)


def main():
    prev = load_previous()
    log, problems = [], []
    areas = "+".join(c["bis"] for c in CFG["countries"])
    start_cpi = "%d-01" % (TODAY.year - FETCH_YEARS)
    try:
        cpi_all = bis_csv("WS_LONG_CPI", "M.%s.628" % areas, start_cpi)
        log.append("OK   BIS inflation index")
    except Exception as e:
        cpi_all = {}
        log.append("FAIL BIS inflation index: %s" % e)
    try:
        rate_all = bis_csv("WS_CBPOL", "M.%s" % areas, "%d-01" % (TODAY.year - 5))
        log.append("OK   BIS policy rates")
    except Exception as e:
        rate_all = {}
        log.append("FAIL BIS policy rates: %s" % e)
    try:
        usrec = {k for k, v in fred_csv("USREC", "M") if v >= 1}
        log.append("OK   NBER US recession dates")
    except Exception as e:
        usrec = None
        log.append("FAIL NBER US recession dates: %s (falling-GDP rule used instead)" % e)

    countries = []
    for c in CFG["countries"]:
        code, old = c["code"], prev.get(c["code"], {})
        new = {"code": code, "name": c["name"], "short": c["short"], "bank": c["bank"], "bank_short": c["bank_short"]}
        raw = {}  # full-length series fetched for the thermometer, reused by the compass

        def attempt(name, fn):
            try:
                blk = fn()
                blk["kept"] = False
                new[name] = blk
                log.append("OK   %s %s (%s)" % (code, name, blk["asof_label"]))
            except Exception as e:
                if name in old:
                    new[name] = dict(old[name], kept=True)
                    log.append("KEPT %s %s: new fetch failed (%s); showing previous value" % (code, name, e))
                else:
                    log.append("FAIL %s %s: %s" % (code, name, e))
                    problems.append("%s %s" % (code, name))

        def prices():
            if c["bis"] not in cpi_all:
                raise RuntimeError("no BIS inflation data for %s" % c["bis"])
            yoy = cpi_yoy(cpi_all[c["bis"]])
            if not yoy:
                raise RuntimeError("not enough history to compute year-on-year inflation")
            return prices_block(yoy, c["target"])

        def jobs():
            j = c["jobs"]
            if j["src"] == "fred":
                s = fred_csv(j["id"], j["freq"])
            else:
                s, err = None, None
                for geo in j["geo"]:
                    try:
                        s = eurostat("une_rt_m", "geo=%s&s_adj=SA&age=TOTAL&sex=T&unit=PC_ACT" % geo, "%d-01" % (TODAY.year - FETCH_YEARS))
                        break
                    except Exception as e:
                        err = e
                if s is None:
                    raise RuntimeError(err)
            raw["jobs"] = s
            return jobs_block(s, j["freq"])

        def growth():
            g = c["growth"]
            if g["src"] == "fred_level":
                raw["gdp_level"] = fred_csv(g["id"], "Q")
                s = level_to_qoq(raw["gdp_level"])
            elif g["src"] == "fred_qoq":
                s = fred_csv(g["id"], "Q")
            else:
                s, err = None, None
                for geo in g["geo"]:
                    try:
                        s = eurostat("namq_10_gdp", "geo=%s&s_adj=SCA&unit=CLV_PCH_PRE&na_item=B1GQ" % geo, "%d-Q1" % (TODAY.year - FETCH_YEARS))
                        break
                    except Exception as e:
                        err = e
                if s is None:
                    raise RuntimeError(err)
            if len(s) < 8:
                raise RuntimeError("not enough quarterly history")
            raw["gdp_qoq"] = s
            return growth_block(s, "Q")

        def rate():
            if c["bis"] not in rate_all:
                raise RuntimeError("no BIS policy-rate data for %s" % c["bis"])
            return rate_block(rate_all[c["bis"]], c.get("rate_adjust", 0.0))

        def compass():
            if c["bis"] not in cpi_all or "jobs" not in raw or "gdp_qoq" not in raw:
                raise RuntimeError("needs this run's inflation, unemployment and growth data")
            index = cpi_all[c["bis"]]
            infl_q = yoy(quarterly_mean(index), 4)  # quarterly average of the index, then year on year
            gdp_yoy = yoy(raw["gdp_level"], 4) if "gdp_level" in raw else qoq_to_yoy(raw["gdp_qoq"])
            if usrec is not None and code == "US":
                rec_q = {m_to_q(k) for k in usrec}
                rec_m = set(usrec)
            else:
                rec_q = recession_quarters(raw["gdp_qoq"])
                rec_m = {month_add(mkey(int(q[:4]), int(q[-1]) * 3), -i) for q in rec_q for i in range(3)}
            tail = [v for _, v in gdp_yoy[-HIST_YEARS * 4:]]
            out = {"asof_label": label(gdp_yoy[-1][0]), "infl_center": round((c["target"]["lo"] + c["target"]["hi"]) / 2, 2),
                   "target": c["target"]["label"], "rec_rule": "nber" if code == "US" and usrec is not None else "gdp",
                   "growth": compass_view(gdp_yoy, infl_q, rec_q, "Q", median(tail))}
            jobs_m = raw["jobs"] if c["jobs"]["freq"] == "M" else None
            if jobs_m and c.get("cpi_freq", "M") == "M":
                un, infl, rec, fq = jobs_m, cpi_yoy(index), rec_m, "M"
            else:
                un = quarterly_mean(jobs_m) if jobs_m else raw["jobs"]
                un, infl, rec, fq = un, infl_q, rec_q, "Q"
            n = HIST_YEARS * (12 if fq == "M" else 4)
            out["unemp"] = compass_view(un, infl, rec, fq, median([v for _, v in un[-n:]]))
            return out

        attempt("prices", prices)
        attempt("jobs", jobs)
        attempt("growth", growth)
        attempt("rate", rate)
        attempt("compass", compass)

        for name, freq in (("prices", "M"), ("jobs", c["jobs"]["freq"]), ("growth", "Q")):
            if name in new:
                new[name]["stale"] = stale(new[name], freq)
        if all(k in new for k in ("prices", "jobs", "growth")):
            sc = [SCORE[new[k]["state"]] for k in ("prices", "jobs", "growth")]
            avg = sum(sc) / 3.0
            new["score"] = round(avg, 2)
            new["verdict"] = ("hot" if avg >= 1.25 else "warm" if avg >= 0.4 else "normal" if avg > -0.4
                              else "cool" if avg > -1.25 else "cold")
        countries.append(new)

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "demo": DEMO, "schema": SCHEMA, "history_years": HIST_YEARS, "countries": countries}
    path = OUT if not DEMO else os.path.join(ROOT, "data.demo.json")
    # Validate before touching the last usable file. JSON forbids NaN and infinity.
    payload = json.dumps(out, ensure_ascii=False, indent=1, allow_nan=False)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(temporary, path)
    print("\n".join(log))
    print("\nWrote %s" % path)
    if problems:
        print("::error::No data at all for: %s" % ", ".join(problems))
        sys.exit(1)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
