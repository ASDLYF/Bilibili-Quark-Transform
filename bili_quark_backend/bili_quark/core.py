"""共享核心：路径约定、筛选逻辑、状态读写、目录解析、原子写。

**筛选逻辑只在这里实现一次**（targets / classify），list、status、run、verify
四个命令全部调用它，保证「目标」的选取完全一致。
"""
import json
import os
import shutil
import tempfile
import threading
import time

from bili_dl import safe_name                       # 复用现有实现（不改）
from bili_quark.credentials import load_quark        # noqa: F401  (供外部复用)
from quark_upload import Quark, QuarkError          # noqa: F401

# ---------------------------------------------------------------- 默认值

# 与参数化前 run_all.py 完全一致的默认筛选行为：
#   BV1nm3w6uEAJ（784x544「扒舞自用」）默认排除
#   BV1g1he6bEAF（横屏正式投稿，用户确认要）默认包含
DEFAULT_EXCLUDE = ('BV1nm3w6uEAJ',)
DEFAULT_INCLUDE_HORIZONTAL = ('BV1g1he6bEAF',)

DEFAULT_DISK_MIN_GB = 12.0
DEFAULT_CONCURRENCY = 5
DEFAULT_PART_CONCURRENCY = 8


# ---------------------------------------------------------------- 路径布局

def default_project_dir():
    """deepseek_bilibili 目录（保持向后兼容的默认 workdir）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Layout:
    """workdir 内部布局。

    <workdir>/downloads/     下载完成的中转目录
    <workdir>/_tmp/          音视频分片临时目录
    <workdir>/state.jsonl    状态文件（可用 --state-file 覆盖）
    <workdir>/video_list.json 投稿清单（可用 --video-list 覆盖）
    <workdir>/progress.json  实时进度（原子写）
    <workdir>/run.log        日志
    <workdir>/config.json    夸克目录配置
    <workdir>/remote_index.json
                             最近一次 verify 落盘的「远端存在性」快照
                             （面板据此找出夸克上还缺哪些条目）
    """

    def __init__(self, workdir=None, video_list=None, state_file=None):
        self.workdir = os.path.abspath(workdir or default_project_dir())
        self.downloads = os.path.join(self.workdir, 'downloads')
        self.tmp = os.path.join(self.workdir, '_tmp')
        self.video_list = os.path.abspath(video_list or
                                          os.path.join(self.workdir, 'video_list.json'))
        self.state = os.path.abspath(state_file or
                                     os.path.join(self.workdir, 'state.jsonl'))
        self.progress = os.path.join(self.workdir, 'progress.json')
        self.runlog = os.path.join(self.workdir, 'run.log')
        self.config = os.path.join(self.workdir, 'config.json')
        self.remote_index = os.path.join(self.workdir, 'remote_index.json')
        self.bili_cookie = os.path.join(self.workdir, 'bili_cookie.txt')
        self.quark_cookie = os.path.join(self.workdir, 'quark_cookie.txt')

    def ensure_dirs(self):
        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.downloads, exist_ok=True)
        os.makedirs(self.tmp, exist_ok=True)

    def as_dict(self):
        return {'workdir': self.workdir, 'downloads': self.downloads, 'tmp': self.tmp,
                'video_list': self.video_list, 'state': self.state,
                'progress': self.progress, 'run_log': self.runlog,
                'config': self.config, 'remote_index': self.remote_index}


# ---------------------------------------------------------------- 原子写

def atomic_write_json(path, obj):
    """先写 .tmp 再 os.replace —— 读端永远不会看到半截 JSON。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))


def atomic_write_text(path, text):
    """原子写文本（UTF-8）：先写同目录 .tmp 再 os.replace。"""
    d = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.',
                               suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 清单 / 状态

def load_video_list(path):
    """读投稿清单。缺失返回 None；损坏抛 ValueError。"""
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def list_items(video_list):
    if not video_list:
        return []
    return video_list.get('items') or []


def classify(items, vertical_only=True, include_horizontal=()):
    """把清单拆成 竖屏/横屏/未知 三类。

    竖屏判定用清单里探测出来的 vertical 字段（该字段本身就是逐条调
    x/player/pagelist + x/player/wbi/playurl 拿 dash 的 width/height 得出的，
    不是投稿列表里的字段）。

    exclude 不在这里处理 —— 由 select_targets 统一负责，默认值的合并只在 CLI
    一处发生（否则 --exclude "" 无法清空默认排除）。
    """
    inc = set(include_horizontal or ())
    ver, hor, unk = [], [], []
    for x in items:
        if x.get('bvid') in inc:
            hor.append(x)                 # 显式包含的横屏，按横屏归类
        elif x.get('vertical') is True:
            ver.append(x)
        elif x.get('vertical') is False:
            hor.append(x)
        else:
            unk.append(x)
    out = list(ver + hor) if vertical_only else list(ver + hor + unk)
    return ver, hor, unk, out


def select_targets(items, vertical_only=True, exclude=(), include_horizontal=()):
    """**共享筛选函数**：list / status / run / verify 四处唯一入口。

    :param vertical_only: True = 只跑竖屏（默认）；False = 含横屏与未知
    :param exclude: 要剔除的 bvid 集合（**原样使用**；DEFAULT_EXCLUDE 的合并由
        CLI 的 resolve_exclude 负责，这样 --exclude "" 才能真正清空默认排除）
    :param include_horizontal: 强制按横屏纳入的 bvid（原样使用）
    """
    ex = set(exclude or ())
    inc = set(include_horizontal or ())
    ver, hor, unk, out = classify(items, vertical_only, inc)
    out = [x for x in out if x.get('bvid') not in ex]
    ver = [x for x in ver if x.get('bvid') not in ex]
    hor = [x for x in hor if x.get('bvid') not in ex]
    unk = [x for x in unk if x.get('bvid') not in ex]
    return {'vertical': ver, 'horizontal': hor, 'unknown': unk,
            'targets': out, 'excluded': sorted(ex),
            'include_horizontal': sorted(inc)}


def load_state(path):
    """state.jsonl → {bvid: record}，同 bvid 以最后一条为准（支持重跑覆盖）。"""
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get('bvid'):
                done[r['bvid']] = r
    return done


def status_of(rec):
    return (rec or {}).get('status') or 'pending'


def is_done(rec):
    return status_of(rec) == 'uploaded'


class StateWriter:
    """多线程写 state.jsonl：加锁（Windows 并发 open(...,'a') 会抛错）。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, rec):
        line = json.dumps(rec, ensure_ascii=False) + '\n'
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or '.',
                        exist_ok=True)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)
                f.flush()


# ---------------------------------------------------------------- 配置

def load_config(layout):
    if os.path.exists(layout.config):
        try:
            with open(layout.config, encoding='utf-8') as f:
                return json.load(f)
        except ValueError:
            return {}
    return {}


def save_config(layout, cfg):
    atomic_write_json(layout.config, cfg)


# ---------------------------------------------------------------- 磁盘

def disk_usage_gb(path):
    """(free_gb, total_gb, free_bytes, total_bytes) —— 目录不存在时上溯到已存在的祖先。"""
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    u = shutil.disk_usage(p or '.')
    return (u.free / 2**30, u.total / 2**30, u.free, u.total)


# ---------------------------------------------------------------- 夸克目录解析

def _is_dir(it):
    return bool(it.get('dir'))


def dir_children(q, fid, sub_dirs=1, pages=None):
    """列目录；pages 为 None 时走完整分页（list_all）。"""
    if pages is None:
        return q.list_all(fid, size=100, sub_dirs=sub_dirs)
    out = []
    for page in range(1, pages + 1):
        d = q.list_dir(fid, size=100, page=page, sub_dirs=sub_dirs)
        out.extend(d.get('list') or [])
    return out


def find_subdir(q, parent_fid, name):
    """在 parent 下按名字找同名目录（完整分页，绝不用 _total 判断末页）。"""
    for it in q.list_all(parent_fid, size=100, sub_dirs=1):
        if it.get('file_name') == name and _is_dir(it):
            return it
    return None


def resolve_path(q, path, create=True):
    """按 / 或 \\ 分隔从根目录（fid='0'）逐级走。

    每级先 list_all 查同名目录；找不到就 ensure_dir 创建（除非 create=False）。
    目录名里可能含 /，以最后一个分隔符切分即可（这里按需求直接全部分隔符切分）。
    """
    raw = (path or '').replace('\\', '/').strip()
    parts = [p.strip() for p in raw.split('/') if p.strip()]
    cur = '0'
    created = []
    cur_name = ''
    for part in parts:
        it = find_subdir(q, cur, part)
        if it:
            cur = it['fid']
            cur_name = part
            continue
        if not create:
            raise QuarkError('目录不存在（已加 --no-create，不创建）：%s（父 fid=%s）' % (part, cur))
        cur = q.ensure_dir(cur, part)
        created.append(part)
        cur_name = part
    return {'fid': cur, 'name': cur_name, 'path': '/'.join(parts), 'created': created}


def find_path_by_fid(q, fid, max_depth=4):
    """给 fid 反查路径：从根开始逐层找，找到返回 (path, name)，找不到返回 (None, None)。"""
    if fid == '0':
        return ('/', '根目录')
    start = q.list_all('0', size=100, sub_dirs=1)
    name = None
    for it in start:
        if it.get('fid') == fid:
            name = it.get('file_name')
            return ('/' + name, name)
    queue = [(it['fid'], '/' + it['file_name']) for it in start if _is_dir(it)]
    depth = 1
    while queue and depth < max_depth:
        nxt = []
        for parent_fid, parent_path in queue:
            for it in q.list_all(parent_fid, size=100, sub_dirs=1):
                if it.get('fid') == fid:
                    return (parent_path + '/' + it['file_name'], it.get('file_name'))
                if _is_dir(it):
                    nxt.append((it['fid'], parent_path + '/' + it['file_name']))
        queue = nxt
        depth += 1
    return (None, None)


def account_info(q):
    """账号信息：nickname / member_type / total_capacity / use_capacity。

    注意：夸克网盘 PC 接口 `/1/clouddrive/member` **不返回昵称**（只有 member_type /
    容量 / uhx6 之类的哈希 id；已实测 /member/info、/user/info、/account/info 等
    全部 404）。所以 nickname 正常情况就是 None，这里照实返回并给出
    account.nickname_source 说明，不编造。
    """
    r = q.api('GET', '/1/clouddrive/member')
    d = r.get('data') or {}
    return {
        'nickname': d.get('nickname'),
        'member_type': d.get('member_type'),
        'total_capacity': d.get('total_capacity'),
        'use_capacity': d.get('use_capacity'),
        'nickname_source': 'unavailable: quark /1/clouddrive/member does not expose a nickname',
    }


def root_dirs(q):
    out = []
    for it in q.list_all('0', size=100, sub_dirs=1):
        if _is_dir(it):
            out.append({'name': it.get('file_name'), 'fid': it.get('fid')})
    return out


def remote_files(q, fid):
    """远端实际文件列表（完整分页；绝不依赖 _total）。返回 [条目...]。"""
    return [it for it in q.list_all(fid, size=100, sub_dirs=0) if not _is_dir(it)]


# ---------------------------------------------------------------- Windows 删除重试

def remove_with_retry(path, attempts=6, log=None):
    """Windows 上刚读完/合并完的文件句柄可能没释放，删除要重试（6 次，逐步加长）。"""
    for attempt in range(attempts):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == attempts - 1:
                raise
            if log:
                log('文件被占用，%.1fs 后重试删除（%d/%d）' %
                    (1.5 * (attempt + 1), attempt + 1, attempts))
            time.sleep(1.5 * (attempt + 1))
    return False


# ---------------------------------------------------------------- 进度

def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')
