# evernote-enex-to-md

**English** · [中文](README.md)

Convert **Evernote / 印象笔记 (Yinxiang)** export archives (`.enex` / `.notes`) into clean Markdown:
one `.md` per note (with YAML front matter), every attachment extracted into an `附件/` folder,
plus a clickable index.

The key difference: **many of these archives ship with an encrypted note body**
(`<content encoding="base64:aes">`). Ordinary converters leave you with a pile of ciphertext.
This tool recovers the plaintext body **without knowing the passphrase** — as long as the
Evernote / Yinxiang desktop client has run on your machine (it caches the plaintext locally).

## Why this works

Exported archives use Evernote's own **ENC0** encryption: AES-128-CBC with a PBKDF2-derived key.
The passphrase is **not stored in the file**. AES itself is not broken, so the mainstream
conclusion online is "no passphrase, no recovery."

But the desktop client keeps a **plaintext** copy on disk so it can render the notes:

```
<account dir>/
├── localNoteStore/LocalNoteStore.sqlite   ← title / created date / local UUID
└── content/<ZLOCALUUID>/content.enml      ← plaintext body
```

This tool aligns the archive against that local plaintext by **title + creation time**, then
applies a hard check on the **PKCS#7 padding length**
(`len(local plaintext) + padding == len(ciphertext)`). Once that check passes, you know the
plaintext really is the matching one — not merely "looks similar". Far more reliable than
comparing titles alone.

## Applicable versions

**Verified (the only combination with a complete evidence trail):**

| Item | Value |
|---|---|
| Client | **印象笔记 Mac 9.8.7 (build 478777)** (bundle id `com.yinxiang.Mac`) |
| Exporter string | `application="Evernote" version="Evernote Mac 9.8.7 (478777)"` |
| Archive format | ENEX 3.0 (`evernote-export3.dtd`), body ENC0-encrypted |
| Local store model | Core Data `Unified_27_0` (31 entities / 34 tables, includes `ZENNOTE`) |

Related versions (other 印象笔记 Mac 9.x) will most likely work, but **verify it yourself first**.
Official Evernote 10.x (Electron) and the Windows client are untested. Linux has no official
client, so only the passphrase route is available there.

**How to verify** (check the structure, not the version number):

```bash
python3 scripts/find_store.py --json     # look at model_version / has_zenote / content_dirs
python3 scripts/enex_to_md.py "archive.notes" --dry-run --json   # look at alignment.score (>= 0.5 required)
```

The full version list, untested scope, and non-applicable cases live in **`compat.json`**
(machine-readable). Agents should read it before deciding whether to convert.

## Install

Requires Python 3.10+ (3.9 also works — the scripts are kept 3.9-compatible).

```bash
pip3 install -r requirements.txt          # dependencies (only needed for rendering / decryption)
```

**Installing as an agent skill**: copy the whole directory into `~/.workbuddy/skills/`
(user-level, available to every project) or `.workbuddy/skills/` inside a project
(travels with the repo). Restart the session, then saying
"convert my Evernote archive to markdown" will trigger it automatically.

```bash
./install.sh          # one-shot install into ~/.workbuddy/skills/ (optional)
```

**No skill mechanism required**: everything is a CLI, so any script or CI can call it directly.

## Usage

```bash
# One command, decides the route by itself (recommended)
python3 scripts/enex_to_md.py "notes.notes" --out "output"

# Health check first, writes nothing
python3 scripts/enex_to_md.py "notes.notes" --dry-run

# See which local client plaintext stores are available
python3 scripts/enex_to_md.py --list-stores

# Plaintext-only archive (no encryption) — plain conversion
python3 scripts/enex2md.py "archive.enex" --out "output"

# You have the passphrase
python3 scripts/enex_to_md.py "notes.notes" --password 'my passphrase' --out "output"
```

Output layout:

```
output/
├── 00-索引.md          # index of all notes, clickable; includes attachment-reference check
├── 01-note-title.md    # YAML front matter + body (headings/tables/todos/images)
├── 02-another-note.md
├── 附件/               # all original attachments, referenced as 附件/xxx.jpg
└── 转换报告.json       # machine-readable result (source / alignment score / unmatched list)
```

## Common scenarios

**"The converted body is garbled / empty"**
The archive body is encrypted. Run `--list-stores` to see whether a local plaintext cache
exists. If not, and you can log into the desktop client, do so and wait for those notes to
sync, then re-run.

**"No client installed and no passphrase"**
Attachments are never encrypted, so you can at least export those. The body can only be
guessed (`scripts/brute.py`) — by design, a forgotten passphrase is unrecoverable. A strong
passphrase is effectively hopeless, so set expectations accordingly.

**"The store was found, but it says these notes aren't in it"**
The local client is signed into a **different account**, or those notes haven't synced yet.

## Privacy

- The local client database is opened **read-only**. Nothing on your machine is modified,
  moved, or deleted.
- Fully **offline**: no uploads, no account required, no license check.
- Files are written only into the output directory you specify; the source archive is untouched.
- When sharing, share the tool (this directory) — not your converted notes.

## Files

| File | Purpose |
|---|---|
| `scripts/enex_to_md.py` | **Unified entry point**, picks the route automatically (use this one) |
| `scripts/find_store.py` | Cross-platform discovery of the client plaintext store |
| `scripts/crack2md.py` | Restore bodies from local plaintext (`--check` verifies only) |
| `scripts/enex2md.py` | Pure ENEX conversion / passphrase decryption |
| `scripts/brute.py` | Passphrase search (dictionary / charset brute force, multiprocess) |
| `scripts/enc0.py` | ENC0 payload parsing and passphrase verification (importable) |
| `scripts/selftest.notes` | Synthetic test sample — use it after code changes (passphrase `test123`) |
| `compat.json` | Version compatibility list (verified / untested / not applicable), machine-readable |
| `SKILL.md` | Skill description for agents (invocation contract and pitfalls) |

## FAQ

**Will it upload my notes?**
No. The whole pipeline is local; there is no network call anywhere in the code.

**Which platforms are supported?**
The macOS 印象笔记 / Evernote client plaintext cache is the verified path (including the
sandboxed copy). Windows directories are covered by auto-discovery, but were not tested —
no environment available. Linux has no official client, so only the passphrase route works.

**Does the Markdown output lose anything?**
Heading levels, bold, lists, tables, to-dos (☑/☐), links, images, and code are preserved.
`en-crypt` (individually encrypted text segments inside a note) become placeholders — that
segment needs its own passphrase, and nobody can crack it.

**Why not use an existing enex converter?**
Their conversion side is perfectly good, but they skip or blank out encrypted bodies.
The value here is specifically the "what to do about an encrypted body" part.

## License

MIT. Use, modify, and redistribute freely.
