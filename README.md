# B站投稿 → 夸克网盘 批量传输工具

> **Bilibili Quark Transform** — 把 B 站 UP 主的投稿批量下载为单文件 mp4，逐条上传到夸克网盘，支持与 B 站源流核对文件大小。

[![Version](https://img.shields.io/badge/version-1.4.0-blue)](./bili-quark-pipeline/package.json)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#跨平台部署)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## ✨ 功能特性

- 🎬 **批量抓取**：一键获取 UP 主全部投稿，自动探测真实分辨率与时长
- 📺 **分辨率可选**：最高画质 / 2160p / 1440p / 1080p / 720p / 480p / 360p
- 📑 **多分集支持**：可选择具体分集（P1/P2/…），文件名自动标记
- ⚡ **并发流水线**：下载 → ffmpeg 合并 → 上传夸克 → 校验 → 删本地，流式处理不堆满磁盘
- 🔄 **断点续跑**：进度持久化到 `state.jsonl`，中断后原样重跑自动跳过已完成
- ✅ **双重核对**：
  - `verify`：以夸克远端实际文件列表为准，比对文件名 + 精确字节数
  - `check`：向 B 站 CDN 发 Range 请求取真实字节数，换机器/丢 state 也能核对
- 🔐 **凭据管理**：面板内填写或文件放置，支持环境变量，从不回显 cookie 内容
- 🖥️ **可视化面板**：Web UI 实时进度、视频选择、分辨率切换、一键核对
- 🤖 **Agent 工具**：8 个 DSH 工具供 AI Agent 调用，自然语言驱动

## 📦 项目结构

```
bilibili_to_quark/
├── bili-quark-pipeline/     # DSH 插件（Host 侧）
│   ├── index.js             # 注册 8 个工具 + 面板路由
│   ├── client.js            # Web 面板客户端（每 2s 轮询进度）
│   ├── api.js               # 面板 HTTP 接口（只读，不返回 cookie）
│   ├── platform.js          # 跨平台适配（Python/ffmpeg 查找、进程管理）
│   ├── cordis.patch.yml     # DSH 插件配置
│   ├── package.json         # 插件元信息
│   └── locale/              # 国际化（zh/en）
├── bili_quark_backend/      # Python 后端（真正干活的部分）
│   ├── bili_quark/          # 参数化 CLI（python -m bili_quark.cli）
│   │   ├── cli.py           # CLI 入口
│   │   ├── core.py          # 核心逻辑
│   │   ├── credentials.py   # 凭据加载
│   │   └── progress.py      # 进度上报
│   ├── bili_api.py          # B 站接口（WBI 签名、投稿列表、playurl）
│   ├── bili_dl.py           # 下载器（选最高画质 + ffmpeg 合并）
│   ├── quark_upload.py      # 夸克上传器（纯标准库，含分片上传）
│   ├── http_util.py         # 标准库 HTTP 客户端
│   ├── run_all.py           # 老版主流水线（仍可用）
│   ├── fetch_list.py        # 老版抓取脚本（仍可用）
│   └── final_check.py       # 老版核对脚本（仍可用）
├── _tools/
│   └── make_deploy.py       # 打包部署脚本
└── 部署说明.md               # 详细部署指南
```

## 🚀 快速开始

### 前置依赖

| 依赖 | 说明 |
|---|---|
| [DSH Desktop](https://github.com/deepseek-ai/dsh) | 已安装并能正常打开（插件运行在它里面） |
| Python 3.8+ | macOS/Linux 通常已有；Windows 需单独装并勾选 Add to PATH |
| ffmpeg + ffprobe | 用于合并音视频流。**必须安装** |

安装 ffmpeg：

```bash
# macOS
brew install ffmpeg

# Windows
winget install Gyan.FFmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### 安装步骤

1. **克隆或下载本项目**到目标电脑

2. **编辑插件配置**：修改 `bili-quark-pipeline/cordis.patch.yml`，将 `scriptDir` 改为本机上的后端目录绝对路径：
   ```yaml
   config:
     scriptDir: '/你的路径/bilibili_to_quark/bili_quark_backend'
   ```

3. **放置凭据**（二选一）：
   - **推荐**：安装插件后在 DSH 面板中填写
   - **手动**：在 `bili_quark_backend/` 下创建：
     - `bili_cookie.txt`：B 站整行 cookie（需含 `SESSDATA`、`bili_jct`）
     - `quark_cookie.txt`：夸克网盘整行 cookie（需含 `__pus`、`__puus`）

   > 💡 获取方式：浏览器登录对应网站 → F12 → Network → 复制任意请求的 `Cookie:` 整行

4. **安装插件**：在 DSH Desktop 中对 Agent 说：
   > 用 install_bundle 安装 /你的路径/bilibili_to_quark/bili-quark-pipeline

5. **重启 DSH Desktop**（必须！Cordis 用固定 URL 加载插件，不重启不会生效）

6. **验证安装**：新会话中让 Agent 执行：
   > 用 bili_quark_resolve 解析夸克目录，mid 填 `3546918307236545`，path 填 `/B站上传/B站竖屏-小圆脸`

   能返回 fid 与账号容量就说明一切正常 ✅

## 🛠️ 八个 Agent 工具

| 工具 | 用途 |
|---|---|
| `bili_quark_fetch` | 抓取 UP 主全部投稿 + 逐条探测真实分辨率/时长 → 生成清单 |
| `bili_quark_targets` | 预览待处理条目、总时长、体积估算、磁盘检查（不下载） |
| `bili_quark_run` | 启动流水线（后台作业），支持 `force`/`maxHeight`/`page`/`only` 等参数 |
| `bili_quark_status` | 查看进度：完成/成功/失败、当前处理到哪一步、日志尾部 |
| `bili_quark_verify` | 以夸克远端实际列表为准做最终核对（文件名 + 字节数） |
| `bili_quark_check` | 拿 B 站源流真实字节数核对网盘文件大小（Range 请求，不下载） |
| `bili_quark_auth` | 检查 B 站/夸克凭据有效性，显示过期时间与账号信息 |
| `bili_quark_resolve` | 解析/创建夸克目标目录，返回 fid |

## 📋 典型使用流程

```text
1. bili_quark_fetch     { mid: "3546918307236545" }                    # 生成清单
2. bili_quark_resolve   { mid: "...", path: "/B站上传/B站竖屏-小圆脸" }  # 拿 fid
3. bili_quark_targets   { mid: "..." }                                  # 看体积估算
4. bili_quark_run       { mid: "...", dryRun: true }                    # 演练确认
5. bili_quark_run       { mid: "...", concurrency: 5 }                  # 真跑（后台）
6. bili_quark_status    { mid: "..." }                                  # 查进度
7. bili_quark_verify    { mid: "..." }                                  # 远端核对
8. bili_quark_check     { mid: "..." }                                  # 与 B 站源核对
```

中断后原样重跑第 5 步即可，已完成的自动跳过。

### 高级用法

```text
# 限分辨率
bili_quark_run { mid, maxHeight: 1080 }                    # 只下 1080p

# 指定分集
bili_quark_run { mid, only: ["BV1Tiec6TEMx:2"] }           # 只下该视频第 2 集
bili_quark_run { mid, page: 2 }                            # 所有多分集视频都下第 2 集

# 补传夸克上缺的（本机以为传过、远端其实没有）
bili_quark_verify   { mid: "..." }                          # 先核对
bili_quark_run      { mid: "...", only: ["BV1xxx"], force: true }  # 强制补传
```

## 🖥️ 可视化面板

插件自带 Web 面板，在 DSH 左侧导航点击「B站→夸克」即可打开：

- **凭据管理**：密码输入框填写 Cookie，保存前校验必需字段，从不回显内容
- **UP 主管理**：添加 UP 主 → 自动抓取投稿清单（约 1~3 分钟）
- **目录设置**：填入夸克路径，一键解析/创建
- **视频选择浮层**：封面、名称、时长、分辨率、远端状态一目了然
- **实时进度**：总进度条 + 每条的阶段进度条（下载/合并/上传/校验/删除）
- **分辨率下拉**：最高画质 ~ 360p，选择记住在 localStorage
- **一键核对**：「与B站核对」「检测有效性」按钮

面板每 2 秒自动刷新，数据来自 `progress.json` / `state.jsonl`，与命令行看到的进度始终一致。

## ⚙️ 插件配置

在 DSH profile 的插件设置或 `cordis.patch.yml` 中修改：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `scriptDir` | 空 | Python 后端目录。留空时插件会自动在同级目录寻找 |
| `workRoot` | 空 | 各 UP 主工作目录根；留空取 `scriptDir` 同级 `_bili_quark_work` |
| `pythonPath` | 空 | 留空自动探测（`python3` / `python` / `py -3`） |
| `ffmpegPath` | 空 | 留空自动查找 |
| `ffprobePath` | 空 | 同上 |
| `diskMinGb` | 12 | 磁盘剩余低于此值拒绝启动 |
| `startGraceMs` | 2500 | 启动后等待确认没立即崩溃的时间 |
| `backendTimeoutMs` | 1200000 | 同步类调用超时（20 分钟） |
| `maxLogTail` | 6000 | 回传给模型的日志尾部字符上限 |

## 🌍 跨平台部署

本项目不绑定操作系统，支持 Windows / macOS / Linux。

部署到新机器的推荐方式：

```bash
python _tools/make_deploy.py
```

会生成 `dist/bili-quark-deploy/`，包含插件、后端、部署说明和 manifest（不含任何 cookie 与历史进度）。拷贝到目标机器后按其中的 `部署说明.md` 操作即可。

平台差异由 `platform.js` 统一处理：
- Python 解释器查找（macOS 走 `python3`，Windows 还会试 `py -3`）
- 结束进程树（Windows `taskkill /T`，类 Unix 杀进程组）
- ffmpeg 定位（环境变量 → PATH → 各平台常见安装位置）

## ❓ 常见问题

| 现象 | 原因与处理 |
|---|---|
| 报「找不到可用的 Python」 | 在插件配置里把 `pythonPath` 填成解释器绝对路径（macOS 常见 `/opt/homebrew/bin/python3`） |
| 报「凭据缺失」 | `bili_quark_backend/` 下缺 cookie 文件，或文件为空 |
| 报「夸克 cookie 无效：必须包含 __puus」 | 夸克 cookie 复制不完整或已过期 |
| 报「目录不存在」 | 夸克目录要写完整层级，如 `/B站上传/B站竖屏-小圆脸` |
| 报「未找到 ffmpeg」 | 安装 ffmpeg，或在配置中填写绝对路径 |
| 报「磁盘空间不足」 | 竖屏 4K 约 150~200 MB/分钟，先清盘或调低 `diskMinGb` |
| 插件装上了但工具不出现 | 忘了重启 DSH Desktop，或没开新会话 |

## ⚠️ 已知限制与踩坑记录

1. **yt-dlp 拿不到 4K**：同一 playurl 接口直接调用能返回 4K，yt-dlp 只看到 1080p。所以**下载器是自己写的**（`bili_dl.py`）
2. **OSS ETag 大小写敏感**：按小写 `etag` 取值会拿到空串导致 `InvalidPart`，已修复为大小写不敏感
3. **hash 状态机极慢**：夸克多分片上传的 SHA1 中间态纯 Python 实现约 0.9 MB/s，改用单分片 PUT 整个文件后 hash 只要 0.8 秒（阈值 900MB）
4. **`_total` 字段不可靠**：夸克目录列表接口常返回 `_total=None/0`，现已改为以「本页返回条数 < 请求条数」为终止条件
5. **并发首传连接中断**：worker 启动错开随机抖动 + 上传重试时重新取签名
6. **网盘目录索引延迟**：上传成功后立刻查可能查不到，校验采用递进重试（2/3/5/8/12/20 秒）
7. **沙箱禁止管道捕获 stdout**：所有结构化结果通过 `--json-out <文件>` 落盘后读取

## 📊 实测参考数据

| 项目 | 数值 |
|---|---|
| 测试对象 | 49 条视频（48 竖屏 + 1 横屏），总时长 2.2 小时 |
| 分辨率 | 源 4K（2160×3840），AVC 编码 16~34 Mbps |
| 总量 | 20.70 GB |
| 下载速度 | 单流 15~33 MB/s |
| 上传速度 | 单分片 1~4 MB/s |
| 全程耗时 | 约 1 小时（4 路并发） |

> 💡 竖屏 4K AVC 大约 **每分钟素材 150~200 MB**。流水线是「下一条→传一条→删一条」的流式处理，不会同时堆满所有文件。

## 📄 License

MIT
