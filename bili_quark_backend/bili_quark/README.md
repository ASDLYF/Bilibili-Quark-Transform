# bili_quark —— 参数化后端 CLI

把原先硬编码的 `run_all.py` 流水线封装成可从命令行完整参数化的后端 CLI。
**入口（必须在 `deepseek_bilibili` 目录下执行）：**

```powershell
cd D:\1\摄影\deepseek_bilibili
python -m bili_quark.cli <command> [options]
```

子命令：`fetch` / `set-dir` / `resolve-dir` / `status` / `list` / `run` / `verify`。

原有的 `run_all.py` / `fetch_list.py` / `final_check.py` **未做任何改动**，仍然可用。

---

## 给上层宿主的调用约定（重要）

1. **`status` / `verify` / `resolve-dir` 的 stdout 只有那一个 JSON 对象**，日志全部走
   stderr（同时落 `<workdir>/run.log`）。可以直接 `JSON.parse(stdout)`。
2. 沙箱禁止管道捕获子进程输出时，用 **`--json-out <路径>`**：把结果 JSON 原子写
   （先写 `.tmp` 再 `os.replace`）到该文件，UTF-8、无 BOM，内容与 stdout **逐字节一致**。
   未指定时行为不变。
3. 所有 stdout/stderr 统一 UTF-8（`newline=''`，不做 `\r\n` 转换）。
4. 参数顺序**两种都支持**（`--mid`/`--workdir`/`--state-file`/`--video-list`/`--quark-dir`
   可以写在子命令之前或之后，子命令里的值优先）：
   ```
   ... --mid 3546918307236545 --workdir D:\wd status
   ... status --mid 3546918307236545 --workdir D:\wd
   ```
5. 退出码：`0` = 成功；`2` = 有失败（仅 `run`）；`1` = 参数/前置条件错误。

## 每个 UP 主隔离工作目录

`--workdir` 缺省 = `deepseek_bilibili` 目录本身（向后兼容）。布局：

```
<workdir>/downloads/       下载完成的中转目录
<workdir>/_tmp/            音视频分片临时目录
<workdir>/state.jsonl      状态文件（可用 --state-file 覆盖）
<workdir>/video_list.json  投稿清单（可用 --video-list 覆盖）
<workdir>/progress.json    实时进度（原子写）
<workdir>/run.log          日志
<workdir>/config.json      夸克目录配置（quark_dir_fid / quark_dir_name）
```

要给每个 mid 隔离状态，例如：

```
python -m bili_quark.cli fetch --mid <MID> --workdir D:\wd\<MID>
python -m bili_quark.cli run   --mid <MID> --workdir D:\wd\<MID>
```

## 凭据：环境变量优先，文件回退

| 环境变量 | 回退文件 |
|---|---|
| `BILI_COOKIE` | `<workdir>/bili_cookie.txt` |
| `QUARK_COOKIE` | `<workdir>/quark_cookie.txt` |

多行值会把非空行拼成一行，`#` 开头的行忽略。**夸克 cookie 必须含 `__puus`**，否则报错退出
（exit 1）。`bili_dl.FFMPEG` / `bili_dl.FFPROBE` 也支持环境变量覆盖：
`BILI_FFMPEG` / `BILI_FFPROBE`（未设置时按下面的查找顺序自动定位，不再写死 `D:\Shotcut\...`）。

## 跨平台（macOS / Linux）

整套代码可以拷到 Windows / macOS / Linux 上跑，ffmpeg 路径是**自动查找**的。

**依赖**：Python 3.8+、ffmpeg（必须含 `ffprobe`，二者由同一个包提供）。

- macOS：`brew install ffmpeg`；Python 用 `python3`（macOS 自带/官方安装器没有 `python` 命令）：
  ```bash
  cd ~/deepseek_bilibili
  python3 -m bili_quark.cli status --mid 3546918307236545
  ```
- Linux（Debian/Ubuntu）：`sudo apt install ffmpeg`；Arch：`sudo pacman -S ffmpeg`；
  Ubuntu 的 snap 版也可用：`sudo snap install ffmpeg`。

**ffmpeg / ffprobe 查找顺序**（`bili_dl._find_tool`，高 → 低）：

1. 环境变量 `BILI_FFMPEG` / `BILI_FFPROBE`（设了就直接用，**不检查是否存在**，保持原有语义）；
2. `PATH` 里的 `ffmpeg` / `ffprobe`（`shutil.which`）——`brew install ffmpeg`、`apt install ffmpeg`、
   Windows 上装了并加进 PATH 的情况都走这一条；
3. 各平台常见安装位置（存在才用）：
   - Windows：`D:\Shotcut\ffmpeg.exe`、`C:\ffmpeg\bin\ffmpeg.exe`、`%ProgramFiles%\ffmpeg\bin\ffmpeg.exe`
   - macOS：`/opt/homebrew/bin`（Apple Silicon）、`/usr/local/bin`（Intel）、`/opt/local/bin`（MacPorts）
   - Linux：`/usr/bin`、`/usr/local/bin`、`/snap/bin`
4. 都找不到时 `bili_dl.FFMPEG` / `FFPROBE` 回退成裸命令名 `'ffmpeg'` / `'ffprobe'`，
   **不会**在 `import` 时报错；真正调用时失败才由 `merge()` / `probe()` 打印含实际路径的提示。

任意位置（含非标准安装、多个版本并存）都可以直接指定：

```bash
export BILI_FFMPEG=/opt/homebrew/bin/ffmpeg
export BILI_FFPROBE=/opt/homebrew/bin/ffprobe
# Windows PowerShell:
# $env:BILI_FFMPEG = 'D:\tools\ffmpeg\bin\ffmpeg.exe'
```

> 注意：Windows 上原有的 `D:\Shotcut\ffmpeg.exe` / `ffprobe.exe` 默认值仍然会被探测，
> 所以老环境（本机已装 Shotcut 的那套）行为不变，无需设置任何环境变量。

## 命令

### `fetch --mid <MID>`
抓取该 UP 主全部投稿，**逐条**调 `x/player/pagelist` 拿 cid，再调
`x/player/wbi/playurl`（qn=127&fnval=4048&fourk=1，WBI 签名）用 dash 的 width/height
判定竖屏（`height > width`）——**不看投稿列表里的字段**。写 `<video-list>`。

### `set-dir --fid <FID> [--name <名字>]`
把夸克目标目录写进 `<workdir>/config.json`（键名 `quark_dir_fid` / `quark_dir_name`）。

### `resolve-dir --path <路径> | --fid <FID>`
- `--path` 按 `/` 或 `\` 从根（`fid='0'`）逐级走；每级先 `list_all` 查同名目录，找不到
  就 `ensure_dir` 创建（加 `--no-create` 则报错退出）。
- `--fid` 校验该 fid 存在并回填 name。
- 输出 schema：
```json
{"fid":"...","name":"...","path":"...","created":["新建的目录名"],
 "root_dirs":[{"name":"..","fid":".."}],
 "account":{"nickname":null,"member_type":"..","total_capacity":0,"use_capacity":0}}
```
> `account.nickname` 实测恒为 `null`：夸克 PC 接口 `/1/clouddrive/member` 不返回昵称
> （`/member/info`、`/user/info`、`/account/info` 等均 404），另有
> `account.nickname_source` 说明。不编造。

### `status [--mid <MID>]`
从 `<video-list>` + `state.jsonl` 计算，附 `shutil.disk_usage(<workdir>)`：
```json
{"mid":..,"total":49,"vertical":48,"horizontal":1,"unknown":0,
 "done":49,"failed":0,"pending":0,"done_bytes":..,"done_gb":20.7,
 "failed_items":[{"bvid":"..","error":".."}],
 "pending_items":[{"bvid":"..","title":"..","height":3840,"width":2160,"duration":123}],
 "disk_free_gb":28.9,"disk_total_gb":753.9}
```
`<video-list>` 不存在时对应字段为 `null` 且附 `"video_list_missing": true`。

### `list [--mid <MID>] [--json]`
人类可读清单（✓/✗ + bvid + 日期 + 时长 + 分辨率 + 标题）。

### `run --mid <MID>`
流式处理：**下一条 → 传一条 → 删一条**（不要先全下再传）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--quark-dir <FID>` | 读 config.json | 目标目录 |
| `--concurrency <N>` | 5 | 同时处理几条视频 |
| `--delete-local` / `--keep-local` | 删除 | 上传校验通过后是否删本地 |
| `--vertical-only` / `--include-horizontal` | 只竖屏 | 横竖屏筛选 |
| `--exclude BV1,BV2` | 默认排除 `BV1nm3w6uEAJ` | 排除名单，可重复；`--exclude ""` 清空默认 |
| `--only BV1,BV2` | — | 只跑这些，可重复/逗号分隔 |
| `--retry-failed` | 关 | 把 failed 的也当待处理 |
| `--part-concurrency <N>` | 8 | 单条上传时的分片并发数（原 `--workers` 语义） |
| `--disk-min-gb <GB>` | 12 | 启动前检查 `<workdir>` 所在盘，不足**报错退出并打印剩余空间** |
| `--limit <N>` | — | 只处理前 N 条 |
| `--dry-run` | 关 | 只 筛选 + 磁盘检查 + 输出待处理清单 JSON，不下载不上传 |
| `--table` | 关 | `--dry-run` 改人类可读表格 |
| `--json-out <路径>` | — | 真跑结束时原子写一份摘要 JSON |

`--dry-run` 输出：`{"dry_run":true,"mid":..,"workdir":..,"video_list":..,"state_file":..,
"vertical_only":true,"exclude":[..],"include_horizontal":[..],"keep_local":false,
"concurrency":5,"part_concurrency":8,"total":49,"todo":..,"disk_min_gb":12.0,
"disk_free_gb":..,"disk_total_gb":..,"disk_ok":true,"items":[{bvid,title,width,height,duration,date}]}`

### `verify [--mid <MID>]`
以夸克**远端实际文件列表**为准核对「文件名 + 精确字节数」：
```json
{"target":49,"confirmed":49,"missing":0,"size_mismatch":0,
 "remote_total_bytes":..,"remote_total_gb":20.7,
 "extra_files":[{"name":"..","size":..}],"local_residue":[{"name":"..","size":..}],
 "missing_items":["BV.."],"mismatch_items":[{"bvid":"..","expected":..,"remote":..}]}
```
期望文件名 = `safe_name(title, bvid)`，期望字节数取 `state.jsonl` 里该 bvid 的 `bytes`。

### 共享筛选（保证 list/status/run/verify 口径一致）
`core.select_targets()` 是唯一入口：默认 `vertical_only=True` +
排除 `DEFAULT_EXCLUDE = {BV1nm3w6uEAJ}` + 强制按横屏纳入
`DEFAULT_INCLUDE_HORIZONTAL = {BV1g1he6bEAF}`。
排除名单的默认值只在 CLI 的 `resolve_exclude()` 里合并一次（否则 `--exclude ""` 清不掉默认）。

## progress.json

`run` 每完成一条（成功或失败）就原子更新 `<workdir>/progress.json`：

```json
{"state":"running|finished|failed_start","mid":..,"total":49,"done":30,
 "ok":28,"fail":2,"current":[{"bvid":"..","title":"..","stage":"download|upload|verify|delete","percent":42.5}],
 "started":"2026-09-28 00:10:00","updated":"2026-09-28 00:20:00","last_error":null,"pid":12345}
```

- 写盘：先写同目录 `.tmp` 再 `os.replace`；多线程更新加锁。
- 磁盘不足等启动期错误 → `state="failed_start"` + `last_error`。
- 跑完 → `state="finished"`，`current` 清空。
- `percent` 为阶段粒度（阶段开始 0、阶段完成 100）；`bili_dl.download` 未暴露字节级回调，
  所以下载中的 percent 不按字节推进。

## 刻意保留的坑（改前必读）

这些是实测修好的，**不要"优化"回去**：

1. `http_util.Resp.headers` 大小写不敏感 —— OSS 返回的是 `ETag`，取小写会拿到空串，
   合并 XML 变成 `<ETag>""</ETag>` 必报 `InvalidPart`。
2. 夸克上传默认**单分片 PUT 整个文件**（`SINGLE_PART_MAX = 900MB`）。`x-oss-hash-ctx`
   纯 Python SHA1 状态机只有约 0.9 MB/s，只在大文件回退多分片时才用。
3. 目录分页**绝不用 `_total` 判末页**，一律以「本页返回条数 < 请求条数」为终止条件；
   `find_file` 走完整分页。（`_total` 常返回 0/None，用它会把第一页误判成末页而漏文件。）
4. 上传重试**必须重新取 OSS 签名**（旧 auth_key 可能失效）；worker 启动错开
   `1.5 + 序号*2.5 + random()*2` 秒，避免并发首传触发 `Errno 10053`。
5. 上传后校验递进重试 **2/3/5/8/12/20 秒**，先用 `find_recent`（更新时间倒序前几页），
   再回退全目录 `find_file`。目录列表有索引延迟。
6. Windows 文件占用：删除重试 6 次并捕获 `PermissionError`；日志与 state 写入必须加锁。
7. 下载带 `Referer: https://www.bilibili.com/`，用 `Range` 断点续传；
   ffmpeg 合并 `-c copy -movflags +faststart -map 0:v:0 -map 1:a:0`。
8. 竖屏判定逐条探测 `pagelist` + `x/player/wbi/playurl` 的 dash 宽高；下载选流同分辨率
   优先 `avc1`，音频选 `bandwidth` 最高。

## 仅测试用的开关（生产别用）

`run` 支持离线注入，用合成小 mp4 + 假上传器验证编排，**不产生任何下载/上传流量**：

```
--fake-download --fake-src <放合成 mp4 的目录> [--fake-transient-fail]
```

不带 `--fake-download` 时这些代码完全不参与生产路径。

## 回归测试脚本

`_verify_out/` 下（不在包内，可随时删）：

| 脚本 | 作用 |
|---|---|
| `final_acceptance.py` | 按验收清单逐条实测（help/status/verify/list/编译/磁盘守卫/兼容性） |
| `test_run_integration.py` | 合成小 mp4 跑通 `run` 的失败与成功两条路径、progress.json、删本地 |
| `test_cli.py` | list/status/run/verify 四处筛选口径一致、`--retry-failed`/`--only`/`--limit` |
| `test_credentials.py` | 环境变量优先、文件回退、`__puus` 校验 |
| `test_fetch_probe.py` | 联网验证 fetch 的探测口径（只探 1 条） |
| `test_legacy.py` | 原入口 `run_all.py` 等未被破坏 |
