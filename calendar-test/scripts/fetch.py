#!/usr/bin/env python3
"""Fetch this week's economic calendar into data/calendar.json.

Runs on GitHub Actions, no API key. Main source: TradingView's public calendar
feed (actual, consensus, previous). Backup: the ForexFactory weekly feed
(consensus and previous only). To switch to another provider later (e.g.
Newsquawk), write one more fetch_xxx() that returns the same event dicts and
put it first in SOURCES.

Every released number is also kept in data/history.csv, so Data Surprises can
read actual-vs-consensus over time.
"""
import csv, json, os, re, sys, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calendar.json")
HIST = os.path.join(ROOT, "data", "history.csv")
COUNTRIES = ["US", "EU", "DE", "GB", "JP", "CN", "CA", "AU", "NZ", "CH", "NO", "SE"]
UA = "newsquawk-calendar-bot (github actions)"


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def week_window(now):
    """Monday 00:00 to next Monday 00:00 UTC. From Saturday on, show the coming week."""
    d = now.date()
    if d.weekday() >= 5:
        d += timedelta(days=7 - d.weekday())
    mon = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=7)


def short_period(p):
    if not p:
        return ""
    return re.sub(r"\.?\s*\d{4}$", "", p).replace(".", "").strip()


def is_rate(title, unit):
    return unit == "%" and re.search(r"interest rate|rate decision|cash rate|bank rate|policy rate|refinancing", title, re.I) is not None


def fetch_tradingview(start, end):
    url = ("https://economic-calendar.tradingview.com/events?from=%s&to=%s&countries=%s&minImportance=0"
           % (start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), end.strftime("%Y-%m-%dT%H:%M:%S.000Z"), ",".join(COUNTRIES)))
    js = get(url, {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"})
    if js.get("status") != "ok":
        raise RuntimeError("TradingView status %r" % js.get("status"))
    out = []
    for e in js.get("result", []):
        imp = e.get("importance", -1)
        if imp is None or imp < 0:
            continue
        unit = e.get("unit") or ""
        title = (e.get("title") or e.get("indicator") or "").strip()
        out.append({
            "id": "tv" + str(e.get("id")),
            "t": e["date"][:19] + "Z",
            "c": e.get("country"),
            "name": title,
            "period": short_period(e.get("period")),
            "actual": e.get("actual"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "unit": unit,
            "scale": e.get("scale") or "",
            "imp": 2 if imp >= 1 else 1,
            "rate": is_rate(title, unit),
        })
    return out


FF_CUR = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CNY": "CN", "CAD": "CA", "AUD": "AU",
          "NZD": "NZ", "CHF": "CH", "NOK": "NO", "SEK": "SE"}


def ff_num(s):
    """'0.4%' -> (0.4, '%', ''), '65K' -> (65, '', 'K'), '' -> (None, '', '')."""
    s = (s or "").strip()
    m = re.match(r"^(-?[\d.]+)\s*([%KMBT]?)", s)
    if not m:
        return None, "", ""
    v, suf = float(m.group(1)), m.group(2)
    return v, ("%" if suf == "%" else ""), ("" if suf in ("", "%") else suf)


def fetch_forexfactory(start, end):
    js = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    out = []
    for e in js:
        imp = {"High": 2, "Medium": 1}.get(e.get("impact"))
        c = FF_CUR.get(e.get("country"))
        if not imp or not c:
            continue
        t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        if not (start <= t < end):
            continue
        f, u1, s1 = ff_num(e.get("forecast"))
        p, u2, s2 = ff_num(e.get("previous"))
        unit, scale = u1 or u2, s1 or s2
        title = e.get("title", "")
        out.append({"id": "ff" + t.strftime("%Y%m%d%H%M") + c + re.sub(r"\W", "", title)[:20],
                    "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": c, "name": title, "period": "",
                    "actual": None, "forecast": f, "previous": p, "unit": unit, "scale": scale,
                    "imp": imp, "rate": is_rate(title, unit)})
    return out


SOURCES = [("TradingView", fetch_tradingview), ("ForexFactory", fetch_forexfactory)]


def save_history(events):
    seen, rows = set(), []
    if os.path.exists(HIST):
        with open(HIST, newline="") as f:
            rows = list(csv.DictReader(f))
            seen = {r["id"] for r in rows}
    new = [e for e in events if e["actual"] is not None and e["id"] not in seen]
    if not new:
        return 0
    for e in new:
        rows.append({"id": e["id"], "time_utc": e["t"], "country": e["c"], "release": e["name"],
                     "period": e["period"], "actual": e["actual"], "consensus": "" if e["forecast"] is None else e["forecast"],
                     "previous": "" if e["previous"] is None else e["previous"], "unit": e["unit"], "scale": e["scale"],
                     "importance": "key" if e["imp"] == 2 else "medium"})
    rows.sort(key=lambda r: (r["time_utc"], r["country"], r["release"]))
    with open(HIST, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(new)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    now = datetime.now(timezone.utc)
    start, end = week_window(now)
    events, used, errors = None, None, []
    for name, fn in SOURCES:
        try:
            ev = fn(start, end)
            if ev:
                events, used = ev, name
                break
            errors.append("%s: no events" % name)
        except Exception as ex:  # keep going to the backup
            errors.append("%s: %s" % (name, ex))
    for e in errors:
        print("warning:", e)
    if events is None:
        print("All sources failed; keeping the last saved calendar.")
        return 0
    events.sort(key=lambda e: (e["t"], -e["imp"], e["c"], e["name"]))
    data = {"week_start": start.strftime("%Y-%m-%d"), "source": used, "countries": COUNTRIES, "events": events}
    old = None
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f)
            old.pop("checked", None)
    if old != data:
        data_out = dict(data, checked=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        with open(OUT, "w") as f:
            json.dump(data_out, f, separators=(",", ":"))
        print("Saved %d events from %s (week of %s)" % (len(events), used, data["week_start"]))
    else:
        print("No change (%d events from %s)" % (len(events), used))
    print("History: %d new released numbers" % save_history(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
