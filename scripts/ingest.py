#!/usr/bin/env python3
"""把「别的搜索工具返回的结果」规范成本项目能读的标准存档。

## 为什么要它
本项目的深读层只认一种输入：`{"results": [...]}`。而各家搜索结果长得都不一样——

    Tavily   : results[].title / url / content / score / published_date
    Exa      : results[].title / url / text / publishedDate / highlights[]
    Serper   : organic_results[].title / link / snippet / date
    自建脚本 : 可能是 {"data": [...]}、{"items": [...]}，甚至只有 url + 正文

没有这层适配，每接一个新来源都要改深读代码。有了它，**只需给一张字段映射表**。

## 用法
    # 先看这个文件长什么样、自动认出了哪些字段
    python ingest.py raw.json --list

    # 自动映射（认得常见字段名），直接转
    python ingest.py raw.json --query "..." --out 存档.json

    # 字段名太怪，手动指定（左=标准名，右=原始名）
    python ingest.py raw.json --map title=name,url=link,snippet=abstract \
        --query "..." --engine tavily --out 存档.json

## 它不做什么
- 不抓网页（要正文请用 search.py --need-content 或自己的抓取器）
- 不改语义（原文照搬，不摘要、不重排、不打分；缺的字段留空，展示时显示 —）
- 不猜 URL（没有 url 的条目会被跳过并报数，避免造出不可回查的条目）

## 输出
标准存档（schema=search-archive/1），`read.py` / `dump.py` / `search.py --replay` /
`merge.py` 都能直接读，编号口径与原生检索一致。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# 标准字段 ← 各来源常见命名（按优先级尝试）
AUTO_MAP: dict[str, tuple[str, ...]] = {
    "title": ("title", "name", "headline", "headline_text", "display_title"),
    "url": ("url", "link", "href", "uri", "source_url", "page_url"),
    "snippet": ("snippet", "content", "text", "description", "summary", "body", "highlights"),
    "summary": ("summary", "content", "text"),
    "date": ("date", "published_date", "publishedDate", "published_at", "page_age", "age"),
    "site_name": ("site_name", "source", "domain", "host", "website"),
    "authority": ("authority",),
    "rerank_score": ("rerank_score", "score", "relevance", "similarity"),
}

# 常见的结果数组容器键名
LIST_KEYS = ("results", "data", "items", "organic_results", "documents",
             "hits", "docs", "entries", "pages", "web")


# --------------------------------------------------------------------------- #
# 读原始文件
# --------------------------------------------------------------------------- #
def load_payload(path: Path) -> tuple[list[dict[str, Any]], str]:
    """返回 (条目列表, 格式说明)。支持 .json / .jsonl / 裸数组 / 带容器的对象。"""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise SystemExit(f"文件是空的: {path}")

    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows, "jsonl"

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"不是合法 JSON: {exc}\n"
            "如果是手贴的正文，先存成 .txt 用 save_yuanbao.py --from-file，或自己包成 "
            '{"results": [{"title": "...", "url": "...", "snippet": "..."}]}'
        )

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], "json-list"

    if isinstance(payload, dict):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and value:
                return [x for x in value if isinstance(x, dict)], f"json[{key}]"
        # 兜底：取第一个「元素是对象的列表」的键
        for key, value in payload.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [x for x in value if isinstance(x, dict)], f"json[{key}]"
    raise SystemExit("没找到结果列表。用 --list 看文件结构，或先包成 {\"results\": [...]}")


# --------------------------------------------------------------------------- #
# 字段映射
# --------------------------------------------------------------------------- #
def parse_map(raw: str | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for pair in raw.split(","):
        if not pair.strip():
            continue
        if "=" not in pair:
            raise SystemExit(f"--map 需要 key=value 形式，收到: {pair}")
        std, src = pair.split("=", 1)
        std, src = std.strip(), src.strip()
        if std not in AUTO_MAP:
            raise SystemExit(f"不认识的标准字段: {std}（可用: {', '.join(AUTO_MAP)}）")
        mapping[std] = src
    return mapping


def pick(item: dict[str, Any], candidates: Iterable[str]) -> Any:
    for name in candidates:
        if name in item and item[name] not in (None, "", [], {}):
            return item[name]
    return None


def flatten(value: Any) -> Any:
    """把列表型字段（如 Exa 的 highlights）拼成字符串。"""
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return value


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize(items: list[dict[str, Any]], mapping: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """逐条映射成标准字段。返回 (结果, 跳过条数)。"""
    rows: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        def value_of(std: str) -> Any:
            # 手动映射优先，其次按内置候选列表自动识别
            if std in mapping:
                return flatten(item.get(mapping[std]))
            return flatten(pick(item, AUTO_MAP[std]))

        url = str(value_of("url") or "").strip()
        if not url:
            skipped += 1          # 没有 URL 的条目不可回查，直接跳过而不是编一个
            continue

        host = ""
        m = re.match(r"https?://([^/]+)", url)
        if m:
            host = re.sub(r"^www\d?\.", "", m.group(1).lower())

        rows.append({
            "title": str(value_of("title") or host or url)[:300],
            "url": url,
            "site_name": str(value_of("site_name") or host),
            "date": str(value_of("date") or "")[:10],
            "authority": to_float(value_of("authority")),
            "rerank_score": to_float(value_of("rerank_score")),
            "snippet": str(value_of("snippet") or ""),
            "summary": str(value_of("summary") or ""),
            "rank": len(rows) + 1,     # 保留原始顺序即排名
        })
    return rows, skipped


# --------------------------------------------------------------------------- #
# 写标准存档
# --------------------------------------------------------------------------- #
def write_archive(out: Path, rows: list[dict[str, Any]], query: str,
                  engine: str, source: Path, source_format: str,
                  mapping: dict[str, str], skipped: int) -> None:
    now = datetime.now().astimezone()
    record = {
        "schema": "search-archive/1",
        "engine": engine,
        "created_at": now.isoformat(timespec="seconds"),
        "query_raw": query,
        "query_final": query,
        "endpoint": f"ingested from {source.name}",
        "request": {},
        "elapsed_ms": None,
        "api_returned": len(rows),
        "kept_after_local_filter": len(rows),
        "filters": {"min_authority": 0},
        "results": rows,
        "provenance": {
            "source_file": str(source),
            "source_format": source_format,
            "skipped_no_url": skipped,
            "mapping": mapping or "auto",
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # 与 search.py / save_yuanbao.py 共用一个 index.jsonl，便于按 query 回溯
    index = out.parent.parent / "index.jsonl"
    try:
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": record["created_at"],
                "engine": engine,
                "query": query,
                "count_requested": None,
                "api_returned": len(rows),
                "kept": len(rows),
                "min_authority": 0,
                "call_id": None,
                "file": str(out),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass          # index 写不了不影响主产物


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(
        description="把其他搜索工具的结果规范成标准存档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python ingest.py raw.json --list\n"
               "  python ingest.py raw.json --out 存档.json --query \"...\" --engine tavily\n",
    )
    p.add_argument("raw", help="原始结果文件（.json / .jsonl）")
    p.add_argument("--out", metavar="PATH", help="输出存档路径")
    p.add_argument("--query", default="", help="这一批对应的查询词（写进存档，便于回溯）")
    p.add_argument("--engine", default="ingested", help="来源标签，如 tavily / exa / serper")
    p.add_argument("--map", help="字段映射，如 title=name,url=link,snippet=abstract")
    p.add_argument("--list", action="store_true", help="只打印文件结构与自动识别的映射，不写文件")
    p.add_argument("--dry-run", action="store_true", help="解析但不写盘，打印统计")
    args = p.parse_args(argv)

    path = Path(args.raw).expanduser()
    if not path.is_file():
        raise SystemExit(f"文件不存在: {path}")

    items, source_format = load_payload(path)
    mapping = parse_map(args.map)

    if args.list:
        print(f"文件: {path}")
        print(f"格式: {source_format} | 条目: {len(items)}")
        if items:
            print(f"首条字段: {', '.join(items[0].keys())}")
            print("\n自动识别结果：")
            for std in AUTO_MAP:
                got = mapping.get(std) or pick(items[0], AUTO_MAP[std])
                shown = "<空>" if got in (None, "", [], {}) else str(flatten(got))[:60]
                print(f"  {std:<13} ← {shown}")
            print("\n字段名对不上就用 --map 指定，例如 --map snippet=abstract")
        return 0

    rows, skipped = normalize(items, mapping)
    if not rows:
        raise SystemExit(
            f"一条都没转换成功（读入 {len(items)} 条，跳过 {skipped} 条）。\n"
            "最常见原因：条目里没有 url 字段。用 --list 看字段名，再用 --map url=... 指定。"
        )

    print(f"读入 {len(items)} 条 → 保留 {len(rows)} 条"
          + (f"（跳过 {skipped} 条：缺 url）" if skipped else ""))
    if args.dry_run or not args.out:
        print("（未写盘；加 --out 存档.json）")
        print("前 3 条：")
        for row in rows[:3]:
            print(f"  #{row['rank']} {row['title'][:50]} | {row['site_name']} | {len(row['snippet'])} 字符")
        return 0

    out = Path(args.out).expanduser()
    write_archive(out, rows, args.query, args.engine, path, source_format, mapping, skipped)
    print(f"saved: {out}")
    print(f"下一步: python read.py \"{out}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
