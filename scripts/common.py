"""副业日报 · 公共常量与工具（被三个脚本共用）"""
import os
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
MAX_PER_SOURCE = 100         # 每个源每次最多保留多少条（RSS 受 Feed 本身条数限制；HTML/Reddit/Diff 类可拉更多）
MAX_CANDIDATES = 600         # 单日候选上限（data/ 已 gitignore，不进 git 历史，仓库容量零影响）

BJ = datetime.timezone(datetime.timedelta(hours=8))


def beijing_now():
    return datetime.datetime.now(BJ)


def date_str(dt=None):
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
