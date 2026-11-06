#!/usr/bin/env python3
"""一条命令读一个搜索结果集：清单 + 批次信息 + 按编号追原文。

## 为什么要有它
接入方原本要记两套命令、六个 flag：

    python dump.py <存档> --fields index,title,url,snippet --limit 10
    python search.py --replay <存档> --expand 3,7 --expand-max 1500 --expand-only

现在只要一个路径：

    python read.py <存档>              # 清单（默认前 10 条）
    python read.py <存档> --limit 0    # 全部
    python read.py <存档> --expand 3,7 # 顺手取这几条的完整正文

**编号口径与 dump.py / search.py --replay 完全一致**（存档记录的 authority 过滤 +
URL 去重后的 1 基序号），因为它直接复用那两个脚本的函数，不另写一套。

## 退出码
0 正常；2 没有结果；1 参数或文件错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 与 dump.py / search.py 共用同一套实现，保证编号与展示口径不漂移
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dump import _as_num, dedup_rows, load_results, select, to_text  # noqa: E402
from search import clean_url, format_expanded, parse_indices, truncate_marked  # noqa: E402

# 机读模式的固定字段：接入方按这个契约取值，不依赖内部实现
JSON_FIELDS = ("index", "title", "url", "site_name", "date",
               "authority", "rerank_score", "snippet")


def short_query(record: dict[str, Any]) -> str:
    """合并存档的 query_raw 形如 "merged: a.json + b.json"，压缩成「合并 N 份」。"""
    raw = str(record.get("query_raw") or "").strip()
    if raw.startswith("merged"):
        parts = [p for p in raw.split("+") if p.strip()]
        return f"合并 {len(parts)} 份存档"
    return raw


def engine_composition(rows: list[dict[str, Any]], record: dict[str, Any]) -> str:
    """从 _source_file 前缀或存档的 engine 字段推断批次构成。"""
    counts: dict[str, int] = {}
    for row in rows:
        src = str(row.get("_source_file") or "")
        if src.startswith("yuanbao"):
            label = "元宝"
        elif src:
            label = "DeepSeek"
        else:
            label = str(record.get("engine") or "未知")
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "空"
    return " / ".join(f"{name} {cnt}" for name, cnt in sorted(counts.items(), key=lambda kv: -kv[1]))


def build_rows(path: Path, show_all: bool, dedup: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    """返回 (record, 原始行, 过滤去重后的行, 过滤说明)。

    顺序必须与 dump.py / search.py 一致：先按存档记录的 authority 阈值过滤，再 URL 去重。
    任何一步换序都会让「第 12 条」指到别的东西。
    """
    record, raw_rows = load_results(path)
    threshold = 0 if show_all else int((record.get("filters") or {}).get("min_authority") or 0)

    rows = [r for r in raw_rows if _as_num(r.get("authority")) >= threshold] if threshold > 0 else raw_rows
    if threshold > 0 and not rows:
        raise SystemExit(f"按 authority>={threshold} 过滤后没有结果（存档共 {len(raw_rows)} 条），加 --all 看全部")

    before_dedup = len(rows)
    if dedup:
        rows = dedup_rows(rows)

    notes = [f"存档 {len(raw_rows)} 条"]
    if threshold > 0:
        notes.append(f"authority>={threshold} 后 {before_dedup} 条")
    if dedup and before_dedup != len(rows):
        notes.append(f"URL 去重后 {len(rows)} 条")
    return record, raw_rows, rows, "，".join(notes)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(
        description="读一个搜索结果集：清单 + 批次信息 + 按编号追原文",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python read.py 存档.json\n"
               "  python read.py 存档.json --limit 0\n"
               "  python read.py 存档.json --expand 3,7 --expand-max 1200\n",
    )
    p.add_argument("archive", help="存档 JSON 路径（search.py / save_yuanbao.py / merge.py / ingest.py 的产物）")
    p.add_argument("--limit", type=int, default=None,
                   help="展示条数，0=全部（默认 text 模式 10，json 模式全部）")
    p.add_argument("--ids", metavar="3,7", help="只展示这些编号，如 3,7 或 1-5")
    p.add_argument("--expand", metavar="3,7", action="append",
                   help="取这些编号的完整正文（可重复给，也可逗号分隔）")
    p.add_argument("--expand-max", dest="expand_max", type=int, default=0,
                   help="expand 的截断字数，0=不截断（默认）")
    p.add_argument("--all", action="store_true", help="忽略存档记录的 authority 过滤（编号会变，慎用）")
    p.add_argument("--no-dedup", dest="no_dedup", action="store_true", help="关闭 URL 去重（编号会变）")
    p.add_argument("--snippet-max", dest="snippet_max", type=int, default=300,
                   help="清单里每条摘要最多显示多少字符，0=不截断（默认 300；存档永远完整）")
    p.add_argument("--stats", action="store_true", help="只输出分布统计")
    p.add_argument("--format", choices=["text", "json"], default="text", help="json 供程序消费")

    args = p.parse_args(argv)
    # 机读模式默认全量，避免接入方以为「只有 10 条」
    if args.limit is None:
        args.limit = 0 if args.format == "json" else 10
    path = Path(args.archive).expanduser()

    record, raw_rows, rows, filter_note = build_rows(path, args.all, dedup=not args.no_dedup)
    items = select(rows, args)

    if args.stats:
        from dump import build_stats
        print(build_stats(items, len(raw_rows), f"（{filter_note}）"))
        return 0

    if args.format == "json":
        shaped = [{k: item.get(k) for k in JSON_FIELDS} for item in items]
        print(json.dumps(shaped, ensure_ascii=False, indent=2))
        return 0 if items else 2

    # 批次头：让接入方知道「这批多大、从哪来、有没有被裁过」
    query = short_query(record)
    print(f"[批次] {len(rows)} 条（{engine_composition(rows, record)}）"
          f"{' · 查询: ' + query if query else ''}"
          f"{' · 检索日期: ' + str(record.get('search_date')) if record.get('search_date') else ''}")
    print(f"[口径] {filter_note} · 以下编号即过滤去重后的序号，可直接用于 --expand")
    print()

    shown = []
    for item in items:
        copy = dict(item)
        copy["url"] = clean_url(item.get("url"))       # 显示层去掉 utm 等跟踪参数
        if args.snippet_max > 0:                        # 只裁展示层：存档与 --expand 仍是完整正文
            for field in ("snippet", "summary"):
                if copy.get(field):
                    copy[field] = truncate_marked(copy[field], args.snippet_max)
        shown.append(copy)
    print(to_text(shown))

    if items and len(items) < len(rows):
        last = int(items[-1]["index"])
        print(f"\n⚠ 未展示 #{last + 1}–#{len(rows)} 共 {len(rows) - len(items)} 条"
              f"（--limit 0 看全部，或 --expand 定向取）")

    for raw in parse_indices(args.expand):
        print(format_expanded(rows, [raw], args.expand_max))

    print(f"\n[存档] {path}")
    print(f"[追问] python read.py \"{path}\" --expand 3,7   # 换编号即可")
    return 0 if items else 2


if __name__ == "__main__":
    sys.exit(main())
