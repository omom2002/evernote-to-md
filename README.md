# evernote-enex-to-md

**中文** · [English](README.en.md)

仓库地址：<https://github.com/omom2002/evernote-to-md>

把**印象笔记 / Evernote** 导出的存档（`.enex` / `.notes`）转成干净的 Markdown：
一篇笔记一个 `.md`（带 YAML 元信息），附件全部抽到 `附件/` 目录，另附一份可点跳转的索引。

最关键的差异：**很多这种存档的正文是被加密的**（`<content encoding="base64:aes">`），
普通转换工具打开只剩一堆密文。本工具能在**不知道口令**的情况下还原正文 ——
只要你的电脑上装过印象笔记/Evernote 桌面客户端（它把明文缓存在本地）。

## 为什么能行

导出的存档里，正文用的是 Evernote 自家的 **ENC0** 加密：AES-128-CBC 加密 + PBKDF2 派生密钥，
口令**不写在文件里**。AES 本身没得破，所以网上主流结论是「没口令就没救」。

但桌面客户端为了显示笔记，会在本地留一份**明文**：

```
<账户目录>/
├── localNoteStore/LocalNoteStore.sqlite   ← 标题/时间/本地 UUID
└── content/<ZLOCALUUID>/content.enml      ← 明文正文
```

本工具把存档和本地明文按「标题 + 创建时间」对齐，再用 **PKCS#7 填充长度**做硬校验
（`本地明文长度 + 填充 == 密文长度`）。这条校验过了，就能证明拿到的确实是同一份明文，
而不是「看着像」—— 这比单纯比对标题可靠得多。

## 适用的版本

**实测通过（唯一有完整证据链的组合）：**

| 项目 | 值 |
|---|---|
| 客户端 | **印象笔记 Mac 9.8.7（build 478777）**（bundle id `com.yinxiang.Mac`） |
| 导出器声明 | `application="Evernote" version="Evernote Mac 9.8.7 (478777)"` |
| 存档格式 | ENEX 3.0（`evernote-export3.dtd`），正文 ENC0 加密 |
| 本地库模型 | Core Data `Unified_27_0`（31 实体 / 34 表，含 `ZENNOTE`） |

同族版本（其他印象笔记 Mac 9.x）大概率可用，但**请先自己验证**；官方 Evernote 10.x（Electron 版）、
Windows 客户端尚未实测；Linux 没有官方客户端，只能走口令路线。

**怎么验证**（别只看版本号，看结构）：

```bash
python3 scripts/find_store.py --json     # 关注 model_version / has_zenote / content_dirs
python3 scripts/enex_to_md.py "存档.notes" --dry-run --json   # 关注 alignment.score（≥0.5 才可用）
```

完整的版本清单、未实测范围、不适用情形都写在 **`compat.json`** 里（机器可读），
智能体应当先读它再决定是否转换。

## 安装

需要 Python 3.9+（已在 3.9.6 与 3.13 下实测通过）。

```bash
pip3 install -r requirements.txt          # 装依赖（只有渲染/解密才需要）
```

**作为智能体技能安装**：把整个目录复制到 `~/.workbuddy/skills/`（用户级，所有项目可用），
或项目下的 `.workbuddy/skills/`（跟着仓库走）。重启会话后，说一句
「把印象笔记存档转成 markdown」就会被自动调用。

```bash
./install.sh          # 一键装到 ~/.workbuddy/skills/（可选）
```

**不需要技能机制也能用**：所有功能都是命令行，任何脚本 / CI 都能直接调。

## 用法

```bash
# 一条命令，自动判断该怎么转（推荐）
python3 scripts/enex_to_md.py "笔记存档.notes" --out "转换结果"

# 先体检，不写任何文件
python3 scripts/enex_to_md.py "笔记存档.notes" --dry-run

# 看看本机有没有可用的客户端明文缓存
python3 scripts/enex_to_md.py --list-stores

# 只有明文存档（没加密）——普通转换
python3 scripts/enex2md.py "存档.enex" --out "转换结果"

# 手上有口令
python3 scripts/enex_to_md.py "存档.notes" --password '我的口令' --out "转换结果"
```

输出结构：

```
转换结果/
├── 00-索引.md          # 全部笔记表格，可点跳转；含附件引用校验结果
├── 01-笔记标题.md      # 每篇 YAML 元信息 + 正文（标题层级/表格/待办/图片）
├── 02-另一篇.md
├── 附件/               # 全部原始附件，md 里用 附件/xxx.jpg 相对路径引用
└── 转换报告.json       # 机器可读的转换结果（来源/对齐率/未还原清单）
```

## 常用场景

**「转换完发现正文是乱码/空白」**
说明存档正文加密了。先跑 `--list-stores` 看本机有没有客户端明文缓存；
没有的话，如果能登录客户端，先登录并等笔记同步完，再重跑。

**「本机没装客户端，也没有口令」**
附件是不加密的，至少能导出附件。正文只能靠猜口令（`scripts/brute.py`）——
官方设计上忘记口令即无法恢复，强口令基本没戏，请有心理准备。

**「库找到了，但说这些笔记不在里面」**
说明本机客户端是**另一个账号**，或者还没同步到这些笔记。

## 隐私说明

- 本地客户端数据库**只读**打开，不修改、不移动、不删除你电脑上的任何文件。
- 全程**不联网**：不做任何上传，不需要账号，不校验授权。
- 转换只在你指定的输出目录里写文件；原始存档不会被改动。
- 需要共享给别人时，建议只分享工具（本目录），别把含个人笔记的产出一起打包。

## 文件说明

| 文件 | 用途 |
|---|---|
| `scripts/enex_to_md.py` | **统一入口**，自动选路（推荐用这个） |
| `scripts/find_store.py` | 跨平台自动定位客户端明文库 |
| `scripts/crack2md.py` | 用本地明文还原正文（`--check` 只校验） |
| `scripts/enex2md.py` | 纯 ENEX 转换 / 口令解密 |
| `scripts/brute.py` | 口令搜索（字典 / 字符集穷举，多进程） |
| `scripts/enc0.py` | ENC0 载荷解析与口令校验 |
| `scripts/selftest.notes` | 自造测试样本，改代码后用它自测（口令 `test123`） |
| `compat.json` | 版本兼容清单（实测 / 未实测 / 不适用），机器可读 |
| `SKILL.md` | 给智能体看的技能说明（含调用契约与坑） |
| `README.en.md` | 英文说明（与本文同内容） |
| `LICENSE` | MIT 许可证 |

## 常见问题

**会不会把我的笔记传走？**
不会。整个流程纯本地，代码里没有任何网络请求。

**支持哪些平台？**
macOS 上的印象笔记/Evernote 客户端明文缓存是验证过的路径（含沙箱版）；
Windows 目录也在自动探测范围内，但手边没有环境实测。Linux 没有官方客户端，只能走口令路线。

**转出来的 Markdown 会丢东西吗？**
标题层级、粗体、列表、表格、待办（☑/☐）、链接、图片、代码都会保留。
`en-crypt`（笔记里单独加密的文本段）会留成占位符 —— 那段需要原口令，谁也解不了。

**为什么不用现成的 enex 转换工具？**
它们的转换部分都很好用，但遇到加密正文会直接跳过或留空。
本工具的价值在于「加密正文怎么办」这一段。

## 许可

MIT。随意使用、修改、转发。
