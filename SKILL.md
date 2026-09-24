---
name: evernote-enex-to-md
description: 把印象笔记 / Evernote 导出的 .enex / .notes 存档转换成 Markdown，附件单独存到 附件/ 目录。能处理正文被 Evernote ENC0 加密（<content encoding="base64:aes">，AES-128-CBC + PBKDF2）的存档：优先从本机桌面客户端的明文缓存还原，无口令也能转；无缓存时才回退口令解密或字典爆破。已实测版本：印象笔记 Mac 9.8.7 (478777) / Core Data Unified_27_0（详见 compat.json）。触发词：破解印象笔记存档、印象笔记导出成 markdown、enex 转 md、.notes 打不开、ENC0 密文、evernote export to markdown、印象笔记附件导出。
agent_created: true
version: "1.2"
license: MIT
---

# 印象笔记 / Evernote 存档 → Markdown

把 `.enex` / `.notes`（ENEX 3.0）转成 Markdown，附件抽到 `附件/`。
正文被 ENC0 加密时**先找本机客户端明文缓存**，找不到才考虑爆破。

**核心判断（别搞反）**：ENC0 是 AES-128-CBC + PBKDF2，口令不落盘，离线爆破基本没戏。
真正的突破口是桌面客户端把每条笔记的**明文 ENML** 缓存在本地磁盘上 —— 所以顺序永远是
「先找本地明文 → 再考虑猜口令」，不是反着来。

## 版本兼容性（先看这个）

**已实测通过的组合**（唯一有完整证据链的）：

| 项目 | 值 |
|---|---|
| 客户端 | **印象笔记 Mac 9.8.7（build 478777）**，bundle id `com.yinxiang.Mac` |
| 导出器声明 | `<en-export application="Evernote" version="Evernote Mac 9.8.7 (478777)">` |
| 存档格式 | **ENEX 3.0**（`evernote-export3.dtd`），正文 ENC0 加密 |
| 本地库布局 | `accounts/<host>/<uid>/{localNoteStore/LocalNoteStore.sqlite, content/<ZLOCALUUID>/content.enml}` |
| Core Data 模型 | **`Unified_27_0`**（31 个实体 / 34 张表，关键表 `ZENNOTE` `ZENRESOURCE` `ZENNOTEBOOK`） |
| 证据 | 32/32 标题+创建时间对齐 · 32/32 PKCS#7 长度一致 · 16/16 附件 hash 命中 · 32 篇+19 附件 0 警告 |

**机器可读版本清单：`compat.json`**（schema `evernote-enex-to-md/compat/1`）。里面区分了
`verified`（实测通过）/ `expected_compatible_untested`（同族但未实测）/ `not_applicable`（根本不适用）。
其他智能体应当**先读 compat.json 再决定要不要转换**，别默认"任何版本都能用"。

**在目标机器上自证兼容性**（不要靠猜版本号）：

```bash
python3 scripts/find_store.py --json    # 看 model_version / has_zenote / content_dirs / note_count
python3 scripts/enex_to_md.py <存档> --dry-run --json   # 看 alignment.score（阈值 0.5）
```

- `model_version` 是 `Unified_27_0` 且 `has_zenote=true` → 与已验证组合同族，可用。
- 没有 `content/` 目录、或没有 `ZENNOTE` 表 → 布局不同，**别硬试**，走 `--password`。
- **同一台机器常有多份库**（多账号 / 沙箱与非沙箱 / 客户端升级残留），模型版本和表数都不一样。
  **不要取第一个**，用 `find_store.py` 的 `score` 选：笔记数与 `content/` 明文目录数一致者才是当前在用的库。
  实测这台机器上并存 `Unified_27_0`(34 表·在用) / `Unified_24_0`(39 表·沙箱旧库) / `Unified_19_0`(33 表·空库)
  三种 —— 详见 `compat.json` 的 `observed_store_variants`。
- 结论不确定时，把 `store_details`（`enex_to_md.py --json` 报告里）连同 compat.json 一起回报。

**已知不适用的情形**：ENEX 1.x / 未加密存档（走明文路径即可）；笔记内 `en-crypt` 内联加密段
（需段落口令，任何版本都解不开）；Linux（无官方客户端，不存在本地缓存）；
只有加密存档 + 无缓存 + 无口令（只能爆破，强口令实际不可行）。

## 0. 前置

```bash
python3 -m pip install -r requirements.txt
# pycryptodome(口令解密) / beautifulsoup4 + lxml + markdownify(ENML 渲染)
```

只用检查类功能（`find_store.py`、`crack2md.py --check`、`--dry-run`）时，**一个依赖都不用装**。

## 1. 一条命令（首选）

```bash
python3 scripts/enex_to_md.py <存档.enex|.notes> --out <输出目录> [--json]
```

它自己决定怎么走：

```
读存档 → 正文有 <content encoding="base64:aes"> 吗？
├─ 没有 → 直接转（正文是明文 ENML）
└─ 有   → 自动发现本机客户端明文库
          ├─ 对齐率 ≥ 50% → 用本地明文还原正文（推荐路径）
          ├─ 对齐率太低  → 有 --password 就口令解密，否则退出码 4
          └─ 本机没库    → 有 --password 就口令解密，否则退出码 4
附件永远来自存档本体（<resource> 从不加密）
```

**退出码**（照它判断，不要靠猜日志）：

| 码 | 含义 | 下一步 |
|---|---|---|
| 0 | 全部成功 | 完事 |
| 1 | 部分成功 | 看报告里的 `unmatched` |
| 2 | 缺 Python 依赖 | `pip install -r requirements.txt` |
| 3 | 存档无法解析 | 确认是 ENEX 3.0 导出文件 |
| 4 | 正文加密且无明文来源 | 见下方「无路可走时」 |
| 5 | IO 错误 | 检查输出目录权限 |

`--json` 时 **stdout 只有一份 JSON**，进度信息走 stderr，可直接 `json.loads`。
报告 schema `evernote-enex-to-md/report/1`，关键字段：`source`(plaintext/client_cache/password)、
`store`、`score`、`matched_encrypted`、`unmatched`、`attachments`、`ok`。
同时会在输出目录落一份 `转换报告.json`。

## 2. 先体检再动手

```bash
python3 scripts/enex_to_md.py <存档> --dry-run --json     # 全流程判定，不写任何文件
python3 scripts/enex_to_md.py --list-stores              # 只看本机有哪些客户端明文库
python3 scripts/crack2md.py <存档> --check                # 只看对齐与 PKCS#7 校验
```

## 3. 分步工具（要精细控制时）

| 脚本 | 用途 |
|---|---|
| `find_store.py` | 跨平台自动定位客户端明文库；`--json` 给机器读，含笔记数/评分 |
| `crack2md.py` | 用本地明文还原正文；`--check` 只校验；`--store` 可指定/重复 |
| `enex2md.py` | 纯 ENEX 转换；有口令时走 `--password`，明文存档直接转 |
| `brute.py` | 口令搜索：字典 / stdin / 字符集穷举，多进程 |
| `enc0.py` | ENC0 载荷解析与口令校验（可 import） |

```bash
# 指定某个账号库
python3 scripts/crack2md.py archive.notes --store "$HOME/Library/Application Support/com.yinxiang.Mac/accounts/<host>/<uid>" --out 转换结果

# 口令路径
python3 scripts/enex2md.py archive.enex --password '口令' --out 转换结果

# 爆破（输出 FOUND_PASSWORD: xxx / NOT_FOUND，退出码 0/1）
python3 scripts/brute.py archive.notes wordlist.txt --note 1 --jobs 8
python3 scripts/brute.py archive.notes --charset 0123456789 --min 4 --max 6
cat commons.txt | python3 scripts/brute.py archive.notes -
```

爆破量级要心里有数：PBKDF2 5 万次迭代，**单核约 1200 条/秒**，8 核约 1 万/秒。
适合「猜口令」，不适合穷举长口令。每条笔记 salt 都不同 → 无彩虹表捷径。

## 4. ENC0 载荷格式

`base64` 解码后的字节布局（官方 scheme，非自定义）：

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | 魔数 `ENC0` |
| 4 | 16 | salt |
| 20 | 16 | salthmac |
| 36 | 16 | iv |
| 52 | n×16 | AES-128-CBC 密文（PKCS#7 填充） |
| 尾部 | 32 | HMAC-SHA256 校验（校验对象 = 前面全部） |

KDF：`PBKDF2-HMAC-SHA256(口令, salt, 50000, 16)`；HMAC 密钥用 `salthmac` 派生。
**校验口令无需解密** —— 比 HMAC 即可，这就是 `brute.py` 的判定依据。

## 5. 本机明文缓存位置

```
<账户目录>/
├── localNoteStore/LocalNoteStore.sqlite   Core Data：ZENNOTE 表
│                                           ZTITLE / ZDATECREATED / ZLOCALUUID
└── content/<ZLOCALUUID>/content.enml      明文正文 ENML
```

- macOS 印象笔记：`~/Library/Application Support/com.yinxiang.Mac/accounts/<host>/<uid>/`
  （沙箱版还有一份在 `~/Library/Containers/com.yinxiang.Mac/Data/Library/Application Support/...`）
- macOS Evernote：`~/Library/Application Support/com.evernote.Evernote/` 等
- Windows：`%APPDATA%\..\Local\Evernote`、`%APPDATA%\yinxiang`、`Documents\Evernote`

`find_store.py` 会自动扫这些根目录并给候选库打分，**不用手抄路径**。

## 6. 三重校验（都要做，不能只靠标题）

1. **对齐**：标题 + 创建时间（时间戳是 Core Data 秒：`2001-01-01T00:00:00Z + ZDATECREATED`）。
2. **PKCS#7 一致性**：`len(本地明文) + pad == len(密文)`，`pad ∈ [1,16]`，`len(密文) % 16 == 0`。
   这条过了基本就证明明文对得上，比「看着像」可靠得多。
3. **附件引用**：正文里每个 `en-media hash` 都要能在存档 `<resource>` 里找到
   （hash = 资源二进制的 MD5 hex）。

## 7. 坑

- **目录名是 `ZLOCALUUID`（大写），不是 `ZGUID`**。按 `ZGUID` 比对会 0 命中。
- 别用 `LocalNoteStore.sqlite` 前缀匹配文件 —— `-wal` / `-shm` 会被误当成库（"file is not a database"）。
- 用 `content.enml`，不要 `content-pre-sync.enml`（同步前旧版）。
- 本地库一律**只读**打开（`mode=ro`）；被锁时复制到临时目录再读，绝不写回用户目录。
- 转换 ENML 前必须把 `en-media` 换成 `<img>`/`<a>`、`en-todo` 换成 ☑/☐、
  `en-crypt` 留占位符，否则 markdownify 会静默丢掉这些标签的内容。
- 标题里的 `/ : * ? " < > |` 要替换（否则 macOS 建不了文件）；附件重名加 `_1` 后缀。
- 「附件 0 个」不代表失败 —— 很多笔记本来就没附件。
- **改脚本时先跑自测**：`scripts/selftest.notes` 是自造的 ENC0 样本（口令 `test123`），
  1 条加密 + 1 条明文 + 1 个附件：
  ```bash
  python3 scripts/enex_to_md.py scripts/selftest.notes --password test123 --out /tmp/t
  python3 scripts/brute.py scripts/selftest.notes --charset 3 --min 1 --max 1 --prefix test12
  ```
  自测能挡住「字节布局搞错 → 白跑几小时爆破」。
- 破坏性操作一律不做：原始存档只读，不删不改用户文件。

## 8. 无路可走时（退出码 4）

1. 让用户打开桌面客户端登录、等这些笔记同步完，再重跑（最省事）。
2. 手上有口令 → `--password '口令'`。
3. 只能猜口令 → `brute.py`。**明确告诉用户**：官方设计上忘记口令即无法恢复，
   爆破命中率取决于口令强度（强口令 = 没戏）。
4. 兜底：附件不加密，可以用 `enex2md.py` 先导出附件；缺正文的笔记在索引里标注。

## 9. 装到别的智能体上

上游仓库：<https://github.com/omom2002/evernote-to-md>
（`git clone https://github.com/omom2002/evernote-to-md.git`）

整个目录就是一个标准 Skill 包：`SKILL.md`（本文件）+ `compat.json`（版本兼容清单）
+ `scripts/` + `requirements.txt` + `install.sh`。

- **WorkBuddy / Claude Code 类**：把目录放进 `~/.workbuddy/skills/`（用户级）或
  项目内 `.workbuddy/skills/`（项目级，跟着仓库走），重启会话即被识别。
- **其他任意智能体 / 脚本 / CI**：不需要 Skill 机制，直接调 CLI ——
  `python3 scripts/enex_to_md.py ... --json`，按退出码和 JSON 报告决策即可。
  报告里的 `tool.version` / `tool.verified_client` 会带上本工具的版本与已验证客户端组合。
- 本技能不依赖任何私有路径或账号：换一台机器直接可用（路径由 `find_store.py` 发现）。

## 10. 版本维护约定

- 上游客户端升级后，`compat.json` 的 `verified` 条目要跟着更新（客户端版本 + build +
  Core Data 模型标识 + 证据数字），并在 `expected_compatible_untested` 里说明尚未覆盖的范围。
- 在别人的机器上跑通/跑不通，都值得把 `store_details` 里的 `model_version` 回报进 compat.json，
  逐步把「未实测」变成「已验证」。
- 千万别写"支持所有版本"这类话 —— 本地库布局是客户端内部实现，说变就变。
