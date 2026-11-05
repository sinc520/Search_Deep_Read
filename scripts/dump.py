#!/usr/bin/env python3
"""从搜索存档（任意含 results 数组的 JSON）里提取字段，或统计分布。

两个用途：
1. 给子智能体读全部 50 条 —— 字段白名单在这里定义，指令里不必重复写字段名
2. 给主轨看站点 / 权威分布 —— 判断主轨那 10 条是否同质化

只输出事实，不输出"该派 / 不该派"的结论。阈值是拍出来的，判断权归主轨。

用法：
    python scripts/dump.py <存档>                    # 全部条目，默认七字段
    python scripts/dump.py <存档> --limit 10          # 只前 10 条
    python scripts/dump.py <存档> --ids 3,7,12        # 只指定编号
    python scripts/dump.py <存档> --stats             # 站点/权威分布 + 疑似重复
    python scripts/dump.py <存档> --stats --limit 10  # 只看主轨那 10 条的分布
    python scripts/dump.py <存档> --format json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# 字段白名单：改这里即可，不必同步修改 references/subagent-prompt.md 里的指令文本
DEFAULT_FIELDS = ["index", "authority", "rerank_score", "site_name", "date", "title", "snippet"]
FIELD_ALIASES = {"idx": "index", "n": "index", "rerank": "rerank_score", "site": "site_name"}

# 判重时比较的标题前缀长度（归一化后的字符数）。
# 定 60 的理由：实测 50 字在 100 条样本里零误报；但搜索引擎常在标题尾部追加
# "PubMed 2026 Sep 16 917666178" / "Research Article" / "DOAJ" 这类站点后缀，
# 全串比对会失效。60 既覆盖后缀噪声，又足够长到不会把同主题的不同论文并成一条。
TITLE_KEY_LEN = 60

# 标题里常见的镜像站前缀，判重前先剥掉，否则同一篇论文会被当成两条
TITLE_PREFIXES = ("javascript disabled - ", "javascript is disabled - ", "[pdf] ", "[html] ",
                  "pdf: ", "epub: ", "(pdf) ")

# DOI 是最可靠的同一性判据：同一 DOI 的不同 host / 路径变体都是同一篇
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#&]+", re.I)



def norm_text(value: Any) -> str:
    """折叠空白，供展示用。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def title_key(title: Any) -> str:
    """标题归一化：剥镜像前缀、去标点、转小写。返回全串，不截断。"""
    text = str(title or "").strip()
    lowered = text.lower()
    for prefix in TITLE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())


def titles_match(a: str, b: str) -> bool:
    """比前 TITLE_KEY_LEN 个归一化字符；短于该长度的要求全等。

    不做全串比对：搜索引擎普遍在标题尾部追加站点后缀（"PubMed 2026 Sep 16…"、
    "Research Article"、"- DOAJ"），全串相等会把同一篇论文判成两条。
    """
    if not a or not b:
        return False
    if len(a) < TITLE_KEY_LEN or len(b) < TITLE_KEY_LEN:
        return a == b
    return a[:TITLE_KEY_LEN] == b[:TITLE_KEY_LEN]


def doi_key(url: Any) -> str:
    """从 URL 里抽 DOI；抽不到返回空串。"""
    match = DOI_RE.search(str(url or ""))
    return match.group(0).rstrip(".,;").lower() if match else ""


def url_key(url: Any) -> str:
    """URL 归一化：去协议、去查询串、去锚点，去掉 2 字母语言段。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw).split("#")[0].split("?")[0].rstrip("/")
    raw = re.sub(r"/(?:zh|en|de|fr|it|es|ja|ko|ru|pt|nl)(?=/|$)", "", raw, count=1)
    return raw.lower()


# URL 前缀判重的最短长度：短于这个长度容易把同站不同栏目误并
URL_PREFIX_MIN = 30


def urls_related(a: str, b: str) -> bool:
    """同一资源的路径变体：/manuscript/x、/manuscript/x/v1/download、/doi/x、/doi/epdf/x。

    只在较短者足够长时才认，避免把 example.com/a 与 example.com/abc 判成一条。
    """
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= URL_PREFIX_MIN and long.startswith(short)


def site_key(entry: dict[str, Any]) -> str:
    """站点名；为空时退化为域名。"""
    name = norm_text(entry.get("site_name"))
    if name:
        return name
    host = re.sub(r"^https?://", "", str(entry.get("url") or "")).split("/")[0]
    return host.replace("www.", "") or "未知"


def dedup_url(value: Any) -> str:
    """去重键。**必须与 search.py 的 clean_url 保持完全一致**。

    两边各写一份是有意为之的权衡：抽公共模块要改两个文件的 import 路径，
    而这份去重规则改动频率极低。若哪天要改，两处必须同步，否则
    dump 的编号会和 search.py --expand 的编号错位——编号一旦错位，
    "看第 12 条"这种指令就会指向另一条，整个协作约定失效。
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    encoded = urlencode(query)
    if len(encoded) > 120:
        encoded = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, ""))


def dedup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """与 search.py 的 --dedup 默认行为一致：按清洗后的 URL 去重，保留首次出现。"""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = dedup_url(row.get("url")) or str(row.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def load_results(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise SystemExit(f"存档不存在: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"读取存档失败: {exc}")
    rows = [r for r in (record.get("results") or []) if isinstance(r, dict)]
    if not rows:
        raise SystemExit(f"存档里没有 results: {path}")
    return record, rows


def select(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """按 --ids / --limit 选区，返回带 1 基编号的副本。"""
    picked: list[tuple[int, dict[str, Any]]] = list(enumerate(rows, 1))
    if args.ids:
        wanted = set()
        for part in args.ids.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                wanted.update(range(int(lo), int(hi) + 1))
            else:
                wanted.add(int(part))
        picked = [(i, r) for i, r in picked if i in wanted]
    if args.limit and args.limit > 0:
        picked = picked[: args.limit]
    out = []
    for idx, row in picked:
        item = dict(row)
        item["index"] = idx
        out.append(item)
    return out


def display_title(item: dict[str, Any]) -> str:
    """标题为空时退回 URL 末段，否则跨条目引用时只能手抄 URL。"""
    title = norm_text(item.get("title"))
    if title:
        return title[:96]
    url = str(item.get("url") or "").rstrip("/")
    tail = url.rsplit("/", 1)[-1] if url else ""
    return f"（无标题）{tail or '#' + str(item.get('index'))}"[:96]


def to_text(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        # 分数带上标签：只有一个裸数字时，读者分不清是相关度还是别的合成分
        head = (
            f"{item['index']:>2}. [auth={_as_num(item.get('authority')):.0f} "
            f"rerank={_as_num(item.get('rerank_score')):.2f}] "
            f"{site_key(item)} | {norm_text(item.get('date'))[:10]} | "
            f"{display_title(item)}"
        )
        lines.append(head)
        snippet = norm_text(item.get("snippet"))
        if snippet:
            lines.append(f"    {snippet}")
        url = str(item.get("url") or "").strip()
        if url:
            lines.append(f"    {url}")
    return "\n".join(lines)


def _as_num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def build_stats(items: list[dict[str, Any]], total: int, scope: str = "") -> str:
    """只给事实：站点分布、权威分布、疑似重复。不给结论。"""
    sites = Counter(site_key(r) for r in items)
    auth: Counter[str] = Counter()
    for r in items:
        value = _as_num(r.get("authority"))
        if value < 0:
            auth["未知"] += 1
        elif value >= 4:
            auth["=4"] += 1
        elif value >= 3:
            auth["=3"] += 1
        else:
            auth["<=2"] += 1

    lines = [f"样本: {len(items)} 条{scope}"]
    top = " | ".join(f"{name} {cnt}" for name, cnt in sites.most_common(8))
    lines.append(f"站点: {top}")
    lines.append(
        "权威: " + " | ".join(f"{k}: {auth[k]}" for k in ("=4", "=3", "<=2", "未知") if auth[k])
    )

    groups: list[str] = []
    seen_doi: dict[str, int] = {}
    seen_url: dict[str, int] = {}
    seen_urls: list[tuple[str, int]] = []
    seen_titles: list[tuple[str, int]] = []
    for r in items:
        idx = r["index"]
        doi, url, title = doi_key(r.get("url")), url_key(r.get("url")), title_key(r.get("title"))
        hit = None
        if doi and doi in seen_doi:
            hit = f"#{seen_doi[doi]}/#{idx} 同DOI"
        elif url and url in seen_url:
            hit = f"#{seen_url[url]}/#{idx} 同URL"
        elif url:
            for other, other_idx in seen_urls:
                if urls_related(url, other):
                    hit = f"#{other_idx}/#{idx} 同URL变体"
                    break
        if not hit:
            for other, other_idx in seen_titles:
                if titles_match(title, other):
                    hit = f"#{other_idx}/#{idx} 同标题"
                    break
        if hit:
            groups.append(hit)
        if doi:
            seen_doi.setdefault(doi, idx)
        if url:
            seen_url.setdefault(url, idx)
            seen_urls.append((url, idx))
        if len(title) >= TITLE_KEY_LEN:
            seen_titles.append((title, idx))
    lines.append("疑似重复: " + ("；".join(groups) if groups else "无"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从搜索存档提取字段或统计分布",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("archive", help="存档 JSON 路径")
    p.add_argument("--limit", type=int, default=0, help="只取前 N 条，0=全部（默认）")
    p.add_argument("--ids", help="只取指定编号，如 3,7 或 1-20")
    p.add_argument("--fields", help=f"覆盖默认字段，逗号分隔；默认 {','.join(DEFAULT_FIELDS)}")
    p.add_argument("--stats", action="store_true", help="只输出分布统计，不输出条目")
    p.add_argument("--all", action="store_true", help="不做权威过滤，按存档原始顺序编号")
    p.add_argument("--no-dedup", dest="no_dedup", action="store_true",
                   help="关闭 URL 去重（默认与 search.py 一致：去重）")
    p.add_argument("--min-authority", dest="min_authority", type=int,
                   help="覆盖存档里记录的 authority 过滤阈值；与 --all 互斥时 --all 优先")
    p.add_argument("--format", choices=["text", "json"], default="text", help="输出格式，默认 text")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    record, raw_rows = load_results(Path(args.archive))

    # 存档里记着当时用的 authority 阈值。必须复现同一套过滤，否则
    # dump.py 数出来的 #12 和主轨 / search.py 看到的 #12 不是同一条。
    recorded = int((record.get("filters") or {}).get("min_authority") or 0)
    if args.all:
        threshold = 0
    elif args.min_authority is not None:
        threshold = args.min_authority
    else:
        threshold = recorded

    rows = [r for r in raw_rows if _as_num(r.get("authority")) >= threshold] if threshold > 0 else raw_rows
    if threshold > 0 and not rows:
        raise SystemExit(f"按 authority>={threshold} 过滤后没有结果，用 --all 看全部 {len(raw_rows)} 条")

    # 再去重，与 search.py 的展示口径完全对齐
    kept_before_dedup = len(rows)
    if not args.no_dedup:
        rows = dedup_rows(rows)

    items = select(rows, args)

    if args.stats:
        bits = [f"存档 {len(raw_rows)} 条"]
        if threshold > 0:
            bits.append(f"authority>={threshold} 后 {kept_before_dedup} 条")
        if kept_before_dedup != len(rows):
            bits.append(f"去重后 {len(rows)} 条")
        print(build_stats(items, len(raw_rows), "（" + "，".join(bits) + "）"))
        return 0

    if args.format == "json":
        fields = [f.strip() for f in args.fields.split(",")] if args.fields else DEFAULT_FIELDS
        shaped = []
        for item in items:
            row = {"index": item["index"], "url": item.get("url", "")}
            for name in fields:
                name = FIELD_ALIASES.get(name, name)
                if name in item and name not in row:
                    row[name] = item[name]
            shaped.append(row)
        print(json.dumps(shaped, ensure_ascii=False, indent=2))
        return 0

    print(to_text(items))
    bits = [f"存档 {len(raw_rows)} 条"]
    if threshold > 0:
        bits.append(f"authority>={threshold} 后 {kept_before_dedup} 条")
    if kept_before_dedup != len(rows):
        bits.append(f"去重后 {len(rows)} 条")
    if len(items) != len(rows):
        bits.append(f"本次展示 {len(items)} 条")
    print("（" + "，".join(bits) + "）", file=sys.stderr)
    query = norm_text(record.get("query_raw"))
    if query:
        print(f"\n（查询: {query}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
