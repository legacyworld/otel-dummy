#!/usr/bin/env python3
"""Manually raise / lower the simulated CPU load while generate.py is running.

  python3 cpu_load.py start redis 80              # ramp redis to 80% until `stop`
  python3 cpu_load.py start app 50 --duration 10  # app at 50% for 10 minutes
  python3 cpu_load.py stop [redis|app|all]        # ramp back down (default: all)
  python3 cpu_load.py status

Changes are written to control.json; generate.py picks them up at its next batch
(within ~interval seconds) and the load shows up in the data from "now" onward.
"""
import argparse
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from generate import HOSTS, read_control, write_control

JST = timezone(timedelta(hours=9))


def hhmmss(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%H:%M:%S")


def is_active(p, now):
    return p.get("end_ts") is None or p["end_ts"] > now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="start (or change) CPU load on a host")
    st.add_argument("host", choices=sorted(HOSTS))
    st.add_argument("cpu", type=float, help="target CPU percent (1-99)")
    st.add_argument("--duration", type=float, help="minutes; omit to keep going until `stop`")
    st.add_argument("--ramp", type=float, default=30, help="ramp-up/down seconds (default 30)")
    sp = sub.add_parser("stop", help="ramp the load back down")
    sp.add_argument("host", nargs="?", default="all", choices=sorted(HOSTS) + ["all"])
    sub.add_parser("status", help="show current and recent manual loads")
    a = ap.parse_args()

    now = time.time()
    phases = [p for p in read_control() if p.get("end_ts") is None or p["end_ts"] > now - 3600]

    if a.cmd == "start":
        if not 1 <= a.cpu <= 99:
            sys.exit("cpu must be between 1 and 99")
        end_ts = now + a.duration * 60 if a.duration else None
        running = [p for p in phases if p["host"] == a.host and is_active(p, now)]
        if running:
            for p in running:
                p["cpu_percent"], p["end_ts"] = a.cpu, end_ts
            print("%s: already loaded, changed to %g%% (applies immediately, no ramp)" % (a.host, a.cpu))
        else:
            phases.append({"id": uuid.uuid4().hex[:8], "host": a.host, "cpu_percent": a.cpu,
                           "start_ts": now, "end_ts": end_ts, "ramp_seconds": a.ramp})
            print("%s: CPU -> %g%% (ramp %gs, %s)" % (a.host, a.cpu, a.ramp,
                  "for %g min" % a.duration if a.duration else "until `stop`"))
    elif a.cmd == "stop":
        n = 0
        for p in phases:
            if (a.host in ("all", p["host"])) and is_active(p, now):
                p["end_ts"] = min(p["end_ts"] or float("inf"), now + p.get("ramp_seconds", 30))
                n += 1
                print("%s: ramping down, back to baseline at %s" % (p["host"], hhmmss(p["end_ts"])))
        if not n:
            print("no active load to stop")
    else:
        if not phases:
            print("no manual loads")
        for p in phases:
            e = p.get("end_ts")
            state = "running" if e is None else ("ends %s" % hhmmss(e) if e > now else "ended %s" % hhmmss(e))
            print("%-6s %5g%%  since %s  %s" % (p["host"], p["cpu_percent"], hhmmss(p["start_ts"]), state))
        return
    write_control(phases)


if __name__ == "__main__":
    main()
