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
import re
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
        "credit": C.CREDIT_INIT,        # 征信信用分 0-100（故障递减/成功递增）
        "reinstate_streak": 0,          # 月末巡检连续恢复次数（达门槛才重纳）
    }


def record_run(source_status):
    """每次 fetch_signals 后调用：把当次每源的抓取结果写入指标。

    source_status: dict id -> {"ok":bool,"reason":str,"got":int,"fresh":int}
    """
    try:
        metrics = _load_metrics()
        # 清理历史遗留非法键：早期版本曾把文章 URL 误当源 id 落键，现已废弃，
        # 这些孤儿键永远不会被更新，留着只会污染指标文件，故在此一次性修剪。
        for bad in [k for k in list(metrics.keys())
                    if isinstance(k, str) and k.startswith("http")]:
            del metrics[bad]
            LOG.info("清理遗留指标键 %s", bad)
        today = C.date_str()
        for sid, st in source_status.items():
            # 与默认指标合并，避免历史/外部写入的残缺条目缺字段导致整轮记录中断
            base = _init_metric(sid)
            existing = metrics.get(sid)
            if existing:
                base.update(existing)
            m = base
            m["runs"] += 1
            m["fetch_total"] += 1
            if st.get("ok"):
                m["fetch_ok"] += 1
                m["consec_fetch_fail"] = 0
                m["last_ok_date"] = today
                m["credit"] = min(C.CREDIT_INIT, m.get("credit", C.CREDIT_INIT) + C.CREDIT_OK_BONUS)
                fresh = int(st.get("fresh", 0) or 0)
                m["fresh_total"] += fresh
                if fresh > 0:
                    m["last_fresh_date"] = today
            elif st.get("reason") == "http_403":
                # 数据中心 IP 偶发 403（非源本身失效）→ 中性处理：不计失败、不扣信用，
                # 避免误把正常源判死（见 fetch_signals 的 403 抖动豁免）。
                pass
            else:
                m["consec_fetch_fail"] += 1
                m["credit"] = max(0, m.get("credit", C.CREDIT_INIT) - C.CREDIT_FAIL_PENALTY)
            if m["status"] == "trial":
                m["trial_runs"] = m.get("trial_runs", 0) + 1
            metrics[sid] = m
        _save_metrics(metrics)
    except Exception as e:
        LOG.warning("源指标记录失败（不影响主流程）: %s", e)


def is_fused(sid):
    """信用熔断：credit ≤ CREDIT_RETIRE 的源直接跳过抓取（自动熔断，防空耗与误告警）。

    源信用注册表（source_credit.json）为主、手动 retired 为辅，统一在此裁决是否抓取。
    """
    try:
        m = _load_metrics().get(sid)
        if not m:
            return False
        return (m.get("credit", C.CREDIT_INIT) <= C.CREDIT_RETIRE)
    except Exception:
        return False


def top_credits(n=5):
    """返回信用分最高的前 n 个源（监控巡检用）。"""
    try:
        metrics = _load_metrics()
        rows = sorted(
            ((sid, m.get("credit", C.CREDIT_INIT)) for sid, m in metrics.items()
             if isinstance(m, dict)),
            key=lambda x: x[1], reverse=True,
        )
        return rows[:n]
    except Exception:
        return []


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


def ensure_fixed_base_sources(sources):
    """地基保障（R5）：幂等确保固定基础源（用户指定）始终存在且 pinned+fixed。

    - 已存在 → 强制 pinned=True, fixed=True（防止配置遗漏导致地基源被动态算法淘汰/饿死）；
    - 缺失 → 注入默认条目（含已知镜像 RSS / GitHub 地址），永不依赖外部配置完整性。
    返回（可能新增后的）源列表。默认不写回磁盘（避免误覆盖部署 sources.json），
    仅保证本次运行的内存态正确；配置侧应在 sources.json 同步打 fixed 标记。
    """
    by_id = {s.get("id") for s in sources}
    out = list(sources)
    DEFAULTS = {
        "zhongnianren": {"id": "zhongnianren", "type": "rss",
                          "url": "https://17678319606.github.io/tg-aggregator-cn-snapshot/zhongnianren/rss.xml",
                          "name": "中年指南", "microblog": True,
                          "rationale": "固定基础源：副业/赚钱/省钱独特观点核心源"},
        "cid": {"id": "cid", "type": "github_readme_diff",
                "url": "https://raw.githubusercontent.com/1c7/chinese-independent-developer/master/README.md",
                "name": "中文独立开发者",
                "anchor": "https://github.com/1c7/chinese-independent-developer",
                "snapshot": "state/cid_readme_snapshot.txt",
                "rationale": "固定基础源：中文独立开发者产品库，主题强契合"},
    }
    for sid in C.FIXED_BASE_SOURCE_IDS:
        if sid in by_id:
            for s in out:
                if s.get("id") == sid:
                    # 强制地基属性（配置即使遗漏也兜底）
                    s["pinned"] = True
                    s["fixed"] = True
        else:
            d = dict(DEFAULTS.get(sid, {"id": sid, "type": "rss", "url": "", "name": sid}))
            d["pinned"] = True
            d["fixed"] = True
            out.append(d)
            LOG.warning("⚠ 地基源 %s 缺失，已注入默认条目（建议同步更新 sources.json）", sid)
    return out


def allocate_caps(sources, metrics=None):
    """按综合分把总预算加权分配给每源 cap。返回 dict id->cap。

    - pinned 源（sources.json pinned==true）：固定 PINNED_SOURCE_CAP，不参与评分分配；
    - active/legacy 源：cap_i = clamp(round(BUDGET * w_i/Σw), CAP_MIN, CAP_MAX)
    - trial 源：固定 TRIAL_CAP
    - 无指标（冷启动）：均匀回退，保证每源都有机会
    - 最终所有 cap 之和不超过 MAX_CANDIDATES（硬上限兜底）
    """
    metrics = metrics or _load_metrics()
    sources = ensure_fixed_base_sources(sources)
    rel_map = {s.get("id"): _relevance_for(s, metrics) for s in sources}
    caps = {}

    # Pinned 源：固定最高配额，不参与轮替
    pinned = [s for s in sources if s.get("pinned")]
    for s in pinned:
        caps[s.get("id")] = C.PINNED_SOURCE_CAP

    trial = [s for s in sources if metrics.get(s.get("id"), {}).get("status") == "trial" and not s.get("pinned")]
    for s in trial:
        caps[s.get("id")] = C.SOURCE_CAP_TRIAL

    # active/legacy 源（排除 pinned 和 trial）
    active = [s for s in sources if metrics.get(s.get("id"), {}).get("status", "active") in ("active", "legacy")
              and not s.get("pinned")]
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
            cap = max(C.SOURCE_CAP_MIN, min(C.SOURCE_CAP_MAX, cap))
            # 信用长期优异 → 放宽内容量（cap 上浮），体现"动态变化算法提升源丰富性"。
            # 仅对活跃优质源生效，且封顶 SOURCE_CAP_MAX 防止单一源垄断候选池。
            if metrics.get(sid, {}).get("credit", C.CREDIT_INIT) >= C.CREDIT_RELAX:
                cap = min(C.SOURCE_CAP_MAX, round(cap * C.SOURCE_CAP_RELAX))
            caps[sid] = cap

    ssum = sum(caps.values())
    if ssum > C.MAX_CANDIDATES:
        scale = C.MAX_CANDIDATES / ssum
        for k in caps:
            caps[k] = max(C.SOURCE_CAP_MIN, int(caps[k] * scale))
    return caps


def recommend_actions(sources, metrics=None):
    """扫描死源 / 观测期毕业，产出动作建议（写入审计文件，不落地）。"""
    metrics = metrics or _load_metrics()
    sources = ensure_fixed_base_sources(sources)
    rel_map = {s.get("id"): _relevance_for(s, metrics) for s in sources}
    actions = []
    dead_ids = set()
    today = C.beijing_now()
    for s in sources:
        sid = s.get("id")
        # 固定基础源（pinned/fixed）永不淘汰/轮替——地基保障
        if s.get("pinned") or s.get("fixed"):
            continue
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
        # 征信信用跌破阈值也视为不可靠 → 淘汰（保留记录可回滚）
        if (m.get("credit", C.CREDIT_INIT)) <= C.CREDIT_RETIRE:
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

    # —— 固定源晋升：信用长期优异 + 运行稳定 → 建议晋升为 fixed(pinned) ——
    # pinned 源不参与淘汰轮替、固定最高配额，是「基础源」的终态。
    for s in sources:
        sid = s.get("id")
        if s.get("pinned") or s.get("fixed"):
            continue
        m = metrics.get(sid, {})
        ft = m.get("fetch_total", 0)
        ok_rate = (m.get("fetch_ok", 0) / ft) if ft else 0.0
        if (m.get("status") in ("active", "legacy")
                and (m.get("credit", C.CREDIT_INIT)) >= C.CREDIT_PINNED
                and m.get("runs", 0) >= C.SOURCE_PIN_RUNS
                and ok_rate >= C.SOURCE_MIN_FETCH_OK
                and rel_map.get(sid, 0.0) >= C.SOURCE_PIN_RELEVANCE):
            actions.append({"type": "pin", "id": sid,
                            "reason": "credit>=%d & runs>=%d & ok>=%.2f & rel>=%.2f" % (
                                C.CREDIT_PINNED, C.SOURCE_PIN_RUNS,
                                C.SOURCE_MIN_FETCH_OK, C.SOURCE_PIN_RELEVANCE)})
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
            if e.get("fixed"):
                LOG.warning("地基源 %s 受保护，跳过淘汰（不参与 retired 轮替）", sid)
            else:
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
            if a["type"] == "pin" and a["id"] == sid:
                e["status"] = "active"
                e["pinned"] = True
                e["pinned_at"] = C.date_str()
        out.append(e)
    C.save_json(C.SOURCES_FILE, out)
    LOG.info("已落地源动作 %d 项（含 retired 备份）", len(actions))
    return out


def review_retired(sources, metrics=None, automate=False):
    """月末征信巡检：对 retired 源重新实拉取校验，恢复且信用达标则重纳为 trial。

    - 仅对 status==retired 的源执行（active/trial 不动）；
    - 恢复有效 → credit += CREDIT_REVIEW_OK，连胜 +1；连续 CREDIT_REINSTATE_STREAK
      次成功且 credit ≥ CREDIT_REINSTATE → 重纳为 trial（重置运行计数，保留信用，防 flapping）；
    - 仍无效 → 连胜清零，credit -= CREDIT_REVIEW_FAIL；
    - 校验零 LLM（仅 requests + feedparser），免费额度内；
    - 默认仅审计（automate=False），显式 --apply 才落地 status 到 sources.json。
    """
    metrics = metrics or _load_metrics()
    try:
        import discover_sources as D
        have_validator = True
    except Exception as e:
        have_validator = False
        LOG.warning("加载发现引擎校验器失败，征信巡检跳过: %s", e)
    actions = []
    for s in sources:
        sid = s.get("id")
        m = metrics.get(sid, {})
        if m.get("status") != "retired":
            continue
        if not have_validator:
            continue
        cfg = {"id": sid, "type": s.get("type", "rss"),
               "url": s.get("url", ""), "name": s.get("name", "")}
        try:
            valid, _, _titles = D.validate_source(cfg)
        except Exception as e:
            valid = False
            LOG.warning("征信巡检校验 %s 异常: %s", sid, e)
        if valid:
            new_credit = min(C.CREDIT_INIT, (m.get("credit") or C.CREDIT_INIT) + C.CREDIT_REVIEW_OK)
            streak = m.get("reinstate_streak", 0) + 1
            m["credit"] = new_credit
            m["reinstate_streak"] = streak
            if streak >= C.CREDIT_REINSTATE_STREAK and new_credit >= C.CREDIT_REINSTATE:
                m["status"] = "trial"
                m["reinstate_streak"] = 0
                m["trial_since"] = C.date_str()
                m["trial_runs"] = 0
                m["runs"] = 0
                m["fetch_total"] = 0
                m["fetch_ok"] = 0
                m["fresh_total"] = 0
                m["consec_fetch_fail"] = 0
                m["last_ok_date"] = None
                m["last_fresh_date"] = None
                m["reinstated_at"] = C.date_str()
                actions.append({"type": "reinstate", "id": sid, "credit": new_credit,
                                "detail": "连续 %d 次巡检恢复，重纳为 trial" % streak})
                LOG.info("♻ %s 征信恢复，重纳为 trial（信用 %d）", sid, new_credit)
            else:
                actions.append({"type": "review_ok", "id": sid, "credit": new_credit,
                                "detail": "第 %d 次恢复，尚未达重纳门槛" % streak})
                LOG.info("• %s 巡检恢复（信用 %d，连胜 %d/%d）", sid, new_credit,
                         streak, C.CREDIT_REINSTATE_STREAK)
        else:
            m["reinstate_streak"] = 0
            m["credit"] = max(0, (m.get("credit") or C.CREDIT_INIT) - C.CREDIT_REVIEW_FAIL)
            actions.append({"type": "review_fail", "id": sid, "credit": m["credit"],
                            "detail": "仍无效，信用递减"})
            LOG.info("✗ %s 巡检仍无效（信用 %d）", sid, m["credit"])
        metrics[sid] = m
    _save_metrics(metrics)
    C.save_json(os.path.join(C.STATE_DIR, "source_credit_review.json"),
                {"date": C.date_str(), "actions": actions})
    if automate:
        reinstated = {a["id"] for a in actions if a["type"] == "reinstate"}
        out = []
        for s in sources:
            e = dict(s)
            e.setdefault("status", "active")
            if s.get("id") in reinstated:
                e["status"] = "trial"
                e["reinstated_at"] = C.date_str()
            out.append(e)
        C.save_json(C.SOURCES_FILE, out)
        LOG.info("已落地 %d 个源重纳为 trial", len(reinstated))
    else:
        LOG.info("征信巡检 %d 项（未落地，加 --apply 启用）", len(actions))
    return actions


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
    # 纳新触发：每次运行都基于多样性体检产出候选源建议（仅审计，不自动写入 sources.json）
    try:
        scout_new_sources(sources)
    except Exception as e:
        LOG.warning("纳新触发跳过（不影响主流程）: %s", e)
    return actions


def _diversity_host_of(url):
    m = re.match(r"https?://([^/]+)/?", url or "")
    return (m.group(1) or "").lower() if m else ""


def diversity_report(sources, metrics=None):
    """源多样性体检：主机/类型分布 + 集中度（防单源垄断 / 漏源监控）。

    输出到 state/source_diversity.json，纯本地计算，零网络。
    指标：
      - distinct_hosts / total：源分散度（过低说明高度同源，漏源风险）；
      - type_distribution：类型覆盖（reddit_json/rss/github_* 是否均衡）；
      - top_host_share：最高频主机占比，>=0.4 标记垄断风险；
      - active_sources / cap_headroom：活跃源数与扩源余量（对照 SOURCE_ACTIVE_CAP）。
    """
    metrics = metrics or _load_metrics()
    n = len(sources)
    hosts, types = {}, {}
    for s in sources:
        h = _diversity_host_of(s.get("url", ""))
        hosts[h] = hosts.get(h, 0) + 1
        t = s.get("type", "?")
        types[t] = types.get(t, 0) + 1
    top_host = max(hosts.values()) if hosts else 0
    top_host_share = round(top_host / n, 2) if n else 0
    active = sum(1 for s in sources
                 if metrics.get(s.get("id"), {}).get("status", "active")
                 in ("active", "legacy", "trial"))
    report = {
        "date": C.date_str(),
        "total_sources": n,
        "active_sources": active,
        "distinct_hosts": len(hosts),
        "type_distribution": types,
        "top_host_share": top_host_share,
        "monopoly_risk": top_host_share >= 0.4,
        "cap_headroom": max(0, C.SOURCE_ACTIVE_CAP - active),
    }
    C.save_json(os.path.join(C.STATE_DIR, "source_diversity.json"), report)
    return report


def scout_new_sources(sources, metrics=None):
    """R2 纳新触发：基于多样性体检，当存在垄断风险或仍有扩源余量时，
    产出『建议引入』的候选源清单（trial），写入 state/source_scout.json 审计。
    候选仅作"待人工/校验器审核"建议，绝不自动写入 sources.json（未校验源直接入池是 bug）。
    幂等：已建议过且尚未入源的候选不再重复举荐，避免每轮刷相同硬编码种子。"""
    metrics = metrics or _load_metrics()
    rep = diversity_report(sources, metrics)
    if not (rep.get("monopoly_risk") or rep.get("cap_headroom", 0) > 0):
        C.save_json(os.path.join(C.STATE_DIR, "source_scout.json"),
                    {"date": C.date_str(), "scouted": False, "reason": "diversity_ok"})
        return []
    # 主题相关候选种子池（副业/赚钱/增长/AI 工具类高质量公开源）；仅作建议，需校验后入源。
    SEED_POOL = [
        {"id": "scout_indiehackers", "name": "Indie Hackers", "type": "rss",
         "url": "https://www.indiehackers.com/feed"},
        {"id": "scout_hackernews", "name": "Hacker News", "type": "rss",
         "url": "https://news.ycombinator.com/rss"},
        {"id": "scout_producthunt", "name": "Product Hunt", "type": "rss",
         "url": "https://www.producthunt.com/feed"},
        {"id": "scout_ycombinator", "name": "Y Combinator Blog", "type": "rss",
         "url": "https://www.ycombinator.com/blog/rss.xml"},
        {"id": "scout_saastr", "name": "SaaStr", "type": "rss",
         "url": "https://www.saastr.com/feed/"},
    ]
    existing_ids = {s.get("id") for s in sources}
    # 幂等：读取历史已建议 id，避免重复刷相同种子
    hist_path = os.path.join(C.STATE_DIR, "source_scout.json")
    suggested_before = set()
    try:
        _h = C.load_json(hist_path) or {}
        suggested_before = set(_h.get("suggested_history", []))
    except Exception:
        pass
    cands = [c for c in SEED_POOL
             if c["id"] not in existing_ids and c["id"] not in suggested_before]
    validated = []
    try:
        import discover_sources as D
        for c in cands:
            try:
                ok, _, _t = D.validate_source(c)
                if ok:
                    validated.append(c)
            except Exception:
                pass
    except Exception:
        # 无校验器：列出为"建议"，但明确标记 needs_validation，交由人工/后续校验器审核。
        validated = [{**c, "needs_validation": True} for c in cands]
    out = {"date": C.date_str(), "scouted": True,
           "monopoly_risk": rep.get("monopoly_risk"),
           "cap_headroom": rep.get("cap_headroom"),
           "candidates": validated,
           "suggested_history": sorted(suggested_before | {c["id"] for c in cands})}
    C.save_json(hist_path, out)
    if validated:
        LOG.info("📡 纳新触发：建议引入 %d 个候选源(待校验/trial，未自动入池): %s",
                 len(validated), [c["id"] for c in validated])
    else:
        LOG.info("📡 纳新触发但无可新增候选（已建议过的不再重复举荐）")
    return out


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
    elif cmd == "review-retired":
        automate = "--apply" in sys.argv
        acts = review_retired(sources, automate=automate)
        print(json.dumps(acts, ensure_ascii=False, indent=2))
    elif cmd == "diversity":
        rep = diversity_report(sources)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif cmd == "scout":
        out = scout_new_sources(sources)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("用法: python source_manager.py [status|caps|recommend|apply|review-retired [--apply]|diversity|scout]")


if __name__ == "__main__":
    main()
