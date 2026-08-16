"""副业日报 · 公共常量与工具（被三个脚本共用）"""
import os
import re
import json
import logging
import datetime
import hashlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
STATE_DIR = os.path.join(REPO_ROOT, "state")
SOURCES_FILE = os.path.join(REPO_ROOT, "scripts", "sources.json")
SKILL_FILE = os.path.join(REPO_ROOT, "SKILL.md")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")

# —— 可调参数 ——
RETENTION_DAYS = 60          # 数据文件 / 去重表的保留天数（到期自动清理）
COLD_START_MAX_PER_SOURCE = 15  # 冷启动（首次运行）每个源最多取多少条，避免首期爆量
MAX_PER_SOURCE = 300         # 每个源每次最多保留多少条（RSS 受 Feed 本身条数限制；HTML/Reddit/Diff 类可拉更多；用户要求放宽到 300 以容纳高产源）
MAX_CANDIDATES = 1500        # 单日候选硬上限（兜底安全网，正常均衡后远不会触及）

# —— 单篇日报条目量与免费预筛（零 LLM，不破免费额度）——
MAX_ITEMS_PER_MODULE = 8      # 每个模块最多条目（防 AI 一次 dumping 过多导致不可扫读/上下文溢出；三模块合计通常 12–24）
PREFILTER_MIN_LEN = 80        # 免费预筛：标题+正文合计 < 此值且不含主题词的桩条目直接丢（降噪、省 AI 调用）

# —— 多样性均衡（P1-A 修复）——
# 候选超过 BALANCE_TRIGGER 即触发「按源均衡采样」，使单源占比可控、
# 不再出现单源垄断（如中年指南一度占 ~47%）；同时不浪费 AI 额度。
BALANCE_TRIGGER = 300         # 触发均衡的总候选阈值（正常量级 ~643 >> 300，必触发）
BALANCE_TARGET = 900          # 均衡目标总量；普通源单源上限 = ceil(目标 / 组数)
HIGH_SOURCE_CAP = 70          # 高配额源（增量源，如中年指南，sources.json 中 quota=="high"）
                              # 的更高但仍有上限的配额，避免过度砍掉其新内容
PINNED_SOURCE_CAP = 100       # 固定主用源（pinned==true，如中年指南）的专属高配额
                              # 不参与轮替淘汰，始终保留最高优先级和容量

# 渲染器版本：每次修改 publish_wp.py 的排版/结构时 +1。
# 发布到 WP 的文章顶部会写入 <!-- dr-renderer:N --> 标记；
# 定时/宝塔触发时发现线上当日文章版本过旧则强制重渲染，使修复自动落地（无需手动 Run workflow）。
RENDERER_VERSION = 8

BJ = datetime.timezone(datetime.timedelta(hours=8))


def beijing_now():
    return datetime.datetime.now(BJ)


def date_str(dt=None):
    # 支持「目标日期」覆盖（用于重生成/清洁历史文章）：经 DOCGEN_TARGET_DATE 注入。
    # 正常定时/手动运行该变量为空，回退到北京时间今天。
    override = os.environ.get("DOCGEN_TARGET_DATE", "").strip()
    if override:
        return override
    return (dt or beijing_now()).strftime("%Y-%m-%d")


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


def get_logger():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return logging.getLogger("sidehustle")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_seen():
    """返回 {id: first_seen_iso}"""
    return load_json(SEEN_FILE, {})


def save_seen(seen):
    save_json(SEEN_FILE, seen)


def days_ago_iso(days):
    return (beijing_now() - datetime.timedelta(days=days)).isoformat()


# ---------- 渲染器版本：让排版/结构修复自动落地 ----------

def extract_renderer_version(post_content):
    """从 WP 文章正文提取 dr-renderer 标记版本号；无标记返回版本 0（视为旧版）。"""
    if not post_content:
        return 0
    m = re.search(r"dr-renderer\s*:\s*(\d+)", post_content)
    return int(m.group(1)) if m else 0


# ---------- 同日增量累积：保留旧内容 + 追加新内容 ----------

def item_dedup_key(item):
    """稳定的去重键：优先 source_url，其次 title，再次内容哈希（避免无 url/title 的条目跨运行永不 dedup）。"""
    if isinstance(item, dict):
        k = item.get("source_url") or item.get("title")
        if k:
            return k
        sig = item.get("signal") or item.get("content") or ""
        if sig:
            return "h:" + hashlib.md5(sig.encode("utf-8", "ignore")).hexdigest()[:16]
        return id(item)
    return str(item)


MODULES = ("project_opportunities", "growth_operations", "views_insights")


def _normalize_title_key(t):
    """标题归一化键（供 merge_reports 跨源去重使用）。"""
    if not t:
        return ""
    import re, html as _html
    t = _html.unescape(t.lower().strip())
    t = re.sub(r'[^\w\u4e00-\u9fff]', '', t)
    return t if len(t) >= 8 else ""


def merge_reports(existing, new):
    """将 new 的条目按去重键并入 existing（existing 在前保留顺序，new 追加去重）。

    去重策略（两层）：
      ① item_dedup_key（source_url / title / content hash）——防同源重复；
      ② 归一化标题去重——防跨源同文不同 URL 重复。
    """
    if not existing:
        return new
    merged = dict(existing)
    merged.setdefault("modules", {})
    for mod in MODULES:
        ex_items = list(existing.get("modules", {}).get(mod, []))
        new_items = new.get("modules", {}).get(mod, [])
        keys = {item_dedup_key(x) for x in ex_items}
        # 归一化标题集合（跨源去重）
        title_keys = {_normalize_title_key(x.get("title", "")) for x in ex_items}
        for it in new_items:
            k = item_dedup_key(it)
            tk = _normalize_title_key(it.get("title", ""))
            if k not in keys and (not tk or tk not in title_keys):
                ex_items.append(it)
                keys.add(k)
                if tk:
                    title_keys.add(tk)
        merged["modules"][mod] = ex_items
    if new.get("daily_summary"):
        merged["daily_summary"] = new["daily_summary"]
    merged["date"] = existing.get("date") or new.get("date")
    merged["timezone"] = existing.get("timezone") or new.get("timezone") or "Asia/Shanghai"
    return merged


def cap_modules(modules, max_total):
    """截断 modules 使三模块总条数 ≤ max_total（保持相对均衡：反复从最长模块尾部删 1 条）。

    用于强制「分波上限 / 全天上限」：首波 ≤30、末波新增 ≤30、全天 ≤60。
    """
    if not isinstance(max_total, int):
        return modules
    out = {m: list(modules.get(m, [])) for m in MODULES}

    def _total():
        return sum(len(out[m]) for m in MODULES)

    while _total() > max_total:
        cand = [m for m in MODULES if out[m]]
        if not cand:
            break
        longest = max(cand, key=lambda m: len(out[m]))
        out[longest].pop()
    return out


# ─────────────────────────────────────────────────────────────────────
# 内容源管理系统（零 LLM 成本）：按「综合打分」动态分配每源候选容量上限
# ─────────────────────────────────────────────────────────────────────
# 总预算：所有「活跃(active)」源的容量上限之和的软目标。硬上限仍是 MAX_CANDIDATES。
# 设计意图：高质源给更高 cap（提质量），但 cap_max 封顶 + 硬上限兜底（防垄断）。
SOURCE_CAP_BUDGET = 700      # 总预算随目标源数(≈30–45)上调，远 < MAX_CANDIDATES
SOURCE_CAP_MIN = 5          # 单个活跃源的最低 cap（保证多样性，弱源也有露脸机会）
SOURCE_CAP_MAX = 120        # 单个活跃源的最高 cap（防单一高产源垄断候选池）
SOURCE_CAP_TRIAL = 15       # 观测期(trial)源的低容量（先小范围验证，不占满预算）
SOURCE_INCUBATION_RUNS = 8  # 新源观测期需积累的运行次数（≈8 天，跨两次/日）
SOURCE_PROMOTE_SCORE = 60   # 观测期综合分达标线（0-100），达标才晋升 active
SOURCE_MIN_FETCH_OK = 0.6   # 观测期 fetch 成功率下限，低于则淘汰
SOURCE_DEAD_DAYS = 120      # 连续 N 天无任何有效贡献 → 候选淘汰（数月级）
SOURCE_DEAD_FAILS = 14      # 连续 fetch 失败次数达到 → 淘汰（约两周持续故障）
# 活跃源数软上限：discover 自动纳入时若 active+trial 已达此数，则只评估不写入（防 OPML 批量溢出）
SOURCE_ACTIVE_CAP = 45
# —— 源征信管理（credit）：可靠性信用分 0-100，故障递减、成功递增 ——
CREDIT_INIT = 100           # 新建/手策源初始信用（高信任）
CREDIT_OK_BONUS = 3         # 每次 fetch 成功 +3（封顶 100）
CREDIT_FAIL_PENALTY = 8     # 每次 fetch 失败 -8（地板 0）
CREDIT_RETIRE = 20          # 信用 ≤ 此值 → 淘汰（retired 备份，不删）
CREDIT_REINSTATE = 50       # 月末巡检恢复后信用 ≥ 此值且连续 2 次成功 → 重纳
CREDIT_REVIEW_OK = 25       # 月末巡检：源恢复有效 +25（封顶 100）
CREDIT_REVIEW_FAIL = 10     # 月末巡检：仍无效 -10（地板 0）
CREDIT_REINSTATE_STREAK = 2 # 需连续 N 次月末巡检成功才重纳（防 flapping 抖动）
# 四维权重（相关 / 稳定 / 产量 / 质量），和为 1
SOURCE_SCORE_WEIGHTS = (0.30, 0.25, 0.20, 0.25)
SOURCE_YIELD_REF = 20       # 单源日均有效候选参考值（达到即产量维满分）
SOURCE_QUALITY_REF = 1.5    # 单源日均成卡参考值（达到即质量维满分）
SOURCE_METRICS_FILE = os.path.join(STATE_DIR, "source_metrics.json")
SOURCE_ACTIONS_FILE = os.path.join(STATE_DIR, "source_actions.json")

# ─────────────────────────────────────────────────────────────────────
# 成本日志（月度自检 / 低资源消耗监控）
# ─────────────────────────────────────────────────────────────────────
COST_INPUT_RATE = 0.5 / 1_000_000    # 璇玑 ¥/1M 输入 token
COST_OUTPUT_RATE = 1.5 / 1_000_000   # 璇玑 ¥/1M 输出 token
COST_CHARS_PER_TOKEN = 2             # 中文约 2 字符/token
COST_LOG_FILE = os.path.join(STATE_DIR, "cost_log.json")
COST_LOOKBACK_DAYS = 30
COST_BUDGET_DEFAULT = 50.0           # 月度成本预算（¥）；超则仅告警不阻断


def log_run_cost(chars_in=0, chars_out=0, tag="generate_report"):
    """把一次运行的估算成本追加到 state/cost_log.json（滚动保留 60 天）。

    仅做本地记账，不调任何外部服务；用于月度自检与资源监控。
    """
    try:
        today = date_str()
        log = load_json(COST_LOG_FILE, [])
        cost = (chars_in / COST_CHARS_PER_TOKEN) * COST_INPUT_RATE \
             + (chars_out / COST_CHARS_PER_TOKEN) * COST_OUTPUT_RATE
        log.append({"date": today, "tag": tag, "chars_in": int(chars_in),
                    "chars_out": int(chars_out), "cost": round(cost, 6)})
        # 滚动裁剪：仅保留最近 60 天
        cutoff = days_ago_iso(60)[:10]
        log = [r for r in log if r.get("date", "") >= cutoff]
        save_json(COST_LOG_FILE, log)
    except Exception:
        pass


# ---------- 内容源静态相关度（启发式，零 LLM 成本） ----------

SOURCE_RELEVANCE_KEYWORDS = (
    "副业", "独立开发", "创业", "赚钱", "增长", "运营", "变现", "流量",
    "产品", "项目", "个体", "自由职业", "小本", "轻资产",
    "indie", "hacker", "startup", "side", "growth", "maker", "product",
    "saas", "developer", "创业者", "生财", "搞钱",
)


def heuristic_relevance(name, url=""):
    """从源名称/域名启发式估计「副业主题相关度」(0.0~1.0)，无需 LLM。

    - 命中关键词越多越相关；基线 0.4（默认中等相关），上限 1.0。
    - 这是「相关」维度的静态兜底；sources.json 里可放 curated 的 relevance(1-5) 覆盖。
    """
    text = (str(name) + " " + str(url)).lower()
    hits = sum(1 for kw in SOURCE_RELEVANCE_KEYWORDS if kw.lower() in text)
    return min(1.0, 0.4 + 0.12 * hits)
