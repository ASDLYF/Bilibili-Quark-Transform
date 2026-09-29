# Bilibili Quark Transform（DSH 插件）

把 B 站某个 UP 主的投稿批量下载成单文件 mp4（**分辨率可选、多分集可选**），逐条上传到夸克网盘，
还能**拿 B 站源流的真实字节数核对网盘里的文件大小是否一致**。
以 DSH 工具的形式提供给 Agent 调用，后台作业执行、可断点续跑、可只重跑单条。

## 组成

| 部分 | 位置 | 作用 |
|---|---|---|
| 本插件（Host 侧） | `bili-quark-plugin/` | 注册 8 个工具 + 一个可视化面板 |
| Python 后端 | `bili_quark_backend/`（原 `deepseek_bilibili`） | 真正干事：WBI 签名、探测竖屏、下载合并、夸克上传、核对 |
| 工作目录 | `<workRoot>/<UID>/`（默认 scriptDir 同级 `_bili_quark_work`） | 每个 UP 主独立隔离：`state.jsonl` `progress.json` `remote_index.json` `downloads/` `_tmp/` `run.log` |
| 凭据 | 后端目录的 `bili_cookie.txt` / `quark_cookie.txt` | 也可由工具的 cookie 参数直接传（优先级更高） |

后端入口：`python -m bili_quark.cli <command>`（在 `bili_quark_backend` 目录下执行）。
本插件**不重新实现**下载/上传逻辑，那些实测踩过的坑全部保留在后端里。

## 八个工具

| 工具 | 用途 |
|---|---|
| `bili_quark_fetch` | 抓该 UP 主全部投稿 + 逐条探测真实分辨率/时长 → 生成清单（含每条的**分集列表**） |
| `bili_quark_targets` | 预览真正会被处理的条目、总时长、体积估算、磁盘检查（不下载） |
| `bili_quark_run` | 执行流水线（下载→上传→校验→删本地），后台作业立即返回；`force=true` 强制重跑、`maxHeight` 限分辨率、`page`/`only:["BV:2"]` 选分集 |
| `bili_quark_status` | 读进度：完成/成功/失败、正在处理哪条到哪一步、进程是否存活、日志尾部 |
| `bili_quark_verify` | 以夸克**远端实际列表**为准做最终核对（文件名前缀匹配 + 精确字节数），落盘「远端存在性快照」 |
| `bili_quark_check` | **拿 B 站源流的真实字节数**（Range 请求，不下载）逐个核对网盘文件大小，默认 1% 容差 |
| `bili_quark_auth` | 检查 B 站 / 夸克凭据是否还有效，并给出 B 站 cookie 的**真实过期时间** |
| `bili_quark_resolve` | 解析/创建夸克目标目录拿 fid，并保存为该 UP 主的默认目录 |

## 典型流程

```
1. bili_quark_fetch     { mid: "3546918307236545" }              # 生成清单
2. bili_quark_resolve   { mid: "...", path: "/B站上传/B站竖屏-小圆脸" }  # 拿 fid 并记住
3. bili_quark_targets   { mid: "..." }                            # 看体积估算 + 磁盘
4. bili_quark_run       { mid: "...", dryRun: true }              # 演练，确认清单
5. bili_quark_run       { mid: "...", concurrency: 5 }            # 真跑（后台）
6. bili_quark_status    { mid: "..." }                            # 查进度
7. bili_quark_verify    { mid: "..." }                            # 远端核对（相对本机记录）
8. bili_quark_check     { mid: "..." }                            # 与 B 站源核对大小（绝对口径）
```

中断后原样重跑第 5 步即可（进度在 `state.jsonl`，已完成的自动跳过）。
只重跑某条：`bili_quark_run { mid, only: ["BV1xxxxxxxxx"] }`。

**选分辨率 / 选分集**：

```
bili_quark_run { mid, maxHeight: 1080 }                    # 只下 1080p（文件名带 [1080p]）
bili_quark_run { mid, only: ["BV1Tiec6TEMx:2"] }           # 只下该视频第 2 集（文件名带 [P2]）
bili_quark_run { mid, page: 2 }                            # 所有多分集视频都下第 2 集
```

**补传夸克上缺的**（本机以为传过、远端其实没有）：

```
9.  bili_quark_verify   { mid: "..." }                       # 落盘远端快照
10. bili_quark_status   { mid: "..." }                       # 看 missing / missing_items
11. bili_quark_run      { mid: "...", only: ["BV1xxx"], force: true }   # 强制补传
```

`force: true` 会让后端**忽略 state.jsonl 里的「已完成」记录**，所以「勾了却不动」的情形不会再出现；
只跑 `only` 里点名的条目，不用带 `retryFailed`。面板里勾选后点「开始处理选中」已经默认带上这个语义。

## 可视化面板

插件带一个 Web 面板：左侧导航里有一个「B站→夸克」图标，点开是整页视图，每 2 秒自动刷新。

**可以直接在面板里输入**：

- **登录凭据**：两个密码输入框，整行粘贴 **B 站 Cookie**（需含 `SESSDATA`）与 **夸克 Cookie**
  （需含 `__puus`）。保存前会校验必需字段，缺失就拒绝写入；多行与 `#` 注释会被规整成一行。
  凭据写进 `scriptDir` 下的 `bili_cookie.txt` / `quark_cookie.txt`（与本插件一直以来的位置一致）。
  面板**只显示"已保存 / 未设置 / 来自环境变量"和字节数，从不回显 cookie 内容**，
  HTTP 接口也不会返回任何 cookie 片段。保存后新启动的下载/上传立即使用新凭据。
  - 凭据卡片还会显示**保存时间**与 B 站 cookie 里的 `bili_ticket_expires`（真实过期时间戳）；
    点「检测有效性」会调 B 站 nav + 夸克 member 接口，实时告诉你账号是否还有效、夸克容量多少
- **添加 UP 主**：只填 B 站 UID → 抓取投稿清单（不下载视频，约 1～3 分钟）。
  表单里**不再填目录**，添加成功后会自动切到该 UP 主，接着在下方目录卡片里设目标目录。
- **夸克目标目录**（唯一的目录入口）：当前 UP 主的目录显示在输入框里，填入路径后「保存目录」
  即解析并在夸克上按需创建；旁边「核对」会先保存再对夸克远端做一次核对
- 已有 UP 主可在标签之间切换（多于一个时才显示标签行）

推荐流程：**填凭据 → 添加 UP 主（抓清单）→ 填目录并「保存目录」→ 点「选择视频…」勾选要处理的**。

**视频选择浮层（点工具栏「选择视频…」打开）**：

- 视频清单**收进浮层**，主面板不再长期占屏；勾选状态按 UP 主存进 localStorage，刷新不丢
- 每条显示：勾选框、封面图、名称、上传日期、时长、分辨率、竖屏/横屏标记、已上传标记、
  远端标记（**远端已有** / **远端缺失**，核对过才显示）、BV 号
- **多分集视频有下拉菜单**：挑具体哪一集（P1/P2/…），选了自动勾上；P1 文件名保持老格式
  （兼容存量已上传文件），P2 及以上带 `[P2]` 后缀
- 分页每页 12 条；筛选「待处理 / 远端缺失 / 全部 / 已上传」（默认只看待处理的），
  标签上直接带条数
- **「待处理」以夸克远端为准**：有远端快照时，夸克上找不到的条目就算待处理 ——
  哪怕 `state.jsonl` 里记着「已上传」（被删、换目录、传丢了都属于这种）。
  「全选待处理」和「开始处理选中」会把这些一起选上并**强制重传**（忽略本机的已完成记录）；
  勾选里如果有远端已经有的条目，按钮旁会提示「其中 N 条远端已有，会重跑」
- 顶部「核对」按钮跑一次 `bili_quark_verify`：完整分页列一遍目标目录，落盘远端快照，
  然后自动刷新列表。快照会显示核对时间；**换了目标目录（fid 变了）快照自动作废**，
  面板会提示重新核对。没有快照时退回按 `state.jsonl` 判断，并在列表上方给出提示
- 批量：全选本页 / 全选待处理 / 全选远端缺失 / 清空；工具栏「开始处理选中(N)」只跑勾选的条目
- **横屏和竖屏都可以勾选**，不做方向限制：勾了就处理。连后端默认排除名单里那条
  （`BV1nm3w6uEAJ`「扒舞自用」）在勾选时也会被放行——否则勾了它也不会跑
- 不勾选时可用「开始全部待处理」跑整个队列，此时保留默认排除名单，也**不**带 force
  （只跑本机还没传过的）。要把夸克上缺的也一起补，用「全选待处理」→「开始处理选中」
- 封面走 B 站 CDN 缩略图，带 `referrerPolicy="no-referrer"`（否则防盗链会是一片灰）

**工具栏新增控件**：

- **分辨率下拉**：最高画质 / 2160p / 1440p / 1080p / 720p / 480p / 360p。
  按 B 站的清晰度语义（短边像素数）过滤流，所以竖屏 480p = 480×854 不会被当成 854p 过滤掉。
  降画质下载时文件名带 `[480p]` 之类后缀，最高画质保持原名，不会和已上传的重名。
  选择记住在 localStorage 里，下次打开还是上次选的
- **「与B站核对」按钮**：调 `bili_quark_check`，拿 B 站源流的真实字节数（Range 请求，不下载内容）
  跟网盘里的文件逐个比对，默认 1% 容差（合并封装开销通常 <0.1%）。结果摘要显示在工具栏下方
- **「检测有效性」按钮**（凭据卡片）：调 `bili_quark_auth`，验证 B 站 / 夸克 cookie 是否还能用，
  顺带回 B 站用户名、夸克会员类型与容量

**显示**：

- 每个 UP 主一条：状态（运行中 / 已中断 / 已完成 / 空闲）、进度条、已完成·总数、失败数、
  投稿数（竖屏/全部）、本地残留体积、目标夸克目录名
- **总进度条把在途条目算进去**：`(done + Σ 每条整体百分比/100) / total`。
  只用 `done/total` 的话并发跑时数字要等每条结束才跳，看着像卡住
- **正在处理的每条都有自己的阶段进度条**（下载 / 合并 / 上传 / 校验 / 删除）：
  条内百分比 + 细分说明（视频流 / 音频流 / 合并中 / 上传中 / 等待目录同步 i/n）
  + `已传 / 总字节` + 实时速度 + 该条的整体百分比
- 进度数字来源是真字节数，不是猜的：
  - **下载**：`bili_dl.download` 每收到一块就回报（节流 0.8s），视频流映射到 0→97%、
    音频流 97→100%、合并单独算一段
  - **上传**：`_oss_put` 改成流式 body（显式带 `Content-Length`），单分片 PUT 整文件
    也能看到百分比；>900MB 的多分片按「已发出字节」累加（并发下单调不回退）。
    流式这条路失败会自动退回原来的整块 PUT，不会因为要看进度把上传搞挂
  - **校验**：等目录索引同步时按轮次报进度（`等待目录同步 3/6`），不再是干等
- 日志尾部
- 按钮：刷新、添加 UP 主、选择视频…、开始处理选中、分辨率、演练、远端核对、与B站核对、
  开始全部待处理、停止（需点两次确认）

实现方式：Host 侧注册一条**只读**路由 `/bili-quark/api/*`（只返回进度与统计，**不返回任何 cookie**），
客户端侧 `client.js` 每 2 秒轮询它。注册走两条路，先试 Connection 的认证围栏内注册
（官方推荐姿势，`ctx.connection.fetch.register`），拿不到再退回 DSH 自带的 `webServer`
（`ctx.webServer.register`）——两者共用同一份请求处理逻辑，行为一致。

数据全部来自 `progress.json` / `state.jsonl` / `config.json` / `remote_index.json`，
所以面板与命令行看到的进度、以及对「夸克上还缺哪些」的判断始终一致。

面板不需要额外配置。若部署里两者都没有（例如非 Web 前端），面板会安静地不注册，
其余 8 个工具照常工作。

## 跨平台部署（Windows / macOS / Linux）

本插件与后端都不绑定某个操作系统。要装到另一台电脑：

1. 在源机器上执行 `python _tools/make_deploy.py`，会生成
   `dist/bili-quark-deploy/`，里面含插件、后端、`部署说明.md`、`manifest.json`
   （不含任何 cookie 与历史进度）。
2. 把整个目录拷到目标机器，按其中的 `部署说明.md` 操作。要点：
   - 目标机器需要 **Python 3.8+** 与 **ffmpeg / ffprobe**（macOS：`brew install ffmpeg`）
   - 编辑 `bili-quark-pipeline/cordis.patch.yml`，把 `scriptDir` 改成目标机器上的真实路径；
     `workRoot` 留空即可，插件会自动取 `scriptDir` 同级的 `_bili_quark_work`
   - 在 `bili_quark_backend/` 里放好 `bili_cookie.txt` 与 `quark_cookie.txt`
   - 让 Agent 用 `install_bundle` 安装插件目录，然后**重启 DSH Desktop**

平台差异都由 `platform.js` 统一处理，例如 Python 解释器查找（macOS 走 `python3`、
Windows 还会试 `py -3`）、结束进程树（Windows `taskkill /T`、类 Unix 杀进程组）、
ffmpeg 定位（环境变量 → PATH → 各平台常见安装位置）。这些在 Windows 上实测通过，
macOS / Linux 分支为逻辑与静态验证（本机无该环境）。

## 插件配置（在 profile 的插件设置或 cordis.patch.yml 里改）

| 字段 | 默认 | 说明 |
|---|---|---|
| `scriptDir` | 空 | Python 后端目录（含 `bili_quark` 包与 cookie 文件）。**留空或填错也能用**——插件会在插件目录的同级、`workRoot` 等位置自动寻找含 `bili_quark/cli.py` 的目录 |
| `workRoot` | 空 | 各 UP 主工作目录的根；留空则取 `scriptDir` 同级的 `_bili_quark_work` |
| `pythonPath` | 空 | 留空则依次尝试 `python3` / `python`（Windows 还会试 `py -3`），并探测 macOS Framework 与 Homebrew 路径 |
| `ffmpegPath` | 空 | 留空则后端自动查找（环境变量 → PATH → 各平台常见位置） |
| `ffprobePath` | 空 | 同上 |
| `diskMinGb` | 12 | 磁盘剩余低于此值拒绝启动流水线 |
| `startGraceMs` | 2500 | 启动后台作业后等待多久确认没立即崩溃 |
| `backendTimeoutMs` | 1200000 | 同步类调用（fetch/verify/resolve-dir）超时 |
| `maxLogTail` | 6000 | 回传给模型的日志尾部字符上限 |

## 两个必须知道的执行环境约束（实测）

1. **沙箱禁止用管道捕获子进程 stdout**（`spawn` 管道直接 `EPERM`），但允许子进程把结果写进文件。
   因此所有结构化结果都通过后端 `--json-out <文件>` 落盘后再读，插件完全不用管道。
2. **Cordis 用固定 URL 动态 import 插件模块，ESM 缓存使改动本文件后不会热加载**：
   改完 `index.js` 必须**重启 DSH Desktop** 才生效（禁用/启用 bundle 不够）。

## 常见问题

- **报「找不到可用的 Python」**：在插件配置里显式填 `pythonPath`（macOS 常见 `/opt/homebrew/bin/python3`）。
- **报「凭据缺失」**：确认 `scriptDir` 下有 `bili_cookie.txt` / `quark_cookie.txt`，
  或给工具的 `biliCookie` / `quarkCookie` 参数直接粘贴整行 cookie。
- **报「未找到 ffmpeg」**：安装 ffmpeg（macOS `brew install ffmpeg`），或把 `ffmpegPath` / `ffprobePath` 填成绝对路径。
- **夸克 cookie 必须含 `__puus`**，否则后端会拒绝（网盘 PC 接口要求）。
- **目录必须是完整路径**：例如目标在根目录下的「B站上传」里，应写
  `/B站上传/B站竖屏-小圆脸`，只写 `/B站竖屏-小圆脸` 会报「目录不存在」。
- **磁盘**：竖屏 4K AVC 约 150～200 MB/分钟素材，跑之前先看 `bili_quark_targets` 的估算。
  流水线是「下一条→传一条→删一条」的流式处理，不会同时堆满所有文件。
