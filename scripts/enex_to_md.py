#!/usr/bin/env python3
"""ENEX / 印象笔记存档 -> Markdown —— 统一入口（推荐所有调用方用这一个）。

一条命令搞定，自动决策：

    1. 读存档，判断正文是否被 ENC0 加密（<content encoding="base64:aes">）
    2. 未加密  -> 直接转
    3. 已加密  -> 自动发现本机客户端的**明文缓存**，先做对齐校验（不写文件），
                  取可信度最高的库来还原正文；库不可用时才回退口令解密
    4. 产出 Markdown + 附件/ + 索引 + JSON 报告

用法
----
    enex_to_md.py ARCHIVE --out DIR [--store DIR] [--password PW]
                          [--dry-run] [--json] [--list-stores]

退出码（供智能体判断）
--------------------
    0  全部成功
    1  部分成功（个别笔记未还原，看报告里的 unmatched）
    2  缺少 Python 依赖
    3  存档无法解析（非 ENEX / 文件损坏）
    4  正文加密且没有可用的明文来源（需要 --password，或用户在客户端同步过这些笔记）
    5  输出目录不可写等 IO 问题

JSON 报告 schema：`evernote-enex-to-md/report/1`
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA = "evernote-enex-to-md/report/1"
TOOL_VERSION = "1.2"
# 与 compat.json 的 verified 条目对应：已经实测过的客户端组合
VERIFIED_CLIENT = "印象笔记 Mac 9.8.7 (478777) / Core Data Unified_27_0"
# 低于这个对齐率就不敢信这个库
MIN_SCORE = 0.5
# 最多试几个候选库
MAX_STORES = 5


def deps_status():
    """检查可选依赖，返回 {模块: 是否安装}。"""
    want = {
        "Crypto": "pycryptodome",   # 口令解密
        "bs4": "beautifulsoup4",    # ENML -> Markdown
        "lxml": "lxml",
        "markdownify": "markdownify",
    }
    return {mod: importlib.util.find_spec(mod) is not None for mod in want}


def missing_for(need_decrypt, need_convert, status):
    miss = []
    if need_decrypt and not status["Crypto"]:
        miss.append("pycryptodome")
    if need_convert and not (status["bs4"] and status["lxml"] and status["markdownify"]):
        for m, pkg in (("bs4", "beautifulsoup4"), ("lxml", "lxml"),
                       ("markdownify", "markdownify")):
            if not status[m] and pkg not in miss:
                miss.append(pkg)
    return miss


INSTALL_HINT = ("缺依赖。安装方式任选其一：\n"
                "  python3 -m pip install -r requirements.txt\n"
                "  python3 -m pip install pycryptodome beautifulsoup4 lxml markdownify")


def survey(archive):
    """读存档元信息：笔记数 / 加密笔记数 / 附件数。"""
    import xml.etree.ElementTree as ET
    root = ET.parse(archive).getroot()
    notes = root.findall("note")
    enc = 0
    att = 0
    for n in notes:
        c = n.find("content")
        if c is not None and (c.get("encoding") or "") == "base64:aes":
            enc += 1
        att += len(n.findall("resource"))
    return {"notes": len(notes), "encrypted": enc, "attachments": att,
            "export_date": root.get("export-date"), "root": root}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ENEX / 印象笔记存档 -> Markdown（自动选路）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", nargs="?", help=".enex / .notes 存档")
    ap.add_argument("--out", "-o", help="输出目录")
    ap.add_argument("--store", action="append",
                    help="指定客户端账户目录（可重复）；缺省自动发现")
    ap.add_argument("--password", "-p", default="",
                    help="加密笔记口令；本机无明文缓存时才需要")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做检查和对齐校验，不写任何文件")
    ap.add_argument("--json", action="store_true",
                    help="stdout 输出 JSON 报告（进度信息走 stderr）")
    ap.add_argument("--list-stores", action="store_true",
                    help="列出本机发现的客户端明文库后退出")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE,
                    help=f"低于此对齐率不采用该库（默认 {MIN_SCORE}）")
    a = ap.parse_args(argv)

    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    report = {"schema": SCHEMA,
              "tool": {"name": "evernote-enex-to-md", "version": TOOL_VERSION,
                       "verified_client": VERIFIED_CLIENT},
              "archive": os.path.abspath(a.archive) if a.archive else None,
              "out": os.path.abspath(a.out) if a.out else None, "ok": False}

    import find_store

    if a.list_stores:
        return find_store.main(["--json"] if a.json else [])

    # ---- 0. 输入检查 ------------------------------------------------------ #
    if not os.path.isfile(a.archive):
        print(json.dumps({**report, "error": "archive_not_found",
                          "message": f"找不到存档：{a.archive}"}, ensure_ascii=False))
        return 3
    if not a.out and not a.dry_run:
        ap.error("--out 是必填项（--dry-run 例外）")

    status = deps_status()
    report["deps"] = status

    # ---- 1. 读存档 -------------------------------------------------------- #
    try:
        info = survey(a.archive)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({**report, "error": "archive_unreadable", "message": str(e)},
                         ensure_ascii=False))
        return 3
    report.update({"notes": info["notes"], "encrypted_notes": info["encrypted"],
                   "attachments_in_archive": info["attachments"],
                   "export_date": info["export_date"]})
    log(f"存档：{info['notes']} 篇笔记（加密 {info['encrypted']}）· 附件 {info['attachments']}")

    # ---- 2. 明文存档：直接转 ---------------------------------------------- #
    if info["encrypted"] == 0:
        miss = missing_for(False, not a.dry_run, status)
        if miss:
            report.update({"error": "missing_dependency", "missing": miss})
            print(json.dumps(report, ensure_ascii=False) if a.json
                  else f"{INSTALL_HINT}\n缺: {', '.join(miss)}")
            return 2
        if a.dry_run:
            report.update({"source": "plaintext", "ok": True,
                           "matched": info["notes"], "score": 1.0})
            _emit(report, a, log, "正文未加密 → 去掉 --dry-run 即可直接转换")
            return 0
        import enex2md
        st = enex2md.convert(a.archive, a.password, a.out, verbose=not a.json)
        report.update(st)
        report.update({"source": "plaintext", "store": None, "score": 1.0,
                       "matched": st["converted"]})
        return _finish(report, a)

    # ---- 3. 加密存档：优先本机明文缓存 ------------------------------------ #
    stores = a.store or []
    details = []
    if not stores:
        cands = find_store.discover()[:MAX_STORES]
        details = [{"root": c["root"], "score": c["score"], "host": c["host"],
                    "uid": c["uid"], "note_count": c["note_count"],
                    "content_dirs": c["content_dirs"],
                    "model_version": c["model_version"], "has_zenote": c["has_zenote"],
                    "warnings": c["warnings"]} for c in cands]
        stores = [c["root"] for c in cands if c["score"] > 0]
        if cands and not stores:
            log("发现客户端目录，但没有可用的明文库（缺 content/ 或 ZENNOTE 表）。")
    else:
        details = [{"root": s} for s in stores]
    report["store_candidates"] = stores
    report["store_details"] = details

    import crack2md
    best, best_r = None, None
    if stores:
        log(f"检查 {len(stores)} 个本机明文库 …")
        for s in stores:
            r = crack2md.analyze(a.archive, s, verbose=False)
            mv = next((d.get("model_version") for d in details if d.get("root") == s), None)
            log(f"  · {s}\n      对齐 {r['matched_encrypted']}/{r['encrypted']}"
                f" → 可信度 {r['score']*100:.0f}%"
                + (f" · PKCS#7 {r['padding']['ok']}/{r['padding']['checked']} 通过"
                   if r["padding"]["checked"] else "")
                + (f" · 模型 {mv}" if mv else ""))
            if best_r is None or r["score"] > best_r["score"]:
                best, best_r = s, r

    report["alignment"] = best_r

    if best_r and best_r["score"] >= a.min_score:
        mv = next((d.get("model_version") for d in details if d.get("root") == best), None)
        report.update({"source": "client_cache", "store": best,
                       "score": best_r["score"], "matched": best_r["matched"],
                       "matched_encrypted": best_r["matched_encrypted"],
                       "compat": {"verified_client": VERIFIED_CLIENT,
                                  "store_model_version": mv,
                                  "matches_verified_combo": bool(
                                      mv and "Unified_27_0" in mv)}})
        miss = missing_for(False, not a.dry_run, status)
        if miss:
            report.update({"error": "missing_dependency", "missing": miss})
            print(json.dumps(report, ensure_ascii=False) if a.json
                  else f"{INSTALL_HINT}\n缺: {', '.join(miss)}")
            return 2
        if a.dry_run:
            report["ok"] = True
            _emit(report, a, log,
                  f"可信库：{best}\n  对齐 {best_r['matched_encrypted']}/{best_r['encrypted']}"
                  f" 加密笔记（可信度 {best_r['score']*100:.0f}%）→ 去掉 --dry-run 即执行")
            return 0
        st = crack2md.convert(a.archive, best, a.out, verbose=not a.json)
        report.update(st)
        return _finish(report, a)

    # ---- 4. 回退：口令解密 ------------------------------------------------ #
    if a.password:
        miss = missing_for(True, not a.dry_run, status)
        if miss:
            report.update({"error": "missing_dependency", "missing": miss})
            print(json.dumps(report, ensure_ascii=False) if a.json
                  else f"{INSTALL_HINT}\n缺: {', '.join(miss)}")
            return 2
        log("本机明文库不可用或不匹配 → 用口令解密")
        if a.dry_run:
            import enc0
            import re as _re
            src = open(a.archive, encoding="utf-8", errors="replace").read()
            blk = _re.findall(
                r'<content encoding="base64:aes"><!\[CDATA\[(.*?)\]\]></content>', src,
                flags=_re.S)[0]
            ok = enc0.Enc0.from_b64(blk).verify(a.password)
            report.update({"source": "password", "score": 1.0 if ok else 0.0,
                           "ok": ok,
                           "message": "口令正确" if ok else "口令不正确"})
            _emit(report, a, log, "口令正确" if ok else "口令不正确")
            return 0 if ok else 4
        import enex2md
        st = enex2md.convert(a.archive, a.password, a.out, verbose=not a.json)
        report.update(st)
        report.update({"source": "password",
                       "score": 1.0 - st["decrypt_failed"] / max(st["encrypted"], 1),
                       "matched": st["converted"]})
        return _finish(report, a)

    # ---- 5. 无路可走：给出可执行的下一步 ---------------------------------- #
    advice = []
    observed_models = sorted({d.get("model_version") for d in details
                              if d.get("model_version")})
    if not stores:
        advice.append("本机没发现印象笔记/Evernote 客户端明文缓存。"
                      "如果账号装了客户端，先登录并等它同步完这些笔记，再重跑。")
    else:
        advice.append("本机有客户端库，但这些笔记不在里面（对齐率过低）。"
                      "可能是另一个账号，或客户端还没同步到这些笔记。")
        if observed_models and not any("Unified_27_0" in m for m in observed_models):
            advice.append(f"注意：本机库的 Core Data 模型版本是 {observed_models}，"
                          f"与已验证组合（Unified_27_0）不同，本地库布局可能有差异。"
                          "建议改用 --password，并参考 compat.json 回报结果。")
    advice.append("若手上有口令：加 --password '口令' 重跑。")
    advice.append("也可用 brute.py 尝试猜口令（PBKDF2 5 万次迭代，约 1200 条/秒）。")
    report.update({"error": "no_plaintext_source", "message": " ".join(advice),
                   "compat": {"verified_client": VERIFIED_CLIENT,
                              "observed_store_model_versions": observed_models},
                   "ok": False})
    _emit(report, a, log, "\n".join(["无法还原正文："] + [f"  - {x}" for x in advice]))
    return 4


def _emit(report, a, log, human=""):
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif human:
        log(human)


def _finish(report, a):
    """写报告文件 + 决定退出码。"""
    unmatched = report.get("unmatched") or []
    report["ok"] = not unmatched and not report.get("decrypt_failed")
    if report.get("decrypt_failed"):
        report["error"] = "decrypt_failed"
    report["exit_code"] = 0 if report["ok"] else 1

    out = a.out
    if out:
        try:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "转换报告.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        except OSError as e:
            report["report_write_error"] = str(e)

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n正文来源：{report.get('source')}"
              + (f"（{report.get('store')}）" if report.get("store") else ""))
        if unmatched:
            print(f"未还原 {len(unmatched)} 篇：{unmatched[:5]}"
                  + (" …" if len(unmatched) > 5 else ""))
    return report["exit_code"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(5)
