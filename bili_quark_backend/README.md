# B 站 UP 主竖屏视频 → 夸克网盘 流水线

把某个 UP 主的竖屏视频批量下载成 4K mp4，并逐条上传到夸克网盘。

**本次已完成的成果**：UP 主「小圆脸」(mid `3546918307236545`) 的
**49 条视频**（48 竖屏 + 1 横屏）已全部上传到夸克目录 **`B站竖屏-小圆脸`**，
共 **20.70 GB**，逐条校验（文件名 + 字节数）全部一致，本地无残留。

---

## 怎么用

> **新增：参数化 CLI（推荐）** —— 下面的命令全部可以用新的后端 CLI 代替，
> 支持 `--workdir` 隔离、`--json-out`、`--mid` 参数化，见
> [`bili_quark/README.md`](bili_quark/README.md)：
>
> ```powershell
> python -m bili_quark.cli status --mid 3546918307236545
> python -m bili_quark.cli run --mid <MID> --workdir D:\wd\<MID> --dry-run
> ```
>
> 下面这套老命令（`fetch_list.py` / `run_all.py` / `final_check.py`）**原样保留、仍然可用**。

```powershell
cd D:\1\摄影\deepseek_bilibili

# 1) 抓取 UP 主全部投稿 + 探测每条真实分辨率（筛竖屏）
python fetch_list.py                 # 改 fetch_list.py 里的 MID 换人

# 2) 看清单（哪些已完成、哪些待处理）
python run_all.py --list

# 3) 配置夸克：先看账号和根目录文件夹，拿到目标 fid
python run_all.py --quark-info
python run_all.py --quark-dir <fid> --quark-dir-name "<名字>"

# 4) 跑流水线（下载 → 上传 → 校验 → 删本地），默认 4 路并发
python run_all.py --run --workers 4 --concurrency 8

# 5) 以夸克远端为准做最终核对
python final_check.py
```

中断后原样重跑即可：进度写入 `state.jsonl`，已完成的自动跳过。
只重跑某一条：`python run_all.py --run --only BV1xxxxxxxxx`

## 凭据

| 文件 | 内容 |
|---|---|
| `bili_cookie.txt` | B 站整行 cookie（含 `SESSDATA`、`bili_jct`） |
| `quark_cookie.txt` | 夸克网盘整行 cookie（必须含 `__pus` 和 `__puus`） |

两个文件都是整行粘贴，`#` 开头的行会被忽略。**别外传。**

## 文件说明

| 文件 | 作用 |
|---|---|
| `bili_quark/` | **新增** 参数化后端 CLI（`python -m bili_quark.cli`），见其 `README.md` |
| `bili_api.py` | B 站接口：WBI 签名、投稿列表、playurl |
| `bili_dl.py` | 下载器：选最高画质 + ffmpeg 合并成单 mp4 |
| `quark_upload.py` | 夸克上传器（纯标准库，含分片上传） |
| `run_all.py` | 主流水线，并发调度 + 状态记录 |
| `http_util.py` | 标准库 HTTP 客户端（沙箱下 requests 装不了） |
| `selftest_quark.py` | 离线自测（校验哈希状态机，不联网） |
| `final_check.py` | 最终核对：远端文件 vs 目标清单 |
| `video_list.json` | UP 主全量投稿 + 每条分辨率/时长 |
| `state.jsonl` | 逐条处理流水，断点续跑用 |
| `config.json` | 夸克目标目录 fid |

`downloads/` 是下载中转目录（空 = 全部已上传并清理）；`_tmp/` 是音视频临时候选区。

---

## 踩过的坑（改动前务必先读）

### 1. yt-dlp 在本账号下拿不到 4K
同一个 `playurl` 接口，直接调用能返回 `qn=120` 的 **2160×3840 / 21.6 Mbps**，
而 yt-dlp 只看到 1080p（试过换 UA、指定 `-f`，都无效）。
**所以下载器是自己写的**（`bili_dl.py`），别再回去折腾 yt-dlp。

### 2. OSS 响应头是 `ETag`，不是 `etag`
按小写 `r.headers.get('etag')` 取值会拿到空串，导致合并 XML 变成
`<ETag>""</ETag>`，服务端必报 **`InvalidPart`**。
`http_util.Resp.headers` 已改成大小写不敏感。

### 3. hash 状态机极慢，单分片上传直接绕开它
夸克多分片上传需要每片回传 OSS 的 SHA1 中间态（`x-oss-hash-ctx`），
这个状态 hashlib 不暴露，只能用纯 Python 实现 —— **实测约 0.9 MB/s，
453 MB 要跑 499 秒**，比上传还慢 3 倍。
**改用单分片 PUT 整个文件**后：hash 只要 **0.8 秒**（纯 C 的 hashlib）。
实测 501 MB 单分片上传正常，阈值设在 900 MB（`SINGLE_PART_MAX`）。

### 4. `_total` 字段不可靠 —— 曾因此误判"已全部完成"
夸克的目录列表接口经常返回 `_total=None/0`。用它判断"是否末页"会把
第一页误当成末页，**漏掉后面所有文件**。
现在 `list_all()` 一律以「本页返回条数 < 请求条数」为终止条件，
`find_file` 也走完整分页。**任何核对都必须以远端实际列表为准。**

### 5. 并发首传会触发连接中断
worker 同时首传时报 `URLError: [Errno 10053] 你的主机中的软件中止了一个已建立的连接`。
处理：worker 启动错开（`1.5 + slot*2.5` 秒随机抖动）+ 上传重试时**重新取签名**（旧签名可能已失效）。

### 6. 网盘目录列表有索引延迟
上传 `finish` 成功后立刻查列表可能查不到。校验要递进重试（2/3/5/8/12/20 秒），
再用 `find_recent`（按更新时间倒序查前几页）快速命中。

### 7. Windows 文件占用
刚读完/合并完的文件句柄可能没释放，`os.remove` 会抛 `WinError 32`。删除需重试 6 次。

### 8. 日志与 state 必须加锁
多线程并发 `open(...,'a')` 同一个文件在 Windows 上会抛错。两处都已加锁 + 容错。

---

## 实测数据（供估算参考）

| 项目 | 数值 |
|---|---|
| 目标 | 49 条（48 竖屏 + 1 横屏），总时长 2.2 小时 |
| 分辨率 | 源为 4K，实际取到 `2160×3840` 等，AVC 编码 16～34 Mbps |
| 总量 | 20.70 GB |
| 下载速度 | 单流 15～33 MB/s（并发时会被分摊） |
| 上传速度 | 单分片 1～4 MB/s |
| 全程耗时 | 约 1 小时（4 路并发，含中途两次重启） |

### 若换 UP 主，先估体积再看磁盘
`fetch_list.py` 会输出每条分辨率与时长。竖屏 4K AVC 大约
**每分钟素材 150～200 MB**。D 盘本次全程只剩 20～28 GB，
靠"下一条传一条删一条"才安全 —— **不要先全下再传**。

### 已知的横屏特例
UP 主的 2 条横屏里，`BV1nm3w6uEAJ`（784×544「扒舞自用」）按用户要求跳过，
跳过名单在 `run_all.py` 的 `SKIP_BVID`；`BV1g1he6bEAF` 是正式投稿，已包含。
