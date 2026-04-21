#!/usr/bin/env python3
"""Parse events log for [CANCEL-LAT] telemetry and report latency distribution.

After the cancel-auth fix, every cancel call now logs two lines:
  [CANCEL-LAT] COIN SIDE oid=X resp_ms=N success=Y
  [CANCEL-LAT] COIN SIDE oid=X confirmed_ms=N status=CANCELLED

resp_ms = HTTP request round-trip.
confirmed_ms = from cancel send to CLOB status=CANCELLED observed.

The confirmed_ms metric is what the shadow simulation needs to replace its
fixed 3s cancel_latency assumption. A realistic latency distribution will
tighten shadow/live alignment significantly.

Usage:
  python3 scripts/cancel_latency_report.py [--log PATH]
"""
import argparse
import re
import sys
from collections import Counter
from statistics import median, quantiles


def parse(log_path):
    resp_ms = []
    conf_ms = []
    pat_resp = re.compile(r"\[CANCEL-LAT\] .* resp_ms=(\d+)")
    pat_conf = re.compile(r"\[CANCEL-LAT\] .* confirmed_ms=(\d+)")
    with open(log_path, "rb") as f:
        # Read the last ~100MB for performance; full log can be multi-GB
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 100_000_000))
        data = f.read().decode("utf-8", errors="ignore")
    for line in data.splitlines():
        m = pat_resp.search(line)
        if m:
            resp_ms.append(int(m.group(1)))
            continue
        m = pat_conf.search(line)
        if m:
            conf_ms.append(int(m.group(1)))
    return resp_ms, conf_ms


def summarize(name, values):
    if not values:
        print(f"  {name}: no data")
        return
    values = sorted(values)
    n = len(values)
    print(f"  {name}: n={n}")
    print(f"    min={values[0]}ms median={int(median(values))}ms max={values[-1]}ms")
    if n >= 4:
        q = quantiles(values, n=4)
        print(f"    p25={int(q[0])}ms p75={int(q[2])}ms")
    if n >= 20:
        q = quantiles(values, n=20)
        print(f"    p95={int(q[18])}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live_v30.events.log")
    args = ap.parse_args()

    try:
        resp_ms, conf_ms = parse(args.log)
    except FileNotFoundError:
        print(f"ERROR: {args.log} not found")
        sys.exit(1)

    print(f"Log: {args.log}")
    print(f"Cancel latency telemetry (since cancel-auth fix deploy):")
    print()
    print("HTTP response latency (cancel request round-trip):")
    summarize("resp_ms", resp_ms)
    print()
    print("Cancel confirmation latency (send → CLOB CANCELLED):")
    summarize("confirmed_ms", conf_ms)
    print()

    if conf_ms:
        median_conf = median(conf_ms)
        print(f"Implication for shadow: replace cancel_latency=3.0s with "
              f"~{median_conf/1000:.1f}s (median observed).")
        if median_conf > 3000:
            print(f"  WARNING: confirmed latency > 3s median. Reversal-cancel is slower than shadow assumes.")
        elif median_conf < 1000:
            print(f"  GOOD: confirmed latency < 1s median. Reversal-cancel races adverse fills effectively.")


if __name__ == "__main__":
    main()
