#!/usr/bin/env python3
"""副业日报 · 内容源管理系统（零 LLM 成本）

为什么需要它（重新评估结论）：
  锁内容源上限/容量「值得且必须」做，理由有三——
    1) 防单源垄断：中年指南 / aggregator 类高产源会淹没其他源，降低多样性；
    2) 保质量下限：低质源不该占满候选预算，应把额度让给高质源；
    3) 锁资源上限：fetch HTTP 次数、AI 上下文长度、LLM 成本都随候选量线性增长。

本模块在「锁容量」前提下做精细化：用【运行时指标 + 启发式相关度】给每源打
综合分（0-100，相关/稳定/产量/质量 四维），再按分把总预算加权分配给每源 cap。
高分源更高 cap（提质量），但 cap_max 封顶 + 全局硬上限兜底（防垄断）。

同时实现「内容源生命周期」：
  - 新源观测期(trial)：小 cap 试运行，积累指标后综合评分；
  - 晋升替换：达标则转 active 并替换当前最低分 active 源（被替换源进 retired 备份，不删）；
  - 死源淘汰：长期无贡献 / 连续抓取失败 → retired（保留记录可回滚）。

所有判定默认「仅建议」，写入 state/source_actions.json 审计；只有显式
FUYR_SOURCE_AUTOMATION=1 才落地修改 sources.json（绝不静默改动）。
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()


def _load_metrics():
    return C.load_json(C.SOURCE_METRICS_FILE, {})


def _save_metrics(m):
    C.save_json(C.SOURCE_METRICS_FILE, m)


def _save_actions(actions):
    try:
        C.save_json(C.SOURCE_ACTIONS_FILE, {"date": C.date_str(), "actions": actions})
    except Exception:
        pass


def _init_metric(sid):
    return {
        "id": sid,
        "runs": 0, "fetch_ok": 0, "fetch_total": 0,
        "fresh_total": 0, "contributed_cards": 0,
        "last_ok_date": None, "last_fresh_date": None,
        "consec_fetch_fail": 0,
        "status": "active",          # active | trial | retired | legacy
        "score": None, "eval_score": None,
        "trial_since": None, "trial_runs": 0,
        "relevance": None,
    }


def record_run(source_status):
    """每次 fetch_signals 后调用：把当次每源的抓取结果写入指标。

    source_status: dict id -> {"ok":bool,"reason":str,"got":int,"fresh":int}
    """
    try:
        metrics = _load_metrics()
        today = C.date_str()
        for sid, st in source_status.items():
            m = metrics.get(sid) or _init_metric(sid)
            m["runs"] += 1
            m["fetch_total"] += 1
            if st.get("ok"):
                m["fetch_ok"] += 1
                m["consec_fetch_fail"] = 0
                m["last_ok_date"] = today
                fresh = int(st.get("fresh", 0) or 0)
                m["fresh_total"] += fresh
                if fresh > 0:
                    m["last_fresh_date"] = today
            else:
                m["consec_fetch_fail"] += 1
            if m["status"] == "trial":
                m["trial_runs"] = m.get("trial_runs", 0) + 1
            metrics[sid] = m
        _save_metrics(metrics)
    except Exception as e:
        LOG.warning("源指标记录失败（不影响主流程）: %s", e)


def record_contributions(report, sources=None):
    """generate_report 后调用：统计每源最终成卡数，累积到质量维度。

    report: {"modules": {mod: [item,...]}}，item 含 source_name。
    """
    try:
        if not sources:
            sources = C.load_json(C.SOURCES_FILE, [])
        name2id = {s.get("name"): s.get("id") for s in sources if s.get("name")}
        metrics = _load_metrics()
        changed = False
        for mod in (report.get("modules") or {}):
            for it in (report.get("modules", {}).get(mod) or []):
                sid = name2id.get(it.get("source_name", ""))
                if sid and sid in metrics:
                    metrics[sid]["contributed_cards"] = metrics[sid].get("contributed_cards", 0) + 1
                    changed = True
        if changed:
            _save_metrics(metrics)
    except Exception as e:
        LOG.warning("源成卡统计失败（不影响主流程）: %s", e)


def compute_score(m, relevance):
    """四维综合分（0-100）。relevance 为 0-1 静态相关度（curated 或启发式）。"""
    rel = relevance if relevance is not None else (m.get("relevance") if m.get("relevance") is not None else 0.6)
    ft = m.get("fetch_total", 0)
    stab = (m.get("fetch_ok", 0) / ft) if ft else 1.0
    runs = max(1, m.get("runs", 1))
    yield_d = min(1.0, (m.get("fresh_total", 0) / runs) / C.SOURCE_YIELD_REF)
    qual_d = min(1.0, (m.get("contributed_cards", 0) / runs) / C.SOURCE_QUALITY_REF)
    wr = C.SOURCE_SCORE_WEIGHTS
    score = 100 * (wr[0] * rel + wr[1] * stab + wr[2] * yield_d + wr[3] * qual_d)
    return round(score, 1)


def _relevance_for(s, metrics):
    rel = s.get("relevance")
    if isinstance(rel, (int, float)):
        return min(1.0, max(0.0, rel / 5.0))
    return C.heuristic_relevance(s.get("name", ""), s.get("url", ""))


def allocate_caps(sources, metrics=None):
    """按综合分把总预算加权分配给每源 cap。返回 dict id->cap。

    - active/legacy 源：cap_i = clamp(round(BUDGET * w_i/Σw), CAP_MIN, CAP_MAX)
    - trial 源：固定 TRIAL_CAP
    - 无指标（冷启动）：均匀回退，保证每源都有机会
    - 最终所有 cap 之和不超过 MAX_CANDIDATES（硬上限兜底）
    """
    metrics = metrics or _load_metrics()
    rel_map = {s.get("id"): _relevance_for(s, metrics) for s in sources}
    caps = {}
    active = [s for s in sources if metrics.get(s.get("id"), {}).get("status", "active") in ("active", "legacy")]
    trial = [s for s in sources if metrics.get(s.get("id"), {}).get("status") == "trial"]
    for s in trial:
        caps[s.get("id")] = C.SOURCE_CAP_TRIAL

    scores = {s.get("id"): compute_score(metrics.get(s.get("id"), {}), rel_map[s.get("id")]) for s in active}
    has_metrics = any(metrics.get(s.get("id"), {}).get("runs", 0) > 0 for s in active)

    if not has_metrics:
        uni = max(C.SOURCE_CAP_MIN, C.SOURCE_CAP_BUDGET // max(1, len(active)))
        for s in active:
            caps[s.get("id")] = min(C.SOURCE_CAP_MAX, uni)
    else:
        total_w = sum(max(1.0, scores[s.get("id")]) for s in active) or 1.0
        for s in active:
            sid = s.get("id")
            w = max(1.0, scores[sid])
            cap = round(C.SOURCE_CAP_BUDGET * w / total_w)
            caps[sid] = max(C.SOURCE_CAP_MIN, min(C.SOURCE_CAP_MAX, cap))

    ssum = sum(caps.values())
    if ssum > C.MAX_CANDIDATES:
        scale = C.MAX_CANDIDATES / ssum
        for k in caps:
            caps[k] = max(C.SOURCE_CAP_MIN, int(caps[k] * scale))
    return caps


def recommend_actions(sources, metrics=None):
    """扫描死源 / 观测期毕业，产出动作建议（写入审计文件，不落地）。"""
    metrics = metrics or _load_metrics()
    rel_map = {s.get("id"): _relevance_for(s, metrics) for s in sources}
    actions = []
    dead_ids = set()
    today = C.beijing_now()
    for s in sources:
        sid = s.get("id")
        m = metrics.get(sid, {})
        if m.get("status") == "retired":
            continue
        dead = False
        last_fresh = m.get("last_fresh_date")
        if last_fresh:
            try:
                d = datetime.datetime.strptime(last_fresh, "%Y-%m-%d").replace(tzinfo=C.BJ)
                if (today - d).days > C.SOURCE_DEAD_DAYS:
                    dead = True
            except Exception:
                pass
        if m.get("consec_fetch_fail", 0) >= C.SOURCE_DEAD_FAILS:
            dead = True
        if dead:
            dead_ids.add(sid)
            actions.append({"type": "retire", "id": sid, "reason": "dead",
                            "detail": {"last_fresh_date": last_fresh,
                                       "consec_fetch_fail": m.get("consec_fetch_fail", 0)}})

    for s in sources:
        sid = s.get("id")
        m = metrics.get(sid, {})
        if m.get("status") == "trial" and m.get("trial_runs", 0) >= C.SOURCE_INCUBATION_RUNS:
            eval_score = compute_score(m, rel_map[sid])
            m["eval_score"] = eval_score
            ft = m.get("fetch_total", 0)
            fetch_ok_rate = (m.get("fetch_ok", 0) / ft) if ft else 0.0
            if eval_score >= C.SOURCE_PROMOTE_SCORE and fetch_ok_rate >= C.SOURCE_MIN_FETCH_OK:
                pool = [x for x in sources
                        if metrics.get(x.get("id"), {}).get("status", "active") in ("active", "legacy")
                        and x.get("id") != sid
                        and x.get("id") not in dead_ids]
                if pool:
                    low = min(pool, key=lambda x: compute_score(metrics.get(x.get("id"), {}), rel_map[x.get("id")]))
                    actions.append({"type": "promote_replace", "id": sid,
                                    "replace_id": low.get("id"), "eval_score": eval_score})
                else:
                    actions.append({"type": "promote", "id": sid, "eval_score": eval_score})
            else:
                actions.append({"type": "retire", "id": sid, "reason": "trial_failed",
                                "eval_score": eval_score, "fetch_ok_rate": round(fetch_ok_rate, 2)})
    _save_actions(actions)
    return actions


def apply_actions(sources, actions, automate=False):
    """默认仅审计（automate=False）。automate=True 才把 status 变更落地到 sources.json。

    被替换/淘汰的源进入 retired（保留记录 + retired_at），不直接删除，便于回滚。
    """
    if not automate:
        return actions
    retired_ids = {a["id"] for a in actions if a["type"] == "retire"}
    out = []
    for s in sources:
        sid = s.get("id")
        e = dict(s)
        e.setdefault("status", "active")
        if sid in retired_ids:
            e["status"] = "retired"
            e["retired_at"] = C.date_str()
        for a in actions:
            if a["type"] == "promote_replace" and a["id"] == sid:
                e["status"] = "active"
                e["promoted_at"] = C.date_str()
                e.pop("trial_since", None)
            if a["type"] == "promote_replace" and a["replace_id"] == sid:
                e["status"] = "retired"
                e["retired_at"] = C.date_str()
                e["replaced_by"] = a["id"]
            if a["type"] == "promote" and a["id"] == sid:
                e["status"] = "active"
                e["promoted_at"] = C.date_str()
        out.append(e)
    C.save_json(C.SOURCES_FILE, out)
    LOG.info("已落地源动作 %d 项（含 retired 备份）", len(actions))
    return out


def maybe_automate(sources):
    """run_daily 每轮调用：始终写建议；FUYR_SOURCE_AUTOMATION=1 才落地。"""
    actions = recommend_actions(sources)
    automate = os.environ.get("FUYR_SOURCE_AUTOMATION", "").strip() in ("1", "true", "True")
    if automate:
        apply_actions(sources, actions, automate=True)
        LOG.info("源自动化已启用，落地 %d 项动作", len(actions))
    else:
        if actions:
            LOG.info("源动作建议 %d 项（未落地，设 FUYR_SOURCE_AUTOMATION=1 启用）: %s",
                     len(actions), [a["type"] + ":" + a["id"] for a in actions])
    return actions


def main():
    C.ensure_dirs()
    sources = C.load_json(C.SOURCES_FILE, [])
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "caps")
    if cmd == "status":
        metrics = _load_metrics()
        rel_map = {s.get("id"): _relevance_for(s, metrics) for s in sources}
        for s in sources:
            sid = s.get("id")
            m = metrics.get(sid, {})
            print("%-22s status=%-8s score=%s runs=%s fresh=%s cards=%s ok_rate=%s" % (
                sid, m.get("status", "active"),
                compute_score(m, rel_map[sid]) if m.get("runs") else "-",
                m.get("runs", 0), m.get("fresh_total", 0),
                m.get("contributed_cards", 0),
                round(m.get("fetch_ok", 0) / m.get("fetch_total", 1), 2) if m.get("fetch_total") else "-"))
    elif cmd == "caps":
        caps = allocate_caps(sources)
        for s in sources:
            print("%-22s cap=%s" % (s.get("id"), caps.get(s.get("id"))))
        print("--- 总预算=%d 实际分配和=%d 硬上限=%d ---" % (
            C.SOURCE_CAP_BUDGET, sum(caps.values()), C.MAX_CANDIDATES))
    elif cmd == "recommend":
        acts = recommend_actions(sources)
        print(json.dumps(acts, ensure_ascii=False, indent=2))
    elif cmd == "apply":
        acts = recommend_actions(sources)
        apply_actions(sources, acts, automate=True)
        print("已落地。")
    else:
        print("用法: python source_manager.py [status|caps|recommend|apply]")


if __name__ == "__main__":
    main()
