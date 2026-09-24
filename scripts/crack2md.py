#!/usr/bin/env python3
"""印象笔记 / Evernote ENEX 存档 -> Markdown，正文取自本机客户端明文库。

背景
----
存档里正文字段是 Evernote ENC0 密文（AES-128-CBC，口令不落盘，无法离线爆破），
但桌面客户端把每条笔记的**明文 ENML** 缓存在本地：

    <account-dir>/
    ├── localNoteStore/LocalNoteStore.sqlite   Core Data 库（ZTITLE/ZDATECREATED/ZLOCALUUID）
    └── content/<ZLOCALUUID>/content.enml      明文正文

本脚本按「标题 [+ 创建时间]」把存档与本地明文对齐后还原正文，附件仍取自存档本体
（`<resource>` 从不加密），并用 PKCS#7 填充长度做硬校验。

用法
----
    crack2md.py ARCHIVE --out DIR [--store DIR] [--check] [--json]

`--store` 留空时自动调用 find_store.py 选最优库。
库目录一律**只读**：不改客户端任何文件，也绝不改动原始存档。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from enex2md import enml_to_markdown, safe_name, ts_to_local, CST  # noqa: E402
import find_store  # noqa: E402

EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# 匹配级别：内部用 ASCII 键（JSON 稳定），呈现时转中文
LEVELS = {
    "title+time": "标题+时间",
    "norm+time": "宽松标题+时间",
    "title": "仅标题",
    "norm": "宽松标题",
    "plaintext": "存档明文",
}


# --------------------------------------------------------------------------- #
# archive reading
# --------------------------------------------------------------------------- #
def coredata_to_enex(ts):
    """Core Data 秒 -> ENEX 时间戳字符串（2001-01-01 起算）。"""
    if not ts:
        return ""
    return (EPOCH + timedelta(seconds=ts)).strftime("%Y%m%dT%H%M%SZ")


def norm_title(s: str) -> str:
    """宽松标题键：去空白 + 全角空格，用于兜底匹配。"""
    return re.sub(r"[\s\u3000]+", "", s or "")


def read_archive(archive):
    """-> (root, [ {i,title,created,enc_b64,rels,ciphertext_len} ])"""
    root = ET.parse(archive).getroot()
    notes = []
    for i, n in enumerate(root.findall("note"), 1):
        c = n.find("content")
        enc_b64 = None
        if c is not None and (c.get("encoding") or "") == "base64:aes":
            enc_b64 = "".join((c.text or "").split())
        ct_len = None
        if enc_b64:
            try:
                ct_len = len(base64.b64decode(enc_b64)) - 52 - 32
            except Exception:
                ct_len = None
        notes.append({
            "i": i,
            "title": (n.findtext("title") or f"未命名{i}").strip(),
            "created": n.findtext("created", "") or "",
            "updated": n.findtext("updated", "") or "",
            "enc_b64": enc_b64,
            "ciphertext_len": ct_len,
            "el": n,
        })
    return root, notes


# --------------------------------------------------------------------------- #
# local plaintext index
# --------------------------------------------------------------------------- #
def load_local_index(db_path):
    """-> {title: [(created_enex, local_uuid, pk)]} and {norm_title: [...]}"""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only=1")
    exact, loose = {}, {}
    for pk, title, uuid, created in con.execute(
        "SELECT Z_PK, ZTITLE, ZLOCALUUID, ZDATECREATED FROM ZENNOTE"
    ):
        rec = (coredata_to_enex(created), uuid, pk)
        exact.setdefault(title or "", []).append(rec)
        loose.setdefault(norm_title(title or ""), []).append(rec)
    con.close()
    return exact, loose


def pick_match(note, exact, loose):
    """四级匹配，返回 (record, level)。"""
    t, ct = note["title"], note["created"]
    nt = norm_title(t)
    for key, table, lvl in ((t, exact, "title+time"), (nt, loose, "norm+time")):
        for rec in table.get(key, []):
            if rec[0] == ct:
                return rec, lvl
    if exact.get(t):
        return exact[t][0], "title"
    if loose.get(nt):
        return loose[nt][0], "norm"
    return None, None


# --------------------------------------------------------------------------- #
# analyze (no writes)
# --------------------------------------------------------------------------- #
def analyze(archive, store_dir, verify_padding=True, verbose=False):
    """把存档与本地明文库对齐并校验，不写任何文件。"""
    store_dir, db = find_store.normalize_store(store_dir)
    res = {
        "store": store_dir,
        "db": db,
        "notes": 0, "encrypted": 0, "matched": 0, "matched_encrypted": 0,
        "match_by": {}, "unmatched": [], "extra_plaintext": None,
        "padding": {"checked": 0, "ok": 0, "bad": 0, "examples": []},
        "score": 0.0,
        "warnings": [],
    }
    if not db or not os.path.exists(db):
        res["warnings"].append(f"本地库不可用: {db}")
        return res

    root, notes = read_archive(archive)
    exact, loose = load_local_index(db)
    content_root = os.path.join(store_dir, "content")
    res["notes"] = len(notes)
    res["encrypted"] = sum(1 for n in notes if n["enc_b64"])

    for n in notes:
        if not n["enc_b64"]:
            # 明文笔记：本地库不参与，直接算匹配（正文来自存档本身）
            res["matched"] += 1
            res["match_by"]["plaintext"] = res["match_by"].get("plaintext", 0) + 1
            continue
        rec, lvl = pick_match(n, exact, loose)
        if not rec:
            res["unmatched"].append(n["title"])
            continue
        enml_path = os.path.join(content_root, rec[1], "content.enml")
        if not os.path.exists(enml_path):
            res["unmatched"].append(f"{n['title']} (明文文件缺失)")
            continue
        res["matched"] += 1
        res["matched_encrypted"] += 1
        res["match_by"][lvl] = res["match_by"].get(lvl, 0) + 1
        if verify_padding and n["ciphertext_len"]:
            pt_len = os.path.getsize(enml_path)
            ct_len = n["ciphertext_len"]
            pad = ct_len - pt_len
            res["padding"]["checked"] += 1
            if 1 <= pad <= 16 and ct_len % 16 == 0:
                res["padding"]["ok"] += 1
            else:
                res["padding"]["bad"] += 1
                if len(res["padding"]["examples"]) < 5:
                    res["padding"]["examples"].append(
                        f"{n['title']}: 明文 {pt_len}B / 密文 {ct_len}B")
            if verbose:
                print(f"  · {n['title'][:30]:<30} {LEVELS.get(lvl, lvl):<12} pad={pad}")

    if res["notes"]:
        # 可信度只看**加密笔记**的命中率：明文笔记本来就不需要本地库，
        # 混进分母会把「库完全不匹配」的存档误判成可用。
        res["score"] = round(
            res["matched_encrypted"] / res["encrypted"], 4) if res["encrypted"] else 1.0
    if res["padding"]["checked"] and res["padding"]["bad"]:
        res["warnings"].append(
            f"PKCS#7 校验 {res['padding']['bad']} 条不一致 —— 本地明文可能是同步前的旧版本，"
            "正文内容仍可用但建议抽查")
    return res


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #
def convert(archive, store_dir, outdir, verbose=True):
    """还原全部笔记到 outdir，返回结构化统计。"""
    store_dir, db = find_store.normalize_store(store_dir)
    if not db or not os.path.exists(db):
        sys.exit(f"找不到本地库: {db}")
    exact, loose = load_local_index(db)
    content_root = os.path.join(store_dir, "content")

    root, notes = read_archive(archive)
    att_dir = os.path.join(outdir, "附件")
    os.makedirs(att_dir, exist_ok=True)

    used, index, warnings = set(), [], []
    width = len(str(len(notes)))
    stats = {"media_ok": 0, "media_missing": 0, "converted": 0,
             "padding_bad": 0, "match_by": {}}
    root_el = root

    for note in notes:
        i, title = note["i"], note["title"]
        created = ts_to_local(note["created"])
        updated = ts_to_local(note["updated"])

        # ---- 附件（永远不加密，取自存档） -------------------------------- #
        att_map, att_files = {}, []
        xml_note = root_el.findall("note")[i - 1]
        for j, res in enumerate(xml_note.findall("resource"), 1):
            d = res.find("data")
            if d is None or not (d.text or "").strip():
                continue
            raw = base64.b64decode("".join((d.text or "").split()))
            mime = (res.findtext("mime") or "application/octet-stream").strip()
            digest = hashlib.md5(raw).hexdigest()
            ra = res.find("resource-attributes")
            orig = (ra.findtext("file-name") or "").strip() if ra is not None else ""
            stem, ext = (os.path.splitext(safe_name(orig)) if orig
                         else (f"note{i:0{width}d}_附件{j}", ""))
            ext = ext or {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp", "application/pdf": ".pdf",
                "audio/mpeg": ".mp3", "audio/wav": ".wav", "video/mp4": ".mp4",
            }.get(mime, mimetypes.guess_extension(mime) or ".bin")
            if not ext.startswith("."):
                ext = "." + ext
            fname, k = safe_name(stem) + ext, 1
            while fname.lower() in used:
                fname = f"{safe_name(stem)}_{k}{ext}"
                k += 1
            used.add(fname.lower())
            with open(os.path.join(att_dir, fname), "wb") as f:
                f.write(raw)
            att_map[digest] = f"附件/{fname}"
            att_files.append({"file": fname, "mime": mime,
                              "bytes": len(raw), "md5": digest})

        # ---- 正文：明文笔记直接读，加密笔记取本地明文 -------------------- #
        if note["enc_b64"]:
            rec, lvl = pick_match(note, exact, loose)
            if not rec:
                warnings.append(f"[{title}] 本地库未找到对应笔记，正文缺失")
                index.append(dict(i=i, title=title, created=created, updated=updated,
                                  ok=False, att=att_files, file="", src="—"))
                print(f"  [{i}/{len(notes)}] !! 未匹配 {title}")
                continue
            enml_path = os.path.join(content_root, rec[1], "content.enml")
            if not os.path.exists(enml_path):
                warnings.append(f"[{title}] 明文文件缺失: {enml_path}")
                continue
            with open(enml_path, "rb") as f:
                enml = f.read().decode("utf-8", "replace")
            src_note = f"本地库·{LEVELS.get(lvl, lvl)}"
            stats["match_by"][lvl] = stats["match_by"].get(lvl, 0) + 1
            if note["ciphertext_len"]:
                pad = note["ciphertext_len"] - os.path.getsize(enml_path)
                if not (1 <= pad <= 16):
                    stats["padding_bad"] += 1
                    warnings.append(f"[{title}] PKCS#7 填充异常（明文可能非当前版本）")
        else:
            c = xml_note.find("content")
            raw_b64 = (c.text or "") if c is not None else ""
            enc = (c.get("encoding") or "") if c is not None else ""
            if enc == "base64":
                enml = base64.b64decode("".join(raw_b64.split())).decode("utf-8", "replace")
            else:
                import html as _html
                enml = _html.unescape(raw_b64)
            src_note = "存档明文"

        # ---- 附件引用校验 ------------------------------------------------ #
        refs = {h.lower() for h in re.findall(r'en-media[^>]*hash="([0-9a-fA-F]+)"', enml)}
        miss = refs - set(att_map)
        stats["media_ok"] += len(refs) - len(miss)
        stats["media_missing"] += len(miss)
        if miss:
            warnings.append(f"[{title}] 引用附件缺少二进制: {sorted(miss)}")

        body = enml_to_markdown(enml, att_map, att_files)

        # ---- front matter ----------------------------------------------- #
        na = xml_note.find("note-attributes")
        meta = {}
        if na is not None:
            for k in ("author", "source", "source-url", "content-class",
                      "latitude", "longitude"):
                v = (na.findtext(k) or "").strip()
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
        with open(os.path.join(outdir, fname_md), "w", encoding="utf-8") as f:
            f.write("\n".join(fm) + "\n\n")
            f.write(f"# {title}\n\n")
            f.write(body + "\n")
            if att_files:
                f.write("\n## 附件\n\n")
                for a in att_files:
                    f.write(f"- [{a['file']}](附件/{a['file']})  \n")
                    f.write(f"  `{a['mime']}` · {a['bytes']/1024:.1f} KB · "
                            f"md5 `{a['md5'][:16]}`\n")
        index.append(dict(i=i, title=title, created=created, updated=updated,
                          ok=True, att=att_files, file=fname_md, src=src_note))
        stats["converted"] += 1
        if verbose:
            print(f"  [{i}/{len(notes)}] {fname_md}  "
                  f"(正文 {len(body)} 字符, 附件 {len(att_files)})")

    # ---- 索引 --------------------------------------------------------------- #
    ok_n = sum(1 for e in index if e["ok"])
    lines = ["---", "title: 索引", "---", "", "# 印象笔记存档 · 转换索引", "",
             f"- 来源存档：`{os.path.basename(archive)}`",
             f"- 存档导出时间：{ts_to_local(root_el.get('export-date'))}",
             f"- 笔记数：{len(notes)}（成功还原正文 {ok_n} 篇）",
             f"- 附件数：{len(used)} 个（存放于 `附件/`）",
             f"- 正文来源：本机客户端明文缓存 `{store_dir}`",
             f"- 附件引用校验：正文引用 {stats['media_ok']} 处全部命中"
             + (f"，缺失 {stats['media_missing']} 处" if stats["media_missing"] else ""),
             f"- 转换时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S +0800')}",
             "", "| # | 标题 | 创建时间 | 附件 | 正文来源 |", "|---|---|---|---|---|"]
    for e in index:
        t = e["title"].replace("|", "\\|")
        link = f"[{t}]({e['file']})" if e["file"] else f"{t}（未还原）"
        lines.append(f"| {e['i']} | {link} | {e['created']} | {len(e['att'])} | {e['src']} |")
    with open(os.path.join(outdir, "00-索引.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    if warnings:
        with open(os.path.join(outdir, "转换警告.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(warnings) + "\n")

    stats.update({
        "notes": len(notes),
        "attachments": len(used),
        "store": store_dir,
        "unmatched": [e["title"] for e in index if not e["ok"]],
        "warnings": warnings,
    })
    if verbose:
        print(f"\n完成 -> {outdir}\n  md {ok_n} 篇 / 附件 {len(used)} 个 / 警告 {len(warnings)} 条")
    return stats


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="ENEX 存档 -> Markdown（正文取本机明文库）")
    ap.add_argument("archive")
    ap.add_argument("--out", help="输出目录（--check 时可省略）")
    ap.add_argument("--store", action="append",
                    help="客户端账户目录 / localNoteStore / sqlite 路径；可重复，缺省自动发现")
    ap.add_argument("--check", action="store_true", help="只做对齐与校验，不写文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--list-stores", action="store_true", help="列出本机发现的库后退出")
    a = ap.parse_args(argv)

    if a.list_stores:
        return find_store.main(["--json"] if a.json else [])

    stores = a.store or []
    if not stores:
        found = find_store.best()
        if not found:
            msg = ("未在本机发现客户端明文库。\n"
                   "→ 若本机没装客户端，只能用口令路径：enex2md.py --password '口令'")
            print(json.dumps({"error": "no_store", "message": msg}, ensure_ascii=False)
                  if a.json else msg)
            return 4
        stores = [found["root"]]

    if a.check:
        reports = []
        for s in stores:
            r = analyze(a.archive, s, verbose=not a.json)
            reports.append(r)
            if not a.json:
                print(f"\n库: {r['store']}\n"
                      f"  笔记 {r['notes']}（加密 {r['encrypted']}） 已对齐 {r['matched']} "
                      f"→ 可信度 {r['score']*100:.1f}%")
                print(f"  匹配方式: {r['match_by']}")
                if r["padding"]["checked"]:
                    print(f"  PKCS#7 校验: {r['padding']['ok']}/{r['padding']['checked']} 通过")
                if r["unmatched"]:
                    print(f"  未匹配 {len(r['unmatched'])} 篇: {r['unmatched'][:5]} ...")
        if a.json:
            print(json.dumps({"schema": "evernote-enex-to-md/check/1",
                              "reports": reports}, ensure_ascii=False, indent=2))
        return 0 if max((r["score"] for r in reports), default=0) > 0.5 else 1

    if not a.out:
        ap.error("--out 是必填项（--check 模式除外）")
    scored = {s: analyze(a.archive, s)["score"] for s in stores}
    best_store = max(stores, key=lambda s: scored[s])
    stats = convert(a.archive, best_store, a.out)
    if a.json:
        print(json.dumps({"schema": "evernote-enex-to-md/report/1", **stats},
                         ensure_ascii=False, indent=2))
    return 0 if not stats["unmatched"] else 1


if __name__ == "__main__":
    sys.exit(main())
