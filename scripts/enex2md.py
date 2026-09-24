#!/usr/bin/env python3
"""Convert an Evernote / 印象笔记 ENEX archive (incl. ENC0-encrypted notes) to Markdown.

Usage:
    enex2md.py <archive.notes|.enex> --password <pw> --out <dir> [--json]

Attachments are written to <out>/附件/ and referenced with relative links.
Notes that carry `<content encoding="base64:aes">` (Evernote ENC0) are decrypted
with the passphrase you supply. Without a passphrase use crack2md.py instead —
the desktop client's local plaintext cache is the preferred route.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import mimetypes
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enc0 import Enc0

# bs4 / markdownify are imported lazily inside enml_to_markdown(), so that the
# analysis paths (crack2md.py --check, find_store.py) keep working on a machine
# where only the crypto dependency is installed.

CST = timezone(timedelta(hours=8))
EXTS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def ts_to_local(s: str | None) -> str:
    if not s:
        return ""
    try:
        dt = datetime.strptime(s.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S +0800")
    except Exception:
        return s


def safe_name(name: str, maxlen: int = 80) -> str:
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:maxlen] or "untitled"


def text_of(el, tag) -> str:
    if el is None:
        return ""
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


# --------------------------------------------------------------------------- #
# ENML -> Markdown
# --------------------------------------------------------------------------- #
def enml_to_markdown(enml: str, att_map: dict, attachments: list) -> str:
    """enml: decrypted ENML document. att_map: resource md5 -> relative path."""
    try:
        from bs4 import BeautifulSoup, NavigableString
        from markdownify import markdownify as md_convert
    except ImportError as e:  # noqa: BLE001
        raise SystemExit(
            f"缺少依赖 {e.name}。请先安装：\n"
            f"  python3 -m pip install -r requirements.txt\n"
            f"（或 pip install beautifulsoup4 lxml markdownify）") from e
    soup = BeautifulSoup(enml, "lxml-xml")
    root = soup.find("en-note") or soup

    # ---- en-todo -> checkbox text
    for t in root.find_all("en-todo"):
        checked = str(t.get("checked", "false")).lower() in ("true", "1", "yes")
        t.replace_with(NavigableString("☑ " if checked else "☐ "))

    # ---- en-crypt (inline encrypted text we cannot open) -> placeholder
    for t in root.find_all("en-crypt"):
        t.replace_with(NavigableString("`[加密文本，需原口令解密]`"))

    # ---- en-media -> real markdown link/image
    for m in root.find_all("en-media"):
        h = (m.get("hash") or "").lower()
        mime = m.get("type") or ""
        rel = att_map.get(h)
        if rel:
            name = os.path.basename(rel)
            title = m.get("title") or ""
            if mime.startswith("image/"):
                repl = soup.new_tag("img", src=rel, alt=name)
            else:
                repl = soup.new_tag("a", href=rel)
                repl.string = name
        else:
            repl = NavigableString(f"`[缺失附件 {h[:12]}]`")
        m.replace_with(repl)

    # ---- unknown en-* leftovers: keep their inner text
    for t in root.find_all(lambda tag: tag.name and tag.name.startswith("en-")):
        t.unwrap()

    # ---- <div> is Evernote's line container: make it a paragraph
    for d in root.find_all("div"):
        d.name = "p"

    body = "".join(str(c) for c in root.contents)
    text = md_convert(
        body,
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        newline_style="SPACES",
        strip=["span", "font", "center"],
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# main conversion
# --------------------------------------------------------------------------- #
def convert(archive: str, password: str, outdir: str, verbose: bool = True):
    tree = ET.parse(archive)
    root = tree.getroot()
    notes = root.findall("note")
    note_dir = outdir
    att_dir = os.path.join(outdir, "附件")
    os.makedirs(att_dir, exist_ok=True)

    used, index, warnings = set(), [], []
    width = len(str(len(notes)))
    stats = {"notes": len(notes), "converted": 0, "attachments": 0,
             "encrypted": 0, "decrypt_failed": 0, "media_ok": 0,
             "media_missing": 0, "warnings": warnings, "unmatched": []}

    for i, note in enumerate(notes, 1):
        title = text_of(note, "title") or f"未命名笔记{i}"
        created = ts_to_local(text_of(note, "created"))
        updated = ts_to_local(text_of(note, "updated"))

        # --- resources (attachments) -------------------------------------- #
        att_map, att_files = {}, []
        for j, res in enumerate(note.findall("resource"), 1):
            data_el = res.find("data")
            if data_el is None or not (data_el.text or "").strip():
                continue
            try:
                raw = base64.b64decode("".join((data_el.text or "").split()))
            except Exception as e:
                warnings.append(f"[{title}] 附件 {j} base64 解码失败: {e}")
                continue
            mime = text_of(res, "mime") or "application/octet-stream"
            digest = hashlib.md5(raw).hexdigest()
            ra = res.find("resource-attributes")
            orig = text_of(ra, "file-name") if ra is not None else ""

            if orig:
                stem, ext = os.path.splitext(safe_name(orig))
            else:
                stem, ext = f"note{i:0{width}d}_attachment{j}", ""
            ext = ext or EXTS.get(mime, mimetypes.guess_extension(mime) or ".bin")
            if not ext.startswith("."):
                ext = "." + ext
            fname = safe_name(stem) + ext
            k = 1
            while fname.lower() in used:
                fname = f"{safe_name(stem)}_{k}{ext}"
                k += 1
            used.add(fname.lower())
            with open(os.path.join(att_dir, fname), "wb") as f:
                f.write(raw)
            att_map[digest] = f"附件/{fname}"
            att_files.append(
                {"file": fname, "mime": mime, "bytes": len(raw), "md5": digest}
            )

        # --- content ------------------------------------------------------ #
        c = note.find("content")
        raw_b64 = (c.text or "") if c is not None else ""
        encoding = (c.get("encoding") or "") if c is not None else ""
        if encoding == "base64:aes":
            stats["encrypted"] += 1
            try:
                enml = Enc0.from_b64(raw_b64).decrypt(password).decode("utf-8", "replace")
            except Exception as e:
                enml = ""
                warnings.append(f"[{title}] 解密失败: {e}")
                stats["decrypt_failed"] += 1
                index.append({"i": i, "title": title, "created": created,
                              "updated": updated, "ok": False, "att": att_files, "file": ""})
                continue
        elif encoding == "base64":
            enml = base64.b64decode("".join(raw_b64.split())).decode("utf-8", "replace")
        else:
            enml = html.unescape(raw_b64)

        # attachments referenced from the body must resolve to a <resource>
        refs = {h.lower() for h in re.findall(r'en-media[^>]*hash="([0-9a-fA-F]+)"', enml)}
        miss = refs - set(att_map)
        stats["media_ok"] += len(refs) - len(miss)
        stats["media_missing"] += len(miss)
        if miss:
            warnings.append(f"[{title}] 引用附件缺少二进制: {sorted(miss)}")

        body = enml_to_markdown(enml, att_map, att_files)

        # --- note attributes --------------------------------------------- #
        na = note.find("note-attributes")
        meta = {}
        if na is not None:
            for k in ("author", "source", "source-url", "content-class", "latitude", "longitude"):
                v = text_of(na, k)
                if v:
                    meta[k] = v

        fm = ["---", f"title: {title}", f"created: {created}", f"updated: {updated}"]
        if "author" in meta:
            fm.append(f"author: {meta['author']}")
        if "source-url" in meta:
            fm.append(f"source: {meta['source-url']}")
        if meta.get("latitude") and meta.get("longitude"):
            fm.append(f"location: {meta['latitude']},{meta['longitude']}")
        fm.append(f"attachments: {len(att_files)}")
        fm.append("---")

        fname_md = f"{i:0{width}d}-{safe_name(title, 60)}.md"
        path = os.path.join(note_dir, fname_md)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(fm) + "\n\n")
            f.write(f"# {title}\n\n")
            f.write(body + "\n")
            if att_files:
                f.write("\n## 附件\n\n")
                for a in att_files:
                    f.write(f"- [{a['file']}](附件/{a['file']})  \n")
                    f.write(f"  `{a['mime']}` · {a['bytes']/1024:.1f} KB · md5 `{a['md5'][:16]}`\n")
        index.append({"i": i, "title": title, "created": created, "updated": updated,
                      "ok": True, "att": att_files, "file": fname_md})
        print(f"  [{i}/{len(notes)}] {fname_md}  (正文 {len(body)} 字符, 附件 {len(att_files)})")

    # --- index ------------------------------------------------------------ #
    lines = ["---", "title: 索引", "---", "", "# 归档索引", "",
             f"- 来源：`{os.path.basename(archive)}`",
             f"- 导出时间：{ts_to_local(root.get('export-date'))}",
             f"- 笔记数：{len(notes)}",
             f"- 附件数：{len(used)}",
             f"- 转换时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S +0800')}",
             "", "| # | 标题 | 创建时间 | 附件 |", "|---|---|---|---|"]
    for e in index:
        t = e["title"].replace("|", "\\|")
        link = f"[{t}]({e['file']})" if e["file"] else f"{t}（未转换）"
        lines.append(f"| {e['i']} | {link} | {e['created']} | {len(e['att'])} |")
    with open(os.path.join(note_dir, "00-索引.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    if warnings:
        with open(os.path.join(note_dir, "转换警告.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(warnings) + "\n")
        print(f"\n{len(warnings)} 条警告 -> 转换警告.log")
    stats["converted"] = sum(1 for e in index if e["ok"])
    stats["attachments"] = len(used)
    stats["unmatched"] = [e["title"] for e in index if not e["ok"]]
    stats["password_used"] = bool(password)
    print(f"\n完成：{outdir}\n  md {stats['converted']} 篇 / 附件 {len(used)} 个")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ENEX 存档 -> Markdown（口令解密路径）")
    ap.add_argument("archive")
    ap.add_argument("--password", default="",
                    help="加密笔记的口令（明文存档可省略）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    a = ap.parse_args()
    st = convert(a.archive, a.password, a.out, verbose=not a.json)
    if a.json:
        import json
        print(json.dumps({"schema": "evernote-enex-to-md/report/1", **st},
                         ensure_ascii=False, indent=2))
    if st["decrypt_failed"]:
        print(f"\n{st['decrypt_failed']} 篇解密失败：口令不对，或缺口令。"
              "换用本机客户端明文缓存（crack2md.py）往往更快。", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
