#!/usr/bin/env python3
"""Locate the Evernote / 印象笔记 desktop client's local note store.

Why this exists
---------------
A `.enex` / `.notes` archive whose notes carry
`<content encoding="base64:aes">` (Evernote ENC0, AES-128-CBC + PBKDF2) can be
restored without knowing the passphrase **if the desktop client still holds a
plaintext copy of the notes on this machine**. The Mac/Windows clients do:

    <account-dir>/
    ├── localNoteStore/LocalNoteStore.sqlite    Core Data DB: ZENNOTE table
    │                                           (ZTITLE / ZDATECREATED / ZLOCALUUID)
    └── content/<ZLOCALUUID>/content.enml       plaintext ENML body

`crack2md.py` needs `<account-dir>`. Rather than making every caller hard-code a
platform path and an account id, this script finds it.

Everything here is **read-only** — the client database is opened with
`mode=ro` (and copied to a temp dir only when a WAL lock forces it).

CLI
---
    find_store.py                 # human-readable list of candidate stores
    find_store.py --json          # machine-readable list
    find_store.py --json --notes N  # only stores holding at least N notes
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile

HOME = os.path.expanduser("~")

# Directory names that may hold a client install (matched case-insensitively).
NAME_HINTS = ("evernote", "yinxiang", "印象", "enex")
# Depth limit when hunting for LocalNoteStore.sqlite below a candidate root.
MAX_DEPTH = 5
# Sub-directories that never contain the note store; skipping them is a big win.
PRUNE = {"cache", "caches", "tmp", "temp", "logs", "log", "index", "indexes",
         "backup", "backups", "trash", "thumbs", "crashpad", "crashreports"}


# --------------------------------------------------------------------------- #
# platform roots
# --------------------------------------------------------------------------- #
def _iter_bases():
    """Directories that may contain a client application-support folder."""
    if sys.platform == "darwin":
        yield f"{HOME}/Library/Application Support"
        yield f"{HOME}/Documents"
        # sandboxed builds
        for p in glob.glob(f"{HOME}/Library/Containers/*/Data/Library/Application Support"):
            yield p
        for p in glob.glob(f"{HOME}/Library/Group Containers/*"):
            yield p
    elif os.name == "nt":
        for var in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
            if os.environ.get(var):
                yield os.environ[var]
        yield f"{HOME}/Documents"
    else:
        yield os.environ.get("XDG_DATA_HOME") or f"{HOME}/.local/share"
        yield f"{HOME}/Documents"


def candidate_roots():
    seen, out = set(), []
    for base in _iter_bases():
        if not base or not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            low = n.lower()
            if not any(h in low for h in NAME_HINTS):
                continue
            p = os.path.join(base, n)
            if os.path.isdir(p) and p not in seen:
                seen.add(p)
                out.append(p)
    return out


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def _find_dbs(root):
    hits, base_depth = [], root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d.lower() not in PRUNE]
        for fn in filenames:
            # exact name only: ignore the -wal / -shm / -journal sidecars,
            # they are not databases and would blow up with "file is not a database"
            if fn == "LocalNoteStore.sqlite":
                hits.append(os.path.join(dirpath, fn))
    return hits


def normalize_store(path):
    """Accept <account-dir> | <.../localNoteStore> | <.../LocalNoteStore.sqlite>.

    Returns (store_dir, db_path) or (None, None) when nothing usable is found.
    """
    if not path:
        return None, None
    p = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(p):
        d = os.path.dirname(p)
        store = os.path.dirname(d) if os.path.basename(d) == "localNoteStore" else d
        return store, p
    d = p
    if os.path.basename(d) == "localNoteStore":
        d = os.path.dirname(d)
    db = os.path.join(d, "localNoteStore", "LocalNoteStore.sqlite")
    if os.path.exists(db):
        return d, db
    db2 = os.path.join(d, "LocalNoteStore.sqlite")
    if os.path.exists(db2):
        return d, db2
    return None, None


def _open_ro(db):
    """Open read-only; if a WAL lock blocks us, copy the DB set to a temp dir."""
    try:
        uri = "file:{0}?mode=ro".format(db.replace("\\", "/").replace("?", "%3f"))
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.execute("PRAGMA query_only=1")
        con.execute("SELECT count(*) FROM sqlite_master")
        return con, None
    except Exception:
        tmp = tempfile.mkdtemp(prefix="enex_store_")
        for suf in ("", "-wal", "-shm"):
            src = db + suf
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, os.path.basename(db) + suf))
        con = sqlite3.connect(os.path.join(tmp, os.path.basename(db)))
        con.execute("PRAGMA query_only=1")
        return con, tmp


def inspect(root, db):
    """Describe one candidate store. Returns a dict (never raises)."""
    store, db_path = normalize_store(db)
    store = store or os.path.dirname(os.path.dirname(db_path or db))
    info = {
        "root": store,
        "db": db_path,
        "content": os.path.join(store, "content"),
        "host": None,
        "uid": None,
        "note_count": None,
        "content_dirs": None,
        "db_tables": [],
        "table_count": None,
        "model_version": None,
        "has_zenote": False,
        "warnings": [],
    }
    # host / uid from the conventional .../accounts/<host>/<uid>/ layout
    parts = store.replace("\\", "/").split("/")
    if "accounts" in parts:
        k = parts.index("accounts")
        if len(parts) > k + 2:
            info["host"], info["uid"] = parts[k + 1], parts[k + 2]

    content = info["content"]
    if os.path.isdir(content):
        try:
            info["content_dirs"] = len(os.listdir(content))
        except OSError:
            pass
    else:
        info["warnings"].append("没有找到 content/ 明文目录")

    tmp = None
    try:
        con, tmp = _open_ro(db_path)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        info["db_tables"] = sorted(tables)
        info["table_count"] = len(tables)
        # Core Data 模型版本 —— 用它判断本地库布局是否与已验证版本同族
        try:
            row = con.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()
            if row and row[0]:
                meta = plistlib.loads(row[0])
                ids = meta.get("NSStoreModelVersionIdentifiers") or []
                if isinstance(ids, (list, tuple)):
                    ids = ", ".join(str(x) for x in ids)
                info["model_version"] = str(ids) or None
        except Exception:
            pass
        for t in ("ZENNOTE", "ZNOTE"):
            if t in tables:
                info["has_zenote"] = True
                info["note_count"] = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                break
        if info["note_count"] is None:
            info["warnings"].append("库里没有 ZENNOTE 表（可能是新版客户端数据结构）")
        con.close()
    except Exception as e:  # noqa: BLE001 - diagnostics only
        info["warnings"].append(f"读取本地库失败: {e}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # score: a store whose note count matches its content/ dir count is the real one
    score = 0
    if info["has_zenote"]:
        score += 2
    if info["content_dirs"]:
        score += 2
        if info["note_count"] and abs(info["content_dirs"] - info["note_count"]) <= 2:
            score += 3
    info["score"] = score
    return info


def discover(roots=None):
    """All plausible client note stores on this machine, best first."""
    bases = roots or candidate_roots()
    seen_dbs, found = set(), []
    for r in bases:
        for db in _find_dbs(r):
            real = os.path.realpath(db)
            if real in seen_dbs:
                continue
            seen_dbs.add(real)
            found.append(inspect(r, db))
    found.sort(key=lambda d: (-d["score"], d["root"]))
    # one account dir can be reachable through more than one path
    # (real Application Support vs. the sandbox container): keep the better one
    by_root = {}
    for info in found:
        key = os.path.realpath(info["root"])
        if key not in by_root or info["score"] > by_root[key]["score"]:
            by_root[key] = info
    return sorted(by_root.values(), key=lambda d: (-d["score"], d["root"]))


def best(roots=None, min_notes=1):
    """Single best candidate, or None."""
    for info in discover(roots):
        if info["score"] > 0 and (info["note_count"] or 0) >= min_notes:
            return info
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--notes", type=int, default=0, help="只列出至少含 N 条笔记的库")
    ap.add_argument("--root", action="append", help="额外/指定搜索根目录（可重复）")
    a = ap.parse_args(argv)

    stores = discover(a.root)
    if a.notes:
        stores = [s for s in stores if (s["note_count"] or 0) >= a.notes]

    if a.json:
        print(json.dumps({"schema": "evernote-enex-to-md/stores/1", "stores": stores},
                         ensure_ascii=False, indent=2))
        return 0 if stores else 4

    if not stores:
        print("未发现本机客户端笔记库。")
        print("→ 说明：本机没装印象笔记/Evernote 桌面客户端，或客户端从未同步过这些笔记。")
        print("→ 下一步：只能走口令路径（scripts/enex2md.py --password）。")
        return 4

    print(f"发现 {len(stores)} 个候选笔记库（按可用性排序）：\n")
    for i, s in enumerate(stores, 1):
        print(f"[{i}] score={s['score']}  {s['root']}")
        detail = []
        if s["uid"]:
            detail.append(f"账号 {s['uid']}")
        if s["host"]:
            detail.append(s["host"])
        if s["note_count"] is not None:
            detail.append(f"库内笔记 {s['note_count']}")
        if s["content_dirs"] is not None:
            detail.append(f"content/ 明文 {s['content_dirs']} 份")
        if s["model_version"]:
            detail.append(f"模型 {s['model_version']}")
        print("    " + " · ".join(detail) if detail else "    (无法读取元数据)")
        for w in s["warnings"]:
            print(f"    ! {w}")
        print()
    print("用法：把上面某个目录传给 crack2md.py，或直接跑 enex_to_md.py（会自动挑最优的）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
