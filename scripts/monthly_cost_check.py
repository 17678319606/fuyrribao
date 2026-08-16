#!/usr/bin/env python3
"""月度成本自检：统计最近 COST_LOOKBACK_DAYS 天的 AI 估算成本。

读 state/cost_log.json（由 generate_report.py 经 common.log_run_cost 写入），
按璇玑费率折算 ¥。超预算（FUYR_COST_BUDGET，默认 COST_BUDGET_DEFAULT）仅告警不阻断。
退出码 2 = 超预算告警；0 = 正常。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


def main():
    C.ensure_dirs()
    log = C.load_json(C.COST_LOG_FILE, [])
    cutoff = C.days_ago_iso(C.COST_LOOKBACK_DAYS)[:10]
    recent = [r for r in log if r.get("date", "") >= cutoff]
    total = sum(r.get("cost", 0) for r in recent)
    by_tag = {}
    for r in recent:
        by_tag[r.get("tag", "?")] = by_tag.get(r.get("tag", "?"), 0) + r.get("cost", 0)
    budget = float(os.environ.get("FUYR_COST_BUDGET", C.COST_BUDGET_DEFAULT))
    print("近 %d 天 AI 成本估算：¥%.4f（预算 ¥%.2f）" % (C.COST_LOOKBACK_DAYS, total, budget))
    for t, c in sorted(by_tag.items(), key=lambda kv: -kv[1]):
        print("  - %s: ¥%.4f" % (t, c))
    if total > budget:
        print("⚠️ 成本超预算，请检查采集量/模型/重试次数。")
        raise SystemExit(2)
    print("✅ 成本在预算内。")


if __name__ == "__main__":
    main()
