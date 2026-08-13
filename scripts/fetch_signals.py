#!/usr/bin/env python3
"""步骤1：多源采集 + 增量去重 + 60天自动清理。
新增内容源 = 在 scripts/sources.json 里加一条记录（类型可选 rss / reddit_json /
html_zhongnianren / github_readme_diff），主流程无需改动。
"""
import os
import re
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()
UTC = datetime.timezone.utc
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _now_iso():
    return C.beijing_now().isoformat()


def _http_get(url, headers=None, timeout=20):
    import requests
    h = {"User-Agent": "Mozilla/5.0 (compatible; sidehustle-bot/1.0)"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


# ---------------- 解析器 ----------------
def parse_rss(url, name):
    import feedparser
    d = feedparser.parse(url)
    out = []
    for e in d.entries[:30]:
        link = e.get("link") or e.get("id") or ""
        if not link:
            continue
        content = e.get("summary", "")
        if not content and e.get("content"):
            content = e["content"][0].get("value", "")
        pub = e.get("published") or e.get("updated") or _now_iso()
        out.append({
            "id": link,
            "source_name": name,
            "source_url": link,
            "title": (e.get("title") or "").strip(),
            "content": content.strip(),
            "published_at": pub,
        })
    return out


def parse_reddit_json(url, name):
    r = _http_get(url, headers={"User-Agent": "sidehustle-bot/1.0"})
    j = r.json()
    out = []
    for c in j.get("data", {}).get("children", []):
        d = c.get("data", {})
        perm = d.get("permalink", "")
        link = "https://www.reddit.com" + perm if perm else ""
        if not link:
            continue
        created = d.get("created_utc")
        pub = datetime.datetime.fromtimestamp(created, UTC).isoformat() if created else _now_iso()
        out.append({
            "id": link,
            "source_name": name,
            "source_url": link,
            "title": (d.get("title") or "").strip(),
            "content": (d.get("selftext") or "").strip(),
            "published_at": pub,
        })
    return out


def parse_html_zhongnianren(url, name):
    from bs4 import BeautifulSoup
    r = _http_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.find_all("a", href=re.compile(r"^/posts/")):
        href = a["href"]
        source_url = url.rstrip("/") + href
        container = a.find_parent(["li", "article", "div", "section"]) or a.parent
        text = container.get_text(" ", strip=True) if container else a.get_text(" ", strip=True)
        if not text:
            continue
        # 发布时间：同容器内带 ISO 时间戳的 title 属性
        pub = _now_iso()
        for el in (container.find_all(True) if container else [a]):
            t = el.get("title", "")
            m = ISO_RE.search(t)
            if m:
                pub = m.group(0) + ("" if "Z" in t or "+" in t else "")
                break
        out.append({
            "id": source_url,
            "source_name": name,
            "source_url": source_url,
            "title": text[:120],
            "content": text,
            "published_at": pub,
        })
    return out


def parse_github_readme_diff(cfg):
    """对比 README 快照，只取新增的项目条目；冷启动仅播种不产出。"""
    name = cfg["name"]
    url = cfg["url"]
    snap_path = os.path.join(C.REPO_ROOT, cfg["snapshot"])
    try:
        cur = _http_get(url).text
    except Exception as e:
        LOG.warning("github_readme_diff 抓取失败 %s: %s", name, e)
        return []
    old = ""
    if os.path.exists(snap_path):
        with open(snap_path, "r", encoding="utf-8") as f:
            old = f.read()
    # 写回最新快照
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(cur)
    if not old.strip():
        LOG.info("%s 冷启动：已播种快照，今日不产出（从明天起增量）", name)
        return []
    old_lines = set(l.strip() for l in old.splitlines())
    new_lines = [l.strip() for l in cur.splitlines()
                 if l.strip() and l.strip() not in old_lines and "http" in l]
    out = []
    for line in new_lines[:40]:
        m = re.search(r"https?://[^\s\)\]]+", line)
        link = m.group(0) if m else cfg.get("anchor", url)
        # 名称：取第一个 markdown 链接文字或行首文字
        nm = re.search(r"\[([^\]]+)\]", line)
        title = nm.group(1) if nm else line.split("http")[0].strip(" -[]#")
        out.append({
            "id": "cid:" + (link or line)[:80],
            "source_name": name,
            "source_url": link,
            "title": title[:120],
            "content": line,
            "published_at": _now_iso(),
        })
    return out


PARSERS = {
    "rss": parse_rss,
    "reddit_json": parse_reddit_json,
    "html_zhongnianren": parse_html_zhongnianren,
    "github_readme_diff": parse_github_readme_diff,
}


def cleanup():
    """删除 RETENTION_DAYS 天前的数据文件，并裁剪去重表。"""
    import glob
    cutoff = C.beijing_now() - datetime.timedelta(days=C.RETENTION_DAYS)
    for pat in ("candidates-*.json", "report-*.json"):
        for fp in glob.glob(os.path.join(C.DATA_DIR, pat)):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(fp))
            if not m:
                continue
            try:
                d = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=C.BJ)
            except Exception:
                continue
            if d < cutoff:
                os.remove(fp)
                LOG.info("清理过期文件: %s", os.path.basename(fp))
    seen = C.load_seen()
    if seen:
        kept = {k: v for k, v in seen.items()
                if C.days_ago_iso(C.RETENTION_DAYS) <= (v or "")}
        removed = len(seen) - len(kept)
        if removed:
            C.save_seen(kept)
            LOG.info("去重表裁剪 %d 条（>%d天）", removed, C.RETENTION_DAYS)


def main():
    C.ensure_dirs()
    sources = C.load_json(C.SOURCES_FILE, [])
    seen = C.load_seen()
    cold = len(seen) == 0
    LOG.info("开始采集，源数量=%d，冷启动=%s", len(sources), cold)

    candidates = []
    for cfg in sources:
        stype = cfg.get("type")
        parser = PARSERS.get(stype)
        if not parser:
            LOG.warning("未知源类型 %s，跳过: %s", stype, cfg.get("id"))
            continue
        try:
            items = parser(cfg) if stype == "github_readme_diff" else parser(cfg["url"], cfg["name"])
        except Exception as e:
            LOG.warning("源 %s 抓取失败: %s", cfg.get("id"), e)
            continue
        # 去重（按 id）
        fresh = [it for it in items if it.get("id") and it["id"] not in seen]
        LOG.info("源 %s: 抓到 %d，新增 %d", cfg.get("id"), len(items), len(fresh))
        # 冷启动限流，避免首期爆量
        if cold and stype != "github_readme_diff":
            fresh = fresh[: C.COLD_START_MAX_PER_SOURCE]
        candidates.extend(fresh)

    if cold:
        candidates.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        candidates = candidates[: C.MAX_CANDIDATES]

    # 写入今日候选
    today = C.date_str()
    cand_path = os.path.join(C.DATA_DIR, f"candidates-{today}.json")
    C.save_json(cand_path, candidates)
    LOG.info("候选总数=%d，写入 %s", len(candidates), cand_path)

    # 更新去重表
    for it in candidates:
        seen[it["id"]] = C.beijing_now().isoformat()
    C.save_seen(seen)

    # 清理
    cleanup()
    LOG.info("完成。候选数=%d", len(candidates))


if __name__ == "__main__":
    main()
