#!/usr/bin/env python3
"""ENEX / ENC0 口令搜索（穷举 / 字典）。

ENEX 存档里加密笔记的正文是 ENC0 载荷（AES-128-CBC，口令不落盘）。
校验口令不需要解密：把候选口令经 PBKDF2 派生出 HMAC 密钥，与载荷尾部
32 字节 HMAC-SHA256 比对即可 —— 这就是本脚本的判定依据，快且无误判。

用法
----
    brute.py ARCHIVE <候选词文件|-> [更多文件...]   # 字典模式（- 表示 stdin）
    brute.py ARCHIVE --charset 0123456789 --min 4 --max 6   # 字符集穷举
    brute.py ARCHIVE wordlist.txt --note 3 --jobs 8         # 指定用第 3 条笔记做判定

命中时打印 `FOUND_PASSWORD: <口令>` 并以 0 退出；穷尽无果退出 1。
输出是机器可读的单行格式，方便其他智能体直接解析。

注意：PBKDF2-HMAC-SHA256 迭代 5 万次，单核约 1200 次/秒 —— 字典爆破适合
「猜口令」而非「穷举」。真正的首选路径是客户端本地明文缓存，见 SKILL.md。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import itertools
import os
import re
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enc0 import Enc0  # noqa: E402

ITER = 50000
_STATE: dict = {}
CHUNK = 512  # candidates per work item


def load_oracle(archive, note_index=1):
    """从存档里取一条加密笔记的载荷，作为口令校验器。"""
    src = open(archive, encoding="utf-8", errors="replace").read()
    blocks = re.findall(
        r'<content encoding="base64:aes"><!\[CDATA\[(.*?)\]\]></content>', src, flags=re.S)
    if not blocks:
        sys.exit(f"NOT_ENCRYPTED: {archive} 里没有 ENC0 加密笔记，无需爆破")
    if not 1 <= note_index <= len(blocks):
        sys.exit(f"笔记序号 {note_index} 超出范围（该存档有 {len(blocks)} 条加密笔记）")
    return Enc0.from_b64(blocks[note_index - 1]), len(blocks)


def _init_worker(salthmac, body, digest):
    _STATE["salthmac"] = salthmac
    _STATE["body"] = body
    _STATE["digest"] = digest


def check(pw: str):
    """单条候选口令判定；命中返回口令，否则 None。"""
    try:
        b = pw.encode("utf-8")
    except Exception:
        return None
    key = hashlib.pbkdf2_hmac("sha256", b, _STATE["salthmac"], ITER, 16)
    if hmac.compare_digest(hmac.new(key, _STATE["body"], hashlib.sha256).digest(),
                           _STATE["digest"]):
        return pw
    return None


def _work(chunk):
    for c in chunk:
        if check(c):
            return c, len(chunk)
    return None, len(chunk)


def _chunks(it, n=CHUNK):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def from_files(paths):
    for p in paths:
        if p == "-":
            for line in sys.stdin:
                yield line.rstrip("\n")
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                c = line.rstrip("\r\n")
                if c:
                    yield c


def from_charset(charset, lo, hi, prefix="", suffix=""):
    for n in range(lo, hi + 1):
        for tup in itertools.product(charset, repeat=n):
            yield prefix + "".join(tup) + suffix


def main(argv=None):
    ap = argparse.ArgumentParser(description="ENEX/ENC0 口令搜索")
    ap.add_argument("archive")
    ap.add_argument("wordlists", nargs="*", default=[],
                    help="候选词文件，- 表示 stdin")
    ap.add_argument("--charset", help="字符集穷举，如 0123456789 或 abc123")
    ap.add_argument("--min", type=int, default=1, help="穷举最短长度（含）")
    ap.add_argument("--max", type=int, default=4, help="穷举最长长度（含）")
    ap.add_argument("--prefix", default="", help="给穷举结果加前缀")
    ap.add_argument("--suffix", default="", help="给穷举结果加后缀")
    ap.add_argument("--note", type=int, default=1, help="用第几条加密笔记做判定（1 起）")
    ap.add_argument("--jobs", type=int, default=0, help="并发进程数，0 = CPU 核数-1")
    a = ap.parse_args(argv)

    payload, total = load_oracle(a.archive, a.note)
    print(f"# 判定用载荷：第 {a.note}/{total} 条加密笔记  "
          f"salt={payload.salt.hex()[:12]}…  密文 {len(payload.ciphertext)}B", flush=True)

    if a.charset:
        cands = from_charset(a.charset, a.min, a.max, a.prefix, a.suffix)
        mode = f"字符集 {a.charset!r} 长度 {a.min}-{a.max}"
    elif a.wordlists:
        cands = from_files(a.wordlists)
        mode = f"字典 {a.wordlists}"
    else:
        cands = from_files(["-"])
        mode = "stdin"
    print(f"# 模式：{mode}", flush=True)

    t0, done, found = time.time(), 0, None
    jobs = a.jobs or max(1, (os.cpu_count() or 2) - 1)

    if jobs == 1:
        for c in cands:
            done += 1
            if check(c):
                found = c
                break
            if done % 20000 == 0:
                el = time.time() - t0
                print(f"  tried {done}  ({done/el:.0f} pw/s)", flush=True)
    else:
        with Pool(processes=jobs,
                  initializer=_init_worker,
                  initargs=(payload.salthmac, payload.body, payload.digest)) as pool:
            for res, n in pool.imap_unordered(_work, _chunks(cands), chunksize=1):
                done += n
                if res:
                    found = res
                    pool.terminate()
                    break
                if done % 20000 < CHUNK:
                    el = time.time() - t0
                    print(f"  tried {done}  ({done/el:.0f} pw/s)", flush=True)

    el = time.time() - t0
    if found:
        print(f"FOUND_PASSWORD: {found}")
        rate = f"{done/el:.0f} pw/s" if done >= 500 else "样本太小，速率无意义"
        print(f"# 用时 {el:.1f}s，尝试 {done} 条，{rate}，进程 {jobs}")
        return 0
    print(f"NOT_FOUND: {done} candidates in {el:.1f}s")
    if done >= 500:
        print(f"# {done/max(el, 1e-9):.0f} pw/s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
