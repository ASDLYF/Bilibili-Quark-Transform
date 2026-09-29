#!/usr/bin/env python3
"""把插件 + Python 后端打成一个可拷到别的电脑（Windows / macOS / Linux）的部署包。

用法：
    python _tools/make_deploy.py                    # 输出到 dist/bili-quark-deploy
    python _tools/make_deploy.py --out /tmp/pkg     # 指定输出目录
    python _tools/make_deploy.py --no-verify        # 跳过自检

刻意**不包含**（需在目标机器上自己放）：
    bili_cookie.txt / quark_cookie.txt   个人凭据，绝不该跟着包走
    state.jsonl / video_list.json        某个 UP 主的历史进度，换人无用
    _backup_参数化前/ _verify_out/ _tmp/ __pycache__/   备份与缓存
    downloads/                           中转目录，留空
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

# Windows 控制台默认 GBK，装不下中文与符号；统一按 UTF-8 输出，避免自检打印就崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 工作区根（含 bili-quark-plugin 与 deepseek_bilibili）
PLUGIN_NAME = 'bili-quark-plugin'
BACKEND_NAME = 'deepseek_bilibili'

# 插件侧要带的文件（相对 bili-quark-plugin）
PLUGIN_FILES = [
    'index.js',
    'platform.js',
    'api.js',
    'client.js',
    'package.json',
    'cordis.patch.yml',
    'icon.svg',
    'README.md',
    'locale/zh.json',
    'locale/en.json',
]

# 后端侧要带的文件（相对 deepseek_bilibili）
BACKEND_FILES = [
    'bili_api.py',
    'bili_dl.py',
    'quark_upload.py',
    'http_util.py',
    # 免安装的独立脚本（不依赖 bili_quark 包，可单独用）
    'run_all.py',
    'fetch_list.py',
    'final_check.py',
    'README.md',
    'bili_quark/__init__.py',
    'bili_quark/cli.py',
    'bili_quark/core.py',
    'bili_quark/credentials.py',
    'bili_quark/progress.py',
    'bili_quark/README.md',
]


def pathlib_join(*parts):
    return os.path.join(*parts)


def copy_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def to_posix(p):
    """把绝对路径转成适合写进 YAML 的形式（Windows 用反斜杠，其他用正斜杠）。"""
    if os.name == 'nt':
        return p.replace('/', '\\')
    return p.replace('\\', '/')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


def write_manifest(out_dir, plugin_dst, backend_dst):
    """重算清单：对包内所有文件记 sha256 前 16 位与字节数。"""
    manifest = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'generated_on': '%s (%s)' % (sys.platform, os.name),
        'plugin': PLUGIN_NAME,
        'backend': BACKEND_NAME,
        'files': {},
    }
    for base in (plugin_dst, backend_dst):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for fn in filenames:
                full = pathlib_join(dirpath, fn)
                rel = os.path.relpath(full, out_dir).replace('\\', '/')
                manifest['files'][rel] = {'sha256_16': sha256(full), 'bytes': os.path.getsize(full)}
    write_text(pathlib_join(out_dir, 'manifest.json'),
               json.dumps(manifest, ensure_ascii=False, indent=1) + '\n')
    return manifest


def build(out_dir, skip_verify=False):
    plugin_src = pathlib_join(ROOT, PLUGIN_NAME)
    backend_src = pathlib_join(ROOT, BACKEND_NAME)
    for d, label in ((plugin_src, '插件目录'), (backend_src, '后端目录')):
        if not os.path.isdir(d):
            raise SystemExit('找不到%s：%s' % (label, d))

    plugin_dst = pathlib_join(out_dir, PLUGIN_NAME)
    backend_dst = pathlib_join(out_dir, BACKEND_NAME)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(plugin_dst)
    os.makedirs(backend_dst)

    # ---- 拷贝插件
    missing = []
    for rel in PLUGIN_FILES:
        src = pathlib_join(plugin_src, *rel.split('/'))
        if not os.path.isfile(src):
            missing.append('plugin/' + rel)
            continue
        copy_file(src, pathlib_join(plugin_dst, *rel.split('/')))

    # ---- 拷贝后端
    for rel in BACKEND_FILES:
        src = pathlib_join(backend_src, *rel.split('/'))
        if not os.path.isfile(src):
            missing.append('backend/' + rel)
            continue
        copy_file(src, pathlib_join(backend_dst, *rel.split('/')))
    os.makedirs(pathlib_join(backend_dst, 'downloads'), exist_ok=True)

    # ---- 刻意不放 cookie 文件（连空模板也不放）
    # 模板容易被误当成"里面已经有凭据"，也让包里出现 cookie 文件名、混淆安全审计。
    # 凭据改由面板的「登录凭据」输入框写入，或由使用者手动创建。

    # ---- 按「包实际落地位置」生成插件配置
    # patch 的 name 必须是 package.json 里的真实包名（不是目录名），否则安装时 entry 解析不到模块
    pkg_name = PLUGIN_NAME
    try:
        with open(pathlib_join(plugin_src, 'package.json'), encoding='utf-8') as f:
            declared = (json.load(f) or {}).get('name')
        if declared and isinstance(declared, str):
            pkg_name = declared
    except Exception as e:
        print('  [提示] 读 package.json 的 name 失败（%s），回退用目录名 %s' % (e, PLUGIN_NAME))

    cfg = {
        'scriptDir': to_posix(backend_dst),
        # workRoot 留空：插件会自动取 scriptDir 同级的 _bili_quark_work
        'workRoot': '',
        'pythonPath': '',
        'ffmpegPath': '',
        'ffprobePath': '',
        'diskMinGb': 12,
        'startGraceMs': 2500,
        'backendTimeoutMs': 1200000,
        'maxLogTail': 6000,
    }
    patch_lines = ['- insert:', '    - id: %s' % pkg_name, "      name: '%s'" % pkg_name, '      config:']
    for k, v in cfg.items():
        if isinstance(v, bool):
            rendered = 'true' if v else 'false'
        elif isinstance(v, (int, float)):
            rendered = str(v)
        else:
            rendered = "'%s'" % v
        patch_lines.append('        %s: %s' % (k, rendered))
    write_text(pathlib_join(plugin_dst, 'cordis.patch.yml'), '\n'.join(patch_lines) + '\n')

    # ---- 清单文件（部署说明生成后再重算一次，见 main）
    write_manifest(out_dir, plugin_dst, backend_dst)

    return out_dir, plugin_dst, backend_dst, missing


def deploy_doc(out_dir, plugin_dst, backend_dst, missing):
    """生成随包走的部署说明（含本包实际路径，目标机器上对照修改即可）。"""
    interp = 'python3' if os.name != 'nt' else 'python'
    return f"""# B站竖屏 → 夸克网盘 部署说明

本包由工作区里的打包脚本（_tools/make_deploy.py）于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 生成。
包含两部分：`{PLUGIN_NAME}/`（DSH 插件）与 `{BACKEND_NAME}/`（Python 后端）。
**不含**任何 cookie 与历史进度，需在目标机器上自行准备。

## 0. 前置依赖

| 依赖 | 说明 |
|---|---|
| DSH Desktop | 已安装并能正常打开（插件运行在它里面） |
| Python | 3.8 以上。macOS/Linux 通常已有 `python3`；Windows 需单独装并勾选 Add to PATH |
| ffmpeg + ffprobe | 用于把音视频流合并成单个 mp4。**必须装** |

装 ffmpeg：

- macOS：`brew install ffmpeg`
- Windows：`winget install Gyan.FFmpeg`（或从 ffmpeg.org 下载解压后把 `bin` 加进 PATH）
- Ubuntu/Debian：`sudo apt install ffmpeg`

装好后用 `ffmpeg -version` 确认能跑。如果插件找不到它，可在插件配置的 `ffmpegPath` / `ffprobePath` 里直接填绝对路径。

## 1. 放置文件

把本目录整体拷到目标电脑的**同一个位置**（路径不要带空格更省事），例如：

- Windows：`D:\\bili-quark\\`（下面就是 `{PLUGIN_NAME}\\` 与 `{BACKEND_NAME}\\`）
- macOS / Linux：`~/bili-quark/`

**关键**：`{PLUGIN_NAME}/cordis.patch.yml` 里的 `scriptDir` 指向的是**打包机**上的路径：

```
scriptDir: '{to_posix(backend_dst)}'
```

到新机器后必须改成新机器上的真实路径，例如：

- Windows：`scriptDir: 'D:\\bili-quark\\{BACKEND_NAME}'`
- macOS：`scriptDir: '/Users/你的用户名/bili-quark/{BACKEND_NAME}'`

`workRoot` 留空即可——插件会自动取 `scriptDir` 同级的 `_bili_quark_work` 存放各 UP 主的进度与中转文件。

## 2. 放凭据

**本包刻意不含 cookie 文件**（连空模板也没有，以免被误当成"已带凭据"）。两种填法，任选其一：

**推荐：在面板里填**。装好插件并重启 DSH 后，打开左侧「B站→夸克」面板 → 「登录凭据」→ 填写并保存。
它会写入 `{BACKEND_NAME}/` 下的 `bili_cookie.txt` / `quark_cookie.txt`，并对必需字段做校验。

**手动：在 `{BACKEND_NAME}/` 下新建这两个文件**，整行粘贴：

| 文件 | 内容 | 必需字段 |
|---|---|---|
| `bili_cookie.txt` | B 站整行 cookie | `SESSDATA`、`bili_jct` |
| `quark_cookie.txt` | 夸克网盘整行 cookie | `__pus`、`__puus` |

获取方式：浏览器登录对应网站 → F12 → Network → 随便点一个请求 → 复制 Request Headers 里的 `Cookie:` 整行。
以 `#` 开头的行会被忽略。夸克 cookie 有效期较短，过期后要重新复制。

也可用环境变量 `BILI_COOKIE` / `QUARK_COOKIE`（优先级高于文件）。

## 3. 安装插件

在 DSH Desktop 里对它说：

> 用 install_bundle 安装 {plugin_dst}

也就是让 Agent 调用插件管理工具，目标填 `{PLUGIN_NAME}` 目录的**绝对路径**。
安装完成后 **重启 DSH Desktop**（这一步必须做：Cordis 用固定 URL 加载插件模块，
改了文件不重启不会生效），然后开一个新会话。

## 4. 验证安装

新会话里让 Agent 跑一次：

> 用 bili_quark_resolve 解析夸克目录，mid 填 `3546918307236545`，path 填 `/B站上传/B站竖屏-小圆脸`

能返回 fid 与账号容量就说明插件、Python、cookie 三者都通了。
（`mid` 换成你自己的 UP 主 UID；`path` 换成你自己的夸克目录，注意要写完整路径。）

## 5. 日常使用

| 想做什么 | 对 Agent 说 |
|---|---|
| 抓取某 UP 主全部投稿 | 用 `bili_quark_fetch`，mid=... |
| 看会处理多少条、多大体积 | 用 `bili_quark_targets`，mid=... |
| 真正开始下载上传 | 用 `bili_quark_run`，mid=... |
| 查进度 | 用 `bili_quark_status`，mid=... |
| 跑完后核对 | 用 `bili_quark_verify`，mid=... |
| 只重跑某条 | 用 `bili_quark_run`，mid=...，only=["BV号"] |

中断后原样重跑即可，已完成的会自动跳过。

## 6. 排错

| 现象 | 原因与处理 |
|---|---|
| 报「找不到可用的 Python」 | 在插件配置里把 `pythonPath` 填成解释器绝对路径（macOS 常见 `/opt/homebrew/bin/python3`） |
| 报「凭据缺失」 | `{BACKEND_NAME}/` 下缺 `bili_cookie.txt` 或 `quark_cookie.txt`，或文件为空 |
| 报「夸克 cookie 无效：必须包含 __puus」 | 夸克 cookie 复制不完整或已过期 |
| 报「目录不存在」 | 路径写得不完整。夸克目录要写完整层级，如 `/B站上传/B站竖屏-小圆脸` |
| 报「未找到 ffmpeg」 | 装 ffmpeg，或把 `ffmpegPath`/`ffprobePath` 填成绝对路径 |
| 报「磁盘空间不足」 | 竖屏 4K 约 150～200 MB/分钟素材，先清盘或调低面板里的 `diskMinGb` |
| 插件装上了但工具不出现 | 忘了重启 DSH Desktop，或没开新会话 |

## 7. 本包文件清单

见同目录 `manifest.json`（含每个文件的 sha256 前 16 位与字节数，便于校验拷贝完整性）。
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description='打包可移植的 B站→夸克 部署包')
    ap.add_argument('--out', default=pathlib_join(ROOT, 'dist', 'bili-quark-deploy'),
                    help='输出目录（会被清空重建）')
    ap.add_argument('--no-verify', action='store_true', help='跳过自检')
    args = ap.parse_args(argv)

    out_dir, plugin_dst, backend_dst, missing = build(args.out, args.no_verify)

    # 部署说明写进包里，然后重算清单（把说明本身也纳入）
    write_text(pathlib_join(out_dir, '部署说明.md'), deploy_doc(out_dir, plugin_dst, backend_dst, missing))
    write_manifest(out_dir, plugin_dst, backend_dst)

    print('部署包已生成：%s' % out_dir)
    print('  插件：%s' % plugin_dst)
    print('  后端：%s' % backend_dst)
    if missing:
        print()
        print('⚠ 以下文件在源目录里没找到，已跳过：')
        for m in missing:
            print('   ' + m)

    # ---- 自检：后端命令能不能跑起来（不联网、不下载）
    if not args.no_verify:
        print()
        print('自检：')
        py = sys.executable
        child_env = dict(os.environ)
        child_env['PYTHONIOENCODING'] = 'utf-8'
        child_env['PYTHONUTF8'] = '1'
        checks = [
            (['-m', 'bili_quark.cli', '--help'], 'CLI 入口可执行'),
            (['-c', 'import bili_api, bili_dl, quark_upload, http_util; print("ok")'], '模块导入'),
        ]
        for cmd, note in checks:
            p = subprocess.run([py] + cmd, cwd=backend_dst, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', env=child_env)
            ok = p.returncode == 0
            print('  %s %s' % ('[OK]  ' if ok else '[FAIL]', note))
            if not ok:
                print('    ' + (p.stderr or p.stdout or '').strip()[:400])
        p = subprocess.run([py, '-m', 'py_compile'] + [
            pathlib_join(backend_dst, *r.split('/')) for r in BACKEND_FILES if r.endswith('.py')
        ], capture_output=True, text=True, encoding='utf-8', errors='replace', env=child_env)
        print('  %s 语法检查（全部 .py）' % ('[OK]  ' if p.returncode == 0 else '[FAIL]'))
        if p.returncode != 0:
            print('    ' + (p.stderr or '').strip()[:400])

        # patch 的 name 必须等于 package.json 的包名，否则新机器上安装会解析不到模块
        try:
            with open(pathlib_join(plugin_dst, 'package.json'), encoding='utf-8') as f:
                pkg_name = json.load(f)['name']
            with open(pathlib_join(plugin_dst, 'cordis.patch.yml'), encoding='utf-8') as f:
                patch_src = f.read()
            same = ("name: '%s'" % pkg_name) in patch_src
            print('  %s patch 包名与 package.json 一致（%s）' % ('[OK]  ' if same else '[FAIL]', pkg_name))
        except Exception as e:
            print('  [FAIL] patch 包名自检异常：%s' % e)

        # 清理自检产生的 __pycache__，保持交付包干净
        for dirpath, dirnames, _ in os.walk(backend_dst):
            for d in list(dirnames):
                if d == '__pycache__':
                    shutil.rmtree(pathlib_join(dirpath, d), ignore_errors=True)
                    dirnames.remove(d)

    print()
    print('下一步：把 %s 整个目录拷到目标电脑，然后看里面的「部署说明.md」。' % out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
