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
SCHEMA = 2  # bump when the meaning of a stored figure changes, so old values are never reused
DEMO = "--demo" in sys.argv
TODAY = dt.date.today()
HIST_YEARS = CFG.get("history_years", 10)
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
    start = "%d-01-01" % (TODAY.year - HIST_YEARS - 2)
    text = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series_id, start))
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        try:
            out.append((date_to_key(r[0], freq), float(r[1])))
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
            out.setdefault(r["REF_AREA"], []).append((r["TIME_PERIOD"][:7], float(r["OBS_VALUE"])))
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
    out = [(p, float(vals[str(i)])) for p, i in idx.items() if str(i) in vals]
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


def activity_axis(vals, v, qs):
    """Axis for the strip: ignore extreme one-offs (e.g. 2020) so the typical range stays readable."""
    p5, p95 = pct(vals, 0.05), pct(vals, 0.95)
    pad = (p95 - p5) * 0.2 or 1.0
    return min(p5 - pad, v - pad * 0.3), max(p95 + pad, v + pad * 0.3)


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
    lo, hi = activity_axis(vals, v, None)
    pos = lambda x: (hi - x) / (hi - lo) * 100
    return {"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
            "range_lo": round(q25, 2), "range_hi": round(q75, 2),
            "pos": round(pos(v), 1), "zone_l": round(pos(q75), 1), "zone_r": round(pos(q25), 1)}


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
    lo, hi = activity_axis(vals, v, None)
    pos = lambda x: (x - lo) / (hi - lo) * 100
    return {"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
            "range_lo": round(q25, 2), "range_hi": round(q75, 2),
            "pos": round(pos(v), 1), "zone_l": round(pos(q25), 1), "zone_r": round(pos(q75), 1)}


def prices_block(series, tgt):
    key, v = series[-1]
    st = prices_state(v, tgt["lo"], tgt["hi"])
    lo, hi = min(-1.0, math.floor(v - 0.5)), max(7.0, math.ceil(v + 0.5))
    pos = lambda x: (x - lo) / (hi - lo) * 100
    return {"value": round(v, 2), "asof": key, "asof_label": label(key), "state": st,
            "target": tgt["label"], "range_lo": tgt["lo"], "range_hi": tgt["hi"],
            "pos": round(pos(v), 1), "zone_l": round(pos(tgt["lo"]), 1), "zone_r": round(pos(tgt["hi"]), 1)}


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
    start_cpi = "%d-01" % (TODAY.year - 3)
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

    countries = []
    for c in CFG["countries"]:
        code, old = c["code"], prev.get(c["code"], {})
        new = {"code": code, "name": c["name"], "short": c["short"], "bank": c["bank"], "bank_short": c["bank_short"]}

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
                        s = eurostat("une_rt_m", "geo=%s&s_adj=SA&age=TOTAL&sex=T&unit=PC_ACT" % geo, "%d-01" % (TODAY.year - HIST_YEARS - 1))
                        break
                    except Exception as e:
                        err = e
                if s is None:
                    raise RuntimeError(err)
            return jobs_block(s, j["freq"])

        def growth():
            g = c["growth"]
            if g["src"] == "fred_level":
                s = level_to_qoq(fred_csv(g["id"], "Q"))
            elif g["src"] == "fred_qoq":
                s = fred_csv(g["id"], "Q")
            else:
                s, err = None, None
                for geo in g["geo"]:
                    try:
                        s = eurostat("namq_10_gdp", "geo=%s&s_adj=SCA&unit=CLV_PCH_PRE&na_item=B1GQ" % geo, "%d-Q1" % (TODAY.year - HIST_YEARS - 1))
                        break
                    except Exception as e:
                        err = e
                if s is None:
                    raise RuntimeError(err)
            if len(s) < 8:
                raise RuntimeError("not enough quarterly history")
            return growth_block(s, "Q")

        def rate():
            if c["bis"] not in rate_all:
                raise RuntimeError("no BIS policy-rate data for %s" % c["bis"])
            return rate_block(rate_all[c["bis"]], c.get("rate_adjust", 0.0))

        attempt("prices", prices)
        attempt("jobs", jobs)
        attempt("growth", growth)
        attempt("rate", rate)

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n".join(log))
    print("\nWrote %s" % path)
    if problems:
        print("::error::No data at all for: %s" % ", ".join(problems))
        sys.exit(1)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
