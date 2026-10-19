"""
Made-up responses in the same formats as the real sources. Used only by `update.py --demo`
to test the job and the page without internet. Nothing here is real data.
"""
import hashlib
import json
import os
import random
import re


def rng(name):
    return random.Random(int(hashlib.md5(name.encode()).hexdigest()[:8], 16))


def months_between(start_y, start_m, end_y, end_m):
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def lag(today, months):
    t = today.year * 12 + today.month - 1 - months
    return t // 12, t % 12 + 1


def respond(url, today):
    for bad in [s for s in os.environ.get("DEMO_FAIL", "").split(",") if s]:
        if bad in url:
            raise RuntimeError("demo failure for %s" % bad)
    if "fredgraph.csv" in url:
        return fred(url, today)
    if "stats.bis.org" in url and "WS_LONG_CPI" in url:
        return bis_cpi(url, today)
    if "stats.bis.org" in url and "WS_CBPOL" in url:
        return bis_rates(url, today)
    if "une_rt_m" in url:
        return eurostat_une(url, today)
    if "namq_10_gdp" in url:
        return eurostat_gdp(url, today)
    raise RuntimeError("demo: unknown url " + url)


def fred(url, today):
    sid = re.search(r"id=([A-Z0-9]+)", url).group(1)
    r = rng(sid)
    start_y = int(re.search(r"cosd=(\d{4})", url).group(1))
    rows = ["observation_date,%s" % sid]
    if sid == "GDPC1":
        lvl, (ey, em) = 100.0, lag(today, 4)
        for y, m in months_between(start_y, 1, ey, em):
            if m in (1, 4, 7, 10):
                g = r.gauss(0.5, 0.3) if not (y == 2020 and m == 4) else -8.0
                lvl *= 1 + g / 100
                rows.append("%d-%02d-01,%.3f" % (y, m, lvl))
    elif "Q657S" in sid:
        (ey, em) = lag(today, 4)
        for y, m in months_between(start_y, 1, ey, em):
            if m in (1, 4, 7, 10):
                g = r.gauss(0.5, 0.35) + (0.5 if r.random() < 0.1 else 0)
                if y == 2020 and m == 4:
                    g = -7.0
                rows.append("%d-%02d-01,%.5f" % (y, m, g))
    else:
        base = 3 + r.random() * 5
        monthly = sid == "UNRATE" or sid.endswith("M156S")
        (ey, em) = lag(today, 2 if monthly else 4)
        u = base
        for y, m in months_between(start_y, 1, ey, em):
            if not monthly and m not in (1, 4, 7, 10):
                continue
            u += r.gauss(0, 0.08) - (u - base) * 0.03
            val = u + (6 if (y, m) == (2020, 4) else 0)
            rows.append("%d-%02d-01,%.2f" % (y, m, val))
        if r.random() < 0.3:
            rows.append("%d-%02d-01,." % (ey, em))
    return "\n".join(rows)


def bis_cpi(url, today):
    key = re.search(r"/M\.([^/]+)\.628", url).group(1)
    start = re.search(r"startPeriod=(\d{4})-(\d{2})", url)
    sy, sm = int(start.group(1)), int(start.group(2))
    out = ["FREQ,REF_AREA,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE,OBS_STATUS"]
    for area in key.split("+"):
        r = rng("cpi" + area)
        drift = r.choice([0.05, 0.1, 0.17, 0.25, 0.32])
        lvl, ey, em = 100.0, *lag(today, 1 if area not in ("AU", "NZ") else 3)
        last = lvl
        for y, m in months_between(sy, sm, ey, em):
            if area in ("AU", "NZ") and m in (1, 4, 7, 10):
                last = lvl
            lvl *= 1 + (drift + r.gauss(0, 0.15)) / 100
            v = lvl if area not in ("AU", "NZ") else last
            out.append("M,%s,628,%d-%02d,%.6f,A" % (area, y, m, v))
    return "\n".join(out)


def bis_rates(url, today):
    key = re.search(r"/M\.([^?]+)\?", url).group(1)
    start = re.search(r"startPeriod=(\d{4})-(\d{2})", url)
    sy, sm = int(start.group(1)), int(start.group(2))
    out = ["FREQ,REF_AREA,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE,OBS_STATUS"]
    for area in key.split("+"):
        r = rng("rate" + area)
        rate = r.choice([0.0, 0.5, 1.0, 2.0, 3.0])
        ey, em = lag(today, 1)
        for y, m in months_between(sy, sm, ey, em):
            if r.random() < 0.07:
                rate = max(-0.5, rate + r.choice([-0.5, -0.25, 0.25, 0.5]))
            out.append("M,%s,368,%d-%02d,%.3f,A" % (area, y, m, rate))
    return "\n".join(out)


def jsonstat(periods, vals):
    return json.dumps({"dimension": {"time": {"category": {"index": {p: i for i, p in enumerate(periods)}}}},
                       "value": {str(i): v for i, v in enumerate(vals) if v is not None}})


def eurostat_une(url, today):
    geo = re.search(r"geo=([A-Z0-9]+)", url).group(1)
    ey, em = lag(today, 2)
    ps, vs = [], []
    r = rng("eaune")
    u = 8.0
    for y, m in months_between(today.year - 11, 1, ey, em):
        u += r.gauss(0, 0.06) - (u - 7) * 0.02
        ps.append("%d-%02d" % (y, m))
        vs.append(round(u, 1) if geo != "EA21" else None)  # EA21 empty on purpose: tests the fallback
    return jsonstat(ps, vs)


def eurostat_gdp(url, today):
    ey, em = lag(today, 4)
    ps, vs = [], []
    r = rng("eagdp")
    for y, m in months_between(today.year - 11, 1, ey, em):
        if m in (1, 4, 7, 10):
            ps.append("%d-Q%d" % (y, (m - 1) // 3 + 1))
            vs.append(round(r.gauss(0.35, 0.3), 1))
    return jsonstat(ps, vs)
