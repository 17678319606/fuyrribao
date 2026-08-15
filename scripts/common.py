"""副业日报 · 公共常量与工具（被三个脚本共用）"""
import os
import re
import json
import logging
import datetime

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
MAX_CANDIDATES = 1500        # 单日候选上限（data/ 已 gitignore，不进 git 历史，仓库容量零影响；随每源上限放宽到 300 同步提高到 1500，避免总量被卡死）

# 渲染器版本：每次修改 publish_wp.py 的排版/结构时 +1。
# 发布到 WP 的文章顶部会写入 <!-- dr-renderer:N --> 标记；
# 定时/宝塔触发时发现线上当日文章版本过旧则强制重渲染，使修复自动落地（无需手动 Run workflow）。
RENDERER_VERSION = 6

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


def should_regenerate(post_content, event_name):
    """决定是否需要对当日文章重生成。

    - 无当日文章 → 生成（True）
    - 线上版本 < 当前版本（含无标记=旧版） → 强制重生成（True），使修复自动落地
    - 版本已最新：
        * schedule（定时） → 跳过（省额度，由累积逻辑增量刷新）
        * workflow_dispatch / 手动 → 覆盖（True）
    """
    if not post_content:
        return True
    v = extract_renderer_version(post_content)
    if v < RENDERER_VERSION:
        return True
    if event_name == "schedule":
        return False
    return True


# ---------- 同日增量累积：保留旧内容 + 追加新内容 ----------

def item_dedup_key(item):
    """稳定的去重键：优先 source_url，其次 title。"""
    if isinstance(item, dict):
        return item.get("source_url") or item.get("title") or id(item)
    return str(item)


MODULES = ("project_opportunities", "growth_operations", "views_insights")


def merge_reports(existing, new):
    """将 new 的条目按去重键并入 existing（existing 在前保留顺序，new 追加去重）。"""
    if not existing:
        return new
    merged = dict(existing)
    merged.setdefault("modules", {})
    for mod in MODULES:
        ex_items = list(existing.get("modules", {}).get(mod, []))
        new_items = new.get("modules", {}).get(mod, [])
        keys = {item_dedup_key(x) for x in ex_items}
        for it in new_items:
            k = item_dedup_key(it)
            if k not in keys:
                ex_items.append(it)
                keys.add(k)
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
