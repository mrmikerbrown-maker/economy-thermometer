"""Temporary: report how far back each compass series goes, as fetched from GitHub Actions."""
import csv, io, json, sys, time
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (compatible; economy-thermometer/1.0)"
CFG = json.load(open("config.json"))
START = "2004-01-01"


def get(url):
    for i in range(2):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            err = e
            time.sleep(3)
    raise RuntimeError(err)


def fred(sid):
    rows = list(csv.reader(io.StringIO(get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (sid, START)))))[1:]
    return [(r[0][:7], float(r[1])) for r in rows if len(r) > 1 and r[1] not in ("", ".")]


def eurostat(ds, params):
    j = json.loads(get("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/%s?%s&sinceTimePeriod=2004-01" % (ds, params)))
    idx = j["dimension"]["time"]["category"]["index"]
    v = j.get("value", {})
    return sorted((p, float(v[str(i)])) for p, i in idx.items() if str(i) in v and v[str(i)] is not None)


def summary(name, s):
    if not s:
        return "%-34s EMPTY" % name
    return "%-34s %s to %s  (%d obs)" % (name, s[0][0], s[-1][0], len(s))


lines = []


def run(name, fn):
    try:
        lines.append(summary(name, fn()))
    except Exception as e:
        lines.append("%-34s FAIL %s" % (name, e))


areas = "+".join(c["bis"] for c in CFG["countries"])
try:
    text = get("https://stats.bis.org/api/v2/data/dataflow/BIS/WS_LONG_CPI/1.0/M.%s.628?startPeriod=2004-01&format=csv" % areas)
    cpi = {}
    for r in csv.DictReader(io.StringIO(text)):
        cpi.setdefault(r["REF_AREA"], []).append((r["TIME_PERIOD"][:7], r["OBS_VALUE"]))
    for c in CFG["countries"]:
        lines.append(summary("CPI index %s (BIS)" % c["code"], sorted(cpi.get(c["bis"], []))))
except Exception as e:
    lines.append("CPI BIS FAIL %s" % e)

run("USREC (NBER)", lambda: fred("USREC"))
run("GDP US GDPC1", lambda: fred("GDPC1"))
for c in CFG["countries"]:
    cc = c["code"]
    j = c["jobs"]
    if j["src"] == "fred":
        run("Unemp %s %s" % (cc, j["id"]), lambda: fred(j["id"]))
    else:
        for geo in j["geo"]:
            run("Unemp %s eurostat %s" % (cc, geo), lambda: eurostat("une_rt_m", "geo=%s&s_adj=SA&age=TOTAL&sex=T&unit=PC_ACT" % geo))
    if cc == "EA":
        for geo in c["growth"]["geo"]:
            run("GDP yoy EA eurostat %s" % geo, lambda: eurostat("namq_10_gdp", "geo=%s&s_adj=SCA&unit=CLV_PCH_SM&na_item=B1GQ" % geo))
            run("GDP qoq EA eurostat %s" % geo, lambda: eurostat("namq_10_gdp", "geo=%s&s_adj=SCA&unit=CLV_PCH_PRE&na_item=B1GQ" % geo))
    elif cc != "US":
        run("GDP qoq %s 657S" % cc, lambda: fred("NAEXKP01%sQ657S" % cc))
        run("GDP yoy %s 659S" % cc, lambda: fred("NAEXKP01%sQ659S" % cc))
        run("GDP level %s 652S" % cc, lambda: fred("NAEXKP01%sQ652S" % cc))

print("\n".join(lines))
open("_check/report.txt", "w").write("\n".join(lines) + "\n")
