"""bili_quark 命令行入口。

    python -m bili_quark.cli <command> [options]

约定：
  * status / verify / resolve-dir 的 stdout **只有那一个 JSON 对象**，日志一律走
    stderr（同时落 <workdir>/run.log）。上层可直接 JSON.parse(stdout)。
  * 所有 stdout/stderr 统一 UTF-8。
  * 退出码：0=成功；2=有失败；1=参数/前置条件错误。

已实测修复的坑全部保留在原模块里，本文件只做参数化与编排：
  - 响应头大小写不敏感（http_util）
  - 夸克默认单分片 PUT 整文件，仅大文件回退多分片（quark_upload）
  - 目录分页以「本页返回条数 < 请求条数」判末页（quark_upload.list_all）
  - 上传重试重新取 OSS 签名 + worker 启动错开延迟（quark_upload / 本文件）
  - 上传后递进重试校验 + find_recent 再回退 find_file（本文件）
  - Windows 删除重试 6 次、日志与 state 加锁（core / 本文件）
  - 下载带 Referer + Range 断点续传，ffmpeg -c copy -movflags +faststart（bili_dl）
  - 竖屏判定逐条探测 dash 宽高（本文件 fetch）
"""
import argparse
import json
import os
import random
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import bili_quark                                     # noqa: F401  修好 sys.path
from bili_api import Bili
from bili_dl import BiliDownloader, safe_name, name_prefix, pick_formats, remote_size
from bili_quark import core, credentials
from bili_quark.progress import Progress
from quark_upload import Quark, QuarkError

EXIT_OK = 0
EXIT_ARGS = 1
EXIT_FAILED = 2

COMMAND_NAMES = ('fetch', 'seasons', 'set-dir', 'resolve-dir', 'status', 'list', 'run',
                 'verify', 'check-src', 'check-cred', 'repair')


def _fmt_dur(sec):
    """秒 → "M:SS" / "H:MM:SS"。

    合集接口只给秒数，投稿列表给的是现成的 length 字符串，这里补一个换算。
    """
    try:
        sec = int(sec or 0)
    except Exception:
        return ''
    if sec <= 0:
        return ''
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


# ---------------------------------------------------------------- 输出/日志

def setup_io():
    """stdout/stderr 统一 UTF-8，避免 Windows 控制台代码页把中文 JSON 写坏。

    newline='' 关掉 Windows 的 \\n → \\r\\n 翻译，这样 stdout 打印的 JSON 与
    --json-out 写出的文件**逐字节一致**（上层可任选一路解析）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', newline='')
        except Exception:
            try:
                stream.reconfigure(encoding='utf-8')
            except Exception:
                pass


_LOG_LOCK = threading.Lock()


class Logger:
    """日志只走 stderr，绝不污染 stdout。

    注意：不再自行写入 run.log。JS 宿主已通过 fd 重定向把子进程的 stderr
    捕获到 run.log，如果这里再 open(run.log, 'a') 写一次，每条日志就会
    出现两遍，导致前端进度条在两个计数之间来回跳。
    """

    def __init__(self, logfile=None):
        # logfile 参数保留以兼容旧调用方，但不再使用
        self.logfile = None

    def __call__(self, msg, *a):
        if a:
            msg = msg % a
        line = '[%s][%s] %s' % (time.strftime('%H:%M:%S'),
                                threading.current_thread().name, msg)
        with _LOG_LOCK:
            try:
                sys.stderr.write(line + '\n')
                sys.stderr.flush()
            except Exception:
                pass


def out_json(obj, json_out=None):
    """把唯一的结果 JSON 写到 stdout。

    :param json_out: 若给出路径，**同时**原子写入该文件（UTF-8，先写 .tmp 再
        os.replace）。用于宿主沙箱禁止管道捕获子进程输出的场景——sandbox 允许
        子进程自己 open(...,'w') 写文件，只是不允许 stdio 管道。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=1) + '\n'
    if json_out:
        # 原子写：完整合法的单个 JSON 对象，读端永远看不到半截内容
        core.atomic_write_text(json_out, text)
    sys.stdout.write(text)
    sys.stdout.flush()


def add_json_out(sp):
    """所有需要返回结构化结果的命令都支持 --json-out <路径>。

    未指定时行为不变（只写 stdout）；指定时 stdout 照旧 + 文件同时写成。
    """
    sp.add_argument('--json-out', metavar='PATH', default=None,
                    help='把结果 JSON 原子写入该路径（UTF-8）。宿主沙箱禁止管道捕获 '
                         'stdout 时用；指定后 stdout 仍照常打印该 JSON')
    return sp


def die(msg, code=EXIT_ARGS, log=None):
    if log:
        log('错误：%s' % msg)
    else:
        sys.stderr.write('错误：%s\n' % msg)
    return code


# ---------------------------------------------------------------- 公共参数

def add_common(sp):
    sp.add_argument('--workdir', metavar='DIR', default=None,
                    help='工作目录，默认 deepseek_bilibili 目录本身；'
                         '内部使用 downloads/ _tmp/ state.jsonl progress.json run.log')
    sp.add_argument('--video-list', metavar='PATH', default=None,
                    help='投稿清单 JSON 路径，默认 <workdir>/video_list.json')
    sp.add_argument('--state-file', metavar='PATH', default=None,
                    help='状态文件路径，默认 <workdir>/state.jsonl')
    return sp


def add_top_level(sp):
    """顶层也接受公共参数，仅为让顶层 --help 显示这些选项。

    真正的合并逻辑在 main() 里：先单独解析一份「子命令之前」的参数，再补进
    子命令的命名空间（子命令里显式给的值优先）。
    """
    sp.add_argument('--workdir', metavar='DIR', default=None,
                    help='同子命令的 --workdir（也可以写在子命令之前）')
    sp.add_argument('--video-list', metavar='PATH', default=None,
                    help='同子命令的 --video-list（也可以写在子命令之前）')
    sp.add_argument('--state-file', metavar='PATH', default=None,
                    help='同子命令的 --state-file（也可以写在子命令之前）')
    sp.add_argument('--mid', metavar='MID', default=None,
                    help='UP 主 mid（也可以写在子命令之前）')
    sp.add_argument('--quark-dir', metavar='FID', default=None,
                    help='夸克目标目录 fid（也可以写在子命令之前）')
    return sp


def add_filter(sp, include_default=False):
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--vertical-only', dest='vertical_only', action='store_true',
                   default=not include_default, help='只处理竖屏（默认）')
    g.add_argument('--include-horizontal', dest='vertical_only', action='store_false',
                   help='连横屏一起处理')
    sp.add_argument('--exclude', action='append', default=[], metavar='BV1,BV2',
                    help='逗号分隔的排除 BV 号，可重复；传空串 --exclude "" 清空默认排除')
    return sp


def parse_bv_list(values):
    """把 ['BV1,BV2', 'BV3'] 展平成去重列表。传过空串则返回 None（表示清空默认）。"""
    if values is None:
        return []
    out, clear = [], False
    for v in values:
        if v is None:
            continue
        if not str(v).strip():
            clear = True
            continue
        for part in str(v).replace(';', ',').split(','):
            p = part.strip()
            if p:
                out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    if clear and not uniq:
        return None
    return uniq


def resolve_exclude(values):
    """返回实际生效的排除集合（默认含 DEFAULT_EXCLUDE）。"""
    got = parse_bv_list(values)
    if got is None:
        return set()
    return set(core.DEFAULT_EXCLUDE) | set(got)


def _pick_cid(item, want_page=1):
    """从清单条目的分集里挑出 (cid, page_tag)。

    page_tag 只在 P2 及以上才有值 —— P1 沿用「标题 [BV号].mp4」的老名字，
    和存量已上传文件对得上。没分集信息就是单集，返回原 cid。
    """
    pages = item.get('pages') or []
    want = int(want_page or 1)
    if not pages:
        return item.get('cid'), None
    for p in pages:
        if int(p.get('page') or 0) == want:
            return p.get('cid'), (want if want > 1 else None)
    first = pages[0]
    n = int(first.get('page') or 1)
    return first.get('cid'), (n if n > 1 else None)


def _cookie_field(cookie, key):
    """从整行 cookie 里取某个字段的值（没有就返回空串）。"""
    for part in (cookie or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k.strip() == key:
            return v.strip()
    return ''


def _cookie_expires(cookie, key='bili_ticket_expires'):
    """cookie 里带过期时间戳的字段（B 站的 bili_ticket_expires 是 Unix 秒）。"""
    v = _cookie_field(cookie, key)
    if not (v or '').isdigit():
        return ''
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(v)))
    except (ValueError, OSError):
        return ''


def _file_time(path):
    """cookie 文件的保存时间（我们的"最后更新"口径）。"""
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path)))
    except OSError:
        return ''


# ---------------------------------------------------------------- fetch

def _enumerate_items(b, mid, season_id, log):
    """枚举待探测的稿件清单（合集 or 全量投稿）。

    给了 season_id 就只枚举该合集，否则走全量投稿（空间投稿接口）。
    两个接口的条目字段不同：合集给 duration(秒) 且没有 description，
    这里统一成投稿列表那套字段，后面的探测流程完全不用改。
    """
    all_items, page, season_meta = [], 1, None
    while True:
        if season_id:
            v = b.season_archives(mid, season_id, page, 30)
            if v.get('code') != 0:
                raise RuntimeError('合集内容接口出错 page=%d code=%s msg=%s' % (
                    page, v.get('code'), v.get('message')))
            d = v['data'] or {}
            raw = d.get('archives') or []
            total = (d.get('page') or {}).get('total', 0)
            if season_meta is None:
                season_meta = d.get('meta') or {}
            lst = [{
                'bvid': a.get('bvid'),
                'aid': a.get('aid'),
                'title': a.get('title'),
                'created': a.get('pubdate') or a.get('ctime'),
                'length': _fmt_dur(a.get('duration')),
                'description': '',
                'pic': a.get('pic'),
                # 合集接口其实**没有** is_charging_arc（字段集和空间投稿完全不同），
                # 这里只是留个统一入口；合集里的充电专属只能靠「0 档流」兜底识别。
                'charging': bool(a.get('is_charging_arc')),
            } for a in raw]
        else:
            v = b.videos(mid, page, 30)
            if v.get('code') != 0:
                raise RuntimeError('投稿列表接口出错 page=%d code=%s msg=%s' % (
                    page, v.get('code'), v.get('message')))
            d = v['data']
            lst = d.get('list', {}).get('vlist', [])
            total = d.get('page', {}).get('count', 0)
        all_items.extend(lst)
        log('%s page %d: +%d（累计 %d / 总 %s）' % (
            '合集' if season_id else '列表', page, len(lst), len(all_items), total))
        if len(all_items) >= total or not lst:
            break
        page += 1
        time.sleep(0.8)
    return all_items, season_meta


def _unknown_rec(it):
    """探测彻底失败（连 rec 都没构造出来）时的兜底条目：标成「未知」。

    宁可留个未知条目，也不要因为一次异常让这条永远留在 pending 里、每批都重试一遍。
    """
    return {
        'bvid': it.get('bvid'), 'aid': it.get('aid'), 'cid': None,
        'title': it.get('title'), 'created': it.get('created'),
        'date': time.strftime('%Y-%m-%d', time.localtime(it.get('created') or 0)),
        'length': it.get('length'),
        'description': (it.get('description') or '')[:200],
        'pic': it.get('pic'),
        'width': None, 'height': None, 'duration': None,
        'pages': [], 'page_count': 1, 'vertical': None, 'ratio': None,
    }


def _probe_item(b, it, log):
    """探测单条稿件：pagelist 拿 cid → playurl 拿 dash 宽高。

    返回 ``(rec, locked)``，两者最多一个非 None：

    * ``rec``    —— 探测成功的清单条目（``vertical`` 可能是 None = 未知）
    * ``locked`` —— 服务端**明确**回了「一档流都没有」，说明当前 cookie 下不了，需剔除。
      playurl 全程抛异常属于网络问题，那属于「未知」，仍要出 rec ——
      不能因为一次抽风就把视频永久踢出清单。
    """
    bvid = it['bvid']
    cid = None
    pages = []
    try:
        pl = b.get('https://api.bilibili.com/x/player/pagelist?bvid=%s' % bvid)
        if pl.get('code') == 0 and pl.get('data'):
            # 顺手把全部分集记下来：面板要能给多 P 视频选具体下载哪一集
            for p in pl['data']:
                pages.append({
                    'cid': p.get('cid'),
                    'page': p.get('page') or (len(pages) + 1),
                    'part': (p.get('part') or '')[:120],
                    'duration': p.get('duration'),
                })
            cid = pages[0]['cid']
    except Exception as e:
        log('  pagelist 失败 %s：%s' % (bvid, e))
    info = None
    got_response = False
    is_charging = bool(it.get('charging') or it.get('is_charging_arc'))
    if cid:
        attempt = 0
        while True:
            attempt += 1
            try:
                info = b.playurl(bvid, cid)
                got_response = True
                if info and info.get('streams'):
                    break
                # code=0 但 dash 一档流都没有 = 这个 cookie 没有下载权限。
                # 这是服务端**明确**的答复，不是网络抖动，复核一次就够 ——
                # 否则一个 UP 有几十条充电视频时，长退避会白等几十分钟。
                if attempt >= 2:
                    log('  playurl 无可用流 %s（复核 %d 次，判定不可下载）' % (bvid, attempt))
                    break
                log('  playurl 无可用流 %s（复核中…）' % bvid)
                time.sleep(1)
            except Exception as e:
                # 抛异常才可能是网络抖动，保守多试几次
                got_response = False
                info = None
                if attempt >= 3:
                    log('  playurl 失败 %s（第 %d 次，放弃）：%s' % (bvid, attempt, e))
                    break
                log('  playurl 失败 %s（第 %d 次）：%s' % (bvid, attempt, e))
                time.sleep(2 + attempt * 3)
    # 接口明确回了「没有流」→ 这个 cookie 下不了，剔除，不写进清单
    if got_response and not (info or {}).get('streams'):
        reason = ('充电专属（当前账号未充电，无下载权限）' if is_charging
                  else '无可用流（充电专属 / 大会员专享 / 地区受限）')
        return None, {'bvid': bvid, 'title': it.get('title'), 'reason': reason}
    rec = {
        'bvid': bvid,
        'aid': it.get('aid'),
        'cid': cid,
        'title': it.get('title'),
        'created': it.get('created'),
        'date': time.strftime('%Y-%m-%d', time.localtime(it.get('created') or 0)),
        'length': it.get('length'),
        'description': (it.get('description') or '')[:200],
        'pic': it.get('pic'),
        'width': info['width'] if info else None,
        'height': info['height'] if info else None,
        'duration': info['duration'] if info else None,
        'pages': pages,
        'page_count': len(pages) or 1,
    }
    if rec['width'] and rec['height']:
        rec['vertical'] = rec['height'] > rec['width']
        rec['ratio'] = round(rec['width'] / rec['height'], 4)
    else:
        rec['vertical'] = None
        rec['ratio'] = None
    return rec, None


def cmd_fetch(args):
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    layout.ensure_dirs()
    log = Logger(layout.runlog)
    if args.mid is None:
        return die('fetch 必须给 --mid <MID>', log=log)
    try:
        cookie = credentials.load_bili(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)
    b = Bili(cookie)
    mid = int(args.mid)
    season_id = getattr(args, 'season_id', None)
    season_id = int(season_id) if season_id else None
    season_meta = None
    limit = getattr(args, 'limit', None) or 0
    refresh = bool(getattr(args, 'refresh', False))
    concurrency = max(1, min(16, int(getattr(args, 'probe_concurrency', None) or 4)))

    # 0) 已有清单 & 已缓存的投稿枚举。
    #    大 UP 动辄几千条投稿，逐条 playurl 探测要几十分钟，远超宿主的同步调用超时
    #    （backendTimeoutMs，默认 20 分钟）。所以这里做两件事：
    #      * 把「投稿枚举」缓存到 workdir/video_raw.json —— 重新枚举要翻几十页接口，
    #        分批抓取时每批都重翻一遍太浪费；
    #      * 每次最多探测 --limit 条，进度直接落在 video_list.json 里，可断点续抓。
    #    上层按结果里的 pending 循环调用，直到 pending=0。
    prev = {}
    if os.path.exists(layout.video_list):
        try:
            with open(layout.video_list, encoding='utf-8') as fh:
                prev = json.load(fh) or {}
        except Exception as e:
            log('读取已有清单失败（当作空的）：%s' % e)
            prev = {}
    # 清单来源换了（全量投稿 ↔ 某个合集）就整体作废，否则会把另一个来源的条目混进来
    if (prev.get('season_id') or None) != (season_id or None):
        prev = {}

    raw_cache = os.path.join(workdir, 'video_raw.json')
    all_items = None
    if not refresh and os.path.exists(raw_cache):
        try:
            with open(raw_cache, encoding='utf-8') as fh:
                rc = json.load(fh) or {}
            if (rc.get('season_id') or None) == (season_id or None) and rc.get('items'):
                all_items = rc['items']
                season_meta = rc.get('season_meta') or {}
                log('复用已缓存的投稿枚举 %d 条（%s）；要重新拉列表请用 --refresh'
                    % (len(all_items), raw_cache))
        except Exception as e:
            log('读取投稿枚举缓存失败，将重新拉取：%s' % e)

    # 1) 拉取待探测的稿件清单（没有缓存，或用户要求 --refresh）
    if all_items is None:
        try:
            all_items, season_meta = _enumerate_items(b, mid, season_id, log)
        except Exception as e:
            return die('%s失败：%s: %s' % ('拉取合集内容' if season_id else '拉取投稿列表',
                                         type(e).__name__, e), log=log)
        core.atomic_write_json(raw_cache, {
            'mid': mid, 'season_id': season_id,
            'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'enum_total': len(all_items), 'items': all_items,
            'season_meta': season_meta or {},
        })

    # 1.5) 充电专属：接口不报错（playurl 返回 code=0、dash 却是空的），所以它既进不了
    #      「失败」也拿不到分辨率，会被扔进「未知」桶 —— 用户一开 --include-horizontal，
    #      「未知」就会被算进目标，跑到下载环节才炸。必须在筛选期就处理掉。
    #
    #      **但绝不能只看 is_charging_arc 就剔人**：那个字段说的是"这条投稿是充电专属"，
    #      跟"当前这个 cookie 有没有权限"完全是两回事。用户如果确实给这个 UP 充过电，
    #      playurl 是照常返回流的，那种视频必须照下不误。唯一权威的判据是
    #      playurl 到底有没有返回流（见 _probe_item）；is_charging_arc 只用来给剔除原因
    #      贴标签、并在无流时短路掉多余的重试。
    probed = {x['bvid']: x for x in (prev.get('items') or []) if x.get('bvid')}
    locked_map = {x['bvid']: x for x in (prev.get('locked_items') or []) if x.get('bvid')}
    in_enum = set(it['bvid'] for it in all_items)
    # 枚举里已经没有的（UP 删了稿）直接丢掉，别让旧条目一直挂在清单里
    probed = dict((k, v) for k, v in probed.items() if k in in_enum)
    locked_map = dict((k, v) for k, v in locked_map.items() if k in in_enum)
    if refresh:
        # --refresh 的语义 = 「重新抓取清单」：用户充完电要能把之前剔掉的充电视频捡回来，
        # 所以清空 locked 记录让它们重新走一遍探测；已经探测成功的不必重跑。
        locked_map = {}

    todo = [it for it in all_items
            if it['bvid'] not in probed and it['bvid'] not in locked_map]
    if limit > 0 and len(todo) > limit:
        log('本次只探测前 %d 条（还有 %d 条留到下一批）' % (limit, len(todo) - limit))
        todo = todo[:limit]
    log('待探测 %d 条（枚举共 %d 条，已有结果 %d 条，已剔除 %d 条，并发 %d）' % (
        len(todo), len(all_items), len(probed), len(locked_map), concurrency))

    # 2) 探测真实分辨率/时长
    #    竖屏判定**不看投稿列表字段**：逐条调 x/player/pagelist 拿 cid，
    #    再调 x/player/wbi/playurl（qn=127&fnval=4048&fourk=1，WBI 签名）
    #    用 dash 的 width/height 判断 height > width。
    #    并发跑：每条 2 个请求 + 一点间隔，几千条串行必然超过宿主超时；
    #    每个线程用**自己的 Bili 实例**（WBI key 缓存在实例属性上，不共享可变状态）。
    new_recs, new_locked, done = [], [], 0
    if todo:
        tls = threading.local()

        def _worker(item):
            if not hasattr(tls, 'bili'):
                tls.bili = Bili(cookie)
            return _probe_item(tls.bili, item, log)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = dict((ex.submit(_worker, it), it) for it in todo)
            for fu in as_completed(futs):
                it = futs[fu]
                done += 1
                try:
                    rec, lk = fu.result()
                except Exception as e:
                    log('  探测异常 %s：%s: %s' % (it.get('bvid'), type(e).__name__, e))
                    rec, lk = _unknown_rec(it), None
                title = (it.get('title') or '')[:28]
                if lk:
                    new_locked.append(lk)
                    log('[%d/%d] %s 剔除：%s %s' % (
                        done, len(todo), lk['bvid'], lk['reason'], title))
                elif rec:
                    new_recs.append(rec)
                    log('[%d/%d] %s %sx%s %s %s' % (
                        done, len(todo), rec['bvid'], rec['width'], rec['height'],
                        '竖屏' if rec['vertical'] else
                        ('横屏' if rec['vertical'] is False else '未知'), title))
                else:
                    log('[%d/%d] %s 探测无结果，留到下一批重试' % (done, len(todo), it.get('bvid')))

    # 3) 合并：按枚举顺序重排，保证清单顺序稳定（前端按顺序展示、挑分集）
    for rec in new_recs:
        probed[rec['bvid']] = rec
        locked_map.pop(rec['bvid'], None)
    for x in new_locked:
        locked_map[x['bvid']] = x
        probed.pop(x['bvid'], None)
    out = [probed[it['bvid']] for it in all_items if it['bvid'] in probed]
    locked = [locked_map[it['bvid']] for it in all_items if it['bvid'] in locked_map]
    pending = [it for it in all_items
               if it['bvid'] not in probed and it['bvid'] not in locked_map]

    if locked:
        # 充电视频可能几十上百条，日志只列前 20 条，避免刷屏；完整清单在 locked_items 里
        log('以下 %d 条下不了，已剔除、不写入清单：' % len(locked))
        for x in locked[:20]:
            log('  - %s %s（%s）' % (x['bvid'], (x.get('title') or '')[:30], x.get('reason')))
        if len(locked) > 20:
            log('  … 另有 %d 条，见清单文件的 locked_items' % (len(locked) - 20))

    payload = {'mid': mid, 'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
               'count': len(out), 'enum_total': len(all_items),
               'pending': len(pending), 'items': out}
    if locked:
        payload['locked'] = len(locked)
        payload['locked_items'] = locked
    if season_id:
        # 记下来源合集：前端要显示"当前清单来自哪个合集"，重抓时也能回填
        payload['season_id'] = season_id
        payload['season_name'] = (season_meta or {}).get('name') or ''
    core.atomic_write_json(layout.video_list, payload)

    ver = [x for x in out if x['vertical'] is True]
    hor = [x for x in out if x['vertical'] is False]
    unk = [x for x in out if x['vertical'] is None]
    log('本批探测 %d 条 → 清单已有 %d 条：竖屏 %d / 横屏 %d / 未知 %d%s%s' % (
        len(todo), len(out), len(ver), len(hor), len(unk),
        ('，剔除不可下载 %d 条' % len(locked)) if locked else '',
        ('；还剩 %d 条未探测（请再抓一次）' % len(pending)) if pending else ''))
    result = {'mid': mid, 'count': len(out), 'vertical': len(ver),
              'horizontal': len(hor), 'unknown': len(unk),
              'locked': len(locked), 'locked_items': locked,
              'enum_total': len(all_items), 'pending': len(pending),
              'has_more': bool(pending), 'processed': len(todo),
              'video_list': layout.video_list,
              'items': [{'bvid': x['bvid'], 'title': x['title'],
                         'width': x['width'], 'height': x['height'],
                         'duration': x['duration'], 'vertical': x['vertical']}
                        for x in out]}
    if season_id:
        result['season_id'] = season_id
        result['season_name'] = (season_meta or {}).get('name') or ''
    # --json-out 指出路径时，无论是否加 --json 都写文件（stdout 一并保留 JSON）
    if args.json or args.json_out:
        out_json(result, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- seasons

def cmd_seasons(args):
    """列出 UP 主的合集/系列（只读一次接口，不探测，秒回）。

    前端「添加 UP 主 / 抓取投稿清单」会先调这个：
    有合集就先让用户选一个，再用 fetch --season-id <id> 抓该合集。
    """
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    layout.ensure_dirs()
    log = Logger(layout.runlog)
    if args.mid is None:
        return die('seasons 必须给 --mid <MID>', log=log)
    try:
        cookie = credentials.load_bili(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)
    b = Bili(cookie)
    mid = int(args.mid)

    try:
        # page_size 上限就是 20：给 50 会被拒（code=-400 请求错误）
        d = b.seasons(mid, 1, 20)
    except Exception as e:
        return die('拉取合集列表失败：%s: %s' % (type(e).__name__, e), log=log)
    if d.get('code') != 0:
        return die('合集列表接口出错 code=%s msg=%s' % (
            d.get('code'), d.get('message')), log=log)

    il = (d.get('data') or {}).get('items_lists') or {}
    seasons = []
    # seasons_list 是「合集」，series_list 是「系列」（旧版列表），两者都收
    for kind, key in (('season', 'seasons_list'), ('series', 'series_list')):
        for it in (il.get(key) or []):
            m = it.get('meta') or {}
            sid = m.get('season_id')
            if sid is None:
                continue
            seasons.append({
                'season_id': sid,
                'name': m.get('name') or '',
                'total': m.get('total') or 0,
                'cover': m.get('cover') or '',
                'description': (m.get('description') or '')[:200],
                'kind': kind,
            })

    log('合集 %d 个（mid=%s）' % (len(seasons), mid))
    for s in seasons:
        log('  %s | %s | %s 条' % (s['season_id'], (s['name'] or '')[:30], s['total']))

    result = {'mid': mid, 'count': len(seasons), 'seasons': seasons}
    if args.json or args.json_out:
        out_json(result, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- set-dir

def cmd_set_dir(args):
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    layout.ensure_dirs()
    log = Logger(layout.runlog)
    cfg = core.load_config(layout)
    cfg['quark_dir_fid'] = args.fid
    cfg['quark_dir_name'] = args.name or ''
    core.save_config(layout, cfg)
    log('已设定目标目录 fid=%s name=%s → %s' % (args.fid, args.name or '', layout.config))
    out_json({'quark_dir_fid': cfg['quark_dir_fid'],
              'quark_dir_name': cfg['quark_dir_name'],
              'config': layout.config}, getattr(args, 'json_out', None))
    return EXIT_OK


# ---------------------------------------------------------------- resolve-dir

def cmd_resolve_dir(args):
    if not args.path and not args.fid:
        return die('必须给 --path 或 --fid')
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    try:
        cookie = credentials.load_quark(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)
    q = Quark(cookie)
    try:
        acct = core.account_info(q)
        roots = core.root_dirs(q)
        if args.path:
            r = core.resolve_path(q, args.path, create=not args.no_create)
            fid, name, path, created = r['fid'], r['name'], r['path'], r['created']
        else:
            path, name = core.find_path_by_fid(q, args.fid)
            fid, created = args.fid, []
    except QuarkError as e:
        return die('夸克目录解析失败：%s' % e, log=log)
    except Exception as e:
        return die('夸克目录解析失败：%s: %s' % (type(e).__name__, e), log=log)

    out_json({
        'fid': fid,
        'name': name,
        'path': path,
        'created': created,
        'root_dirs': roots,
        'account': acct,
    }, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- status

def cmd_status(args):
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    vl = core.load_video_list(layout.video_list)
    missing = vl is None
    mid = args.mid if args.mid is not None else (vl or {}).get('mid')

    ex = resolve_exclude(args.exclude)
    sel = core.select_targets(core.list_items(vl),
                              vertical_only=args.vertical_only, exclude=ex,
                              include_horizontal=core.DEFAULT_INCLUDE_HORIZONTAL)
    ts = sel['targets']
    state = core.load_state(layout.state)

    done_items = [t for t in ts if core.is_done(state.get(t['bvid']))]
    failed_items, pending_items = [], []
    for t in ts:
        rec = state.get(t['bvid'])
        st = core.status_of(rec)
        if st == 'uploaded':
            continue
        if st == 'failed':
            failed_items.append({'bvid': t['bvid'], 'error': (rec or {}).get('error')})
        else:
            pending_items.append({'bvid': t['bvid'], 'title': t.get('title'),
                                  'height': t.get('height'), 'width': t.get('width'),
                                  'duration': t.get('duration')})

    done_bytes = sum(int((state.get(t['bvid']) or {}).get('bytes') or 0)
                     for t in done_items)
    free_gb, total_gb, _, _ = core.disk_usage_gb(layout.workdir)

    out = {
        'mid': mid,
        'total': None if missing else len(ts),
        'vertical': None if missing else len(sel['vertical']),
        'horizontal': None if missing else len(sel['horizontal']),
        'unknown': None if missing else len(sel['unknown']),
        'done': None if missing else len(done_items),
        'failed': None if missing else len(failed_items),
        'pending': None if missing else len(pending_items),
        'done_bytes': None if missing else done_bytes,
        'done_gb': None if missing else round(done_bytes / 2**30, 2),
        'failed_items': failed_items,
        'pending_items': pending_items,
        'disk_free_gb': round(free_gb, 2),
        'disk_total_gb': round(total_gb, 2),
    }
    if missing:
        out['video_list_missing'] = True
        out['video_list_path'] = layout.video_list
    out_json(out, args.json_out)
    if missing:
        log('清单不存在：%s（先跑 fetch --mid <MID>）' % layout.video_list)
    return EXIT_OK


# ---------------------------------------------------------------- list

def cmd_list(args):
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    vl = core.load_video_list(layout.video_list)
    if vl is None:
        return die('清单不存在：%s（先跑 fetch --mid <MID>）' % layout.video_list, log=log)
    ex = resolve_exclude(args.exclude)
    sel = core.select_targets(core.list_items(vl),
                              vertical_only=args.vertical_only, exclude=ex,
                              include_horizontal=core.DEFAULT_INCLUDE_HORIZONTAL)
    ts = sel['targets']
    state = core.load_state(layout.state)

    rows = []
    for i, t in enumerate(ts, 1):
        rec = state.get(t['bvid']) or {}
        st = core.status_of(rec)
        rows.append({
            'idx': i, 'bvid': t['bvid'], 'title': t.get('title'),
            'date': t.get('date'), 'duration': t.get('duration'),
            'width': t.get('width'), 'height': t.get('height'),
            'vertical': t.get('vertical'), 'status': st,
            'error': rec.get('error'),
        })

    if args.json or args.json_out:
        out_json({'mid': (vl or {}).get('mid'), 'total': len(ts),
                  'done': len([r for r in rows if r['status'] == 'uploaded']),
                  'items': rows}, args.json_out)
        return EXIT_OK

    n_done = len([r for r in rows if r['status'] == 'uploaded'])
    print('目标 %d 条，已完成 %d 条（清单 %s）' % (len(rows), n_done, layout.video_list))
    for r in rows:
        mark = {'uploaded': '✓', 'failed': '✗'}.get(r['status'], ' ')
        print('%s %2d. %s %-7s %4.0fs %sx%s  %s' % (
            mark, r['idx'], r['bvid'], (r['date'] or '')[5:], r['duration'] or 0,
            r['width'], r['height'], (r['title'] or '')[:40]))
        if r['status'] == 'failed':
            print('      └ 失败：%s' % (r['error'] or '')[:100])
    return EXIT_OK


# ---------------------------------------------------------------- run

def _remove_local(path, log, tag):
    return core.remove_with_retry(path, attempts=6, log=lambda m: log('%s %s' % (tag, m)))


# --------------------------------------------------------- 可选：离线注入（测试用）
#
# 只在显式加 --fake-download 时才注入假的下载器/上传器，用来在**不产生大流量**的
# 前提下验证 run 的内部编排（progress.json 的 stage/percent、递进重试、校验、
# 删除重试）。生产路径（不带该参数）完全不受影响。
_FAKE = {}

FAKE_VERIFY_WAITS = (0.05, 0.05, 0.1, 0.1, 0.2, 0.2)


def _install_fakes(args, layout, log):
    fake_src = args.fake_src or os.path.join(layout.workdir, '_fake_src')
    if not os.path.isdir(fake_src):
        raise core.QuarkError('--fake-download 需要 --fake-src 目录（存放合成的小 mp4）：%s'
                              % fake_src)
    m4s = sorted(f for f in os.listdir(fake_src) if f.endswith('.mp4'))
    if not m4s:
        raise core.QuarkError('--fake-src 目录里没有 .mp4：%s' % fake_src)
    _FAKE.clear()
    _FAKE.update({
        'src': os.path.join(fake_src, m4s[0]),
        'calls': {}, 'remote': {}, 'uploaded': [], 'failed_once': set(),
        'transient_fail': bool(args.fake_transient_fail),
        'log': log,
    })
    log('已启用离线注入：源=%s（不会真的下载/上传）' % _FAKE['src'])


def _fake_download(bvid, cid, title, out_dir, prefer_avc=True, on_progress=None, page=None):
    rec = _FAKE['calls'].setdefault('%s#%s' % (bvid, page or 1), {'n': 0})
    rec['n'] += 1
    if 'dest' in rec:
        return rec['dest'], rec['info']
    name = safe_name(title, bvid, page=page)
    dest = os.path.join(out_dir, name)
    shutil.copy2(_FAKE['src'], dest)               # 先把「下载」做完
    size = os.path.getsize(dest)
    if on_progress:
        on_progress(0.0, '视频流', 0, size, 0.0)
        on_progress(50.0, '视频流', size // 2, size, 0.0)
        on_progress(100.0, '合并中', size, size, 0.0)
    _FAKE['remote'][name] = {'file_name': name, 'size': size,
                             'fid': 'fake-remote-' + bvid}
    rec['dest'] = dest
    rec['info'] = {'width': 2160, 'height': 3840, 'vcodec': 'h264', 'duration': 1.0}
    return dest, rec['info']


def _fake_upload(path, pdir_fid, name=None, concurrency=4, log=print, on_progress=None):
    name = name or os.path.basename(path)
    if _FAKE['transient_fail'] and name not in _FAKE['failed_once']:
        # 第一次上传失败（真实场景里的 Errno 10053 / 分片中断），让 _handle_one
        # 走进 failed 分支，验证 state 里仍保留 bytes/file/res + progress 记账
        _FAKE['failed_once'].add(name)
        raise QuarkError('合成：模拟首次上传失败')
    size = os.path.getsize(path)
    if on_progress:
        on_progress(0, size, 0.0)
        on_progress(size // 2, size, 0.0)
        on_progress(size, size, 0.0)
    _FAKE['remote'][name] = {'file_name': name, 'size': size,
                            'fid': 'fake-remote-1'}
    _FAKE['uploaded'].append(name)
    return {'task_id': 'fake-task-1'}


class _FakeQuark:
    def __init__(self):
        self._lock = threading.Lock()

    def find_recent(self, fid, name, pages=2, size=100):
        return _FAKE['remote'].get(name)

    def find_file(self, fid, name):
        return _FAKE['remote'].get(name)


def cmd_run(args):
    """下载 → 上传 → 校验 → 删本地，流式处理（下一条→传一条→删一条）。"""
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    if args.mid is None:
        return die('run 必须给 --mid <MID>（用于确认清单匹配）', log=log)
    mid = int(args.mid)

    vl = core.load_video_list(layout.video_list)
    if vl is None:
        return die('清单不存在：%s —— 先跑 `python -m bili_quark.cli fetch --mid %d`'
                   % (layout.video_list, mid), log=log)
    if vl.get('mid') is not None and int(vl['mid']) != mid:
        return die('清单 mid=%s 与 --mid %d 不一致（清单：%s）'
                   % (vl.get('mid'), mid, layout.video_list), log=log)

    keep = not args.delete_local
    ex = resolve_exclude(args.exclude)
    all_items = core.list_items(vl)
    sel = core.select_targets(all_items,
                              vertical_only=args.vertical_only, exclude=ex,
                              include_horizontal=core.DEFAULT_INCLUDE_HORIZONTAL)
    ts = sel['targets']
    state = core.load_state(layout.state)

    only = None
    page_pick = {}          # bvid -> 用户点名的分集（--only BV1xxx:2）
    if args.only:
        # 显式勾选就是用户的意思：横屏竖屏都按选择来，不再额外做方向过滤，
        # 否则勾了横屏会被静默丢掉。范围只由 --only 决定。
        # 每条还支持 `BV号:分集序号`（例如 BV1Tiec6TEMx:2），不写就是第 1 集。
        got = parse_bv_list(args.only)
        only = set()
        for raw in got or []:
            bv, _, pg = str(raw).partition(':')
            bv = bv.strip()
            if not bv:
                continue
            only.add(bv)
            if pg.strip().isdigit():
                page_pick[bv] = int(pg.strip())
        ts = [t for t in all_items if t['bvid'] in only]
        log('按 --only 处理 %d 条（不做方向过滤）' % len(ts))

    # --force：忽略 state 里的「已完成」判定，按本次选择强制重跑。
    # 用途：state 说上传过、但夸克目标目录里实际没有（被删/换目录/传丢了），
    # 面板把这类条目放回「待处理」并勾选后，必须能真的再传一次。
    force = bool(getattr(args, 'force', False))
    todo = []
    for t in ts:
        rec = state.get(t['bvid'])
        st = core.status_of(rec)
        if not force:
            if st == 'uploaded':
                continue
            if st == 'failed' and not args.retry_failed:
                continue
        todo.append(t)
    if force:
        log('--force：忽略 state 的已完成判定，按选择强制重跑（%d 条）' % len(todo))
    if args.limit is not None and args.limit >= 0:
        todo = todo[:args.limit]

    max_height = getattr(args, 'max_height', None) or None

    def pick_of(t):
        """这条实际下载哪一集：--only 里的 `:N` > 全局 --page > 第 1 集。"""
        want = page_pick.get(t['bvid']) or getattr(args, 'page', None) or 1
        return _pick_cid(t, want)

    pick_map = {t['bvid']: pick_of(t) for t in todo}
    if todo and any(pg for _cid, pg in pick_map.values()):
        log('分集选择：%s' % ', '.join(
            '%s→P%s' % (b, pg) for b, (_c, pg) in sorted(pick_map.items()) if pg))
    if max_height:
        log('分辨率上限：%dp' % int(max_height))

    free_gb, total_gb, _, _ = core.disk_usage_gb(layout.workdir)

    # --- dry-run：只筛选 + 磁盘检查 + 打印清单，不下载不上传
    if args.dry_run:
        need = sum(1 for t in todo)
        out = {
            'dry_run': True, 'mid': mid,
            'workdir': layout.workdir, 'video_list': layout.video_list,
            'state_file': layout.state,
            'vertical_only': args.vertical_only,
            'exclude': sorted(ex),
            'include_horizontal': sorted(core.DEFAULT_INCLUDE_HORIZONTAL),
            'keep_local': keep,
            'concurrency': args.concurrency,
            'part_concurrency': args.part_concurrency,
            'total': len(ts), 'todo': need,
            'force': force,
            'max_height': max_height,
            'disk_min_gb': args.disk_min_gb,
            'disk_free_gb': round(free_gb, 2), 'disk_total_gb': round(total_gb, 2),
            'disk_ok': free_gb >= args.disk_min_gb,
            'items': [{'bvid': t['bvid'], 'title': t.get('title'),
                       'width': t.get('width'), 'height': t.get('height'),
                       'duration': t.get('duration'), 'date': t.get('date'),
                       'page': pick_map.get(t['bvid'], (None, None))[1],
                       'cid': pick_map.get(t['bvid'], (None, None))[0]}
                      for t in todo],
        }
        # dry-run 的 stdout 必须是那一个 JSON（上层直接 JSON.parse），
        # --table 时才退回人类可读表格。--json-out 同时原子写文件。
        if args.table:
            print('DRY-RUN mid=%d 目标 %d 条，待处理 %d 条，并发 %d，磁盘剩余 %.2f GB '
                  '(阈值 %.1f GB, %s)' % (
                      mid, len(ts), need, args.concurrency, free_gb, args.disk_min_gb,
                      'OK' if out['disk_ok'] else '不足'))
            for t in todo:
                print('  %s %sx%s %s' % (t['bvid'], t.get('width'), t.get('height'),
                                         (t.get('title') or '')[:50]))
            if args.json_out:
                core.atomic_write_json(args.json_out, out)
        else:
            out_json(out, args.json_out)
        return EXIT_OK if out['disk_ok'] else EXIT_ARGS

    try:
        cookie = credentials.load_quark(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)

    fid = args.quark_dir
    if not fid:
        fid = (core.load_config(layout) or {}).get('quark_dir_fid')
    if not fid:
        return die('未指定目标目录：传 --quark-dir <FID>，或先跑 '
                   '`set-dir --fid <FID>` 写入 %s' % layout.config, log=log)

    layout.ensure_dirs()
    progress = Progress(layout.progress, mid, len(todo))
    progress.set_total(len(todo))

    # B 站凭据（环境变量 BILI_COOKIE 优先，文件回退）
    try:
        cookie_bili = credentials.load_bili(workdir)
    except credentials.CredentialError as e:
        progress.fail_start(str(e))
        return die(str(e), log=log)

    # 启动前磁盘检查（用 shutil.disk_usage 看 <workdir> 所在盘）
    free_gb, total_gb, free_b, _ = core.disk_usage_gb(layout.workdir)
    if free_gb < args.disk_min_gb:
        msg = ('磁盘空间不足：%s 剩余 %.2f GB < 阈值 %.1f GB'
               % (layout.workdir, free_gb, args.disk_min_gb))
        progress.fail_start(msg)
        return die(msg, log=log)

    log('待处理 %d 条（目标 %d 条），并发 %d，分片并发 %d，磁盘剩余 %.2f GB，fid=%s'
        % (len(todo), len(ts), args.concurrency, args.part_concurrency, free_gb, fid))
    if not todo:
        progress.finish('finished')
        log('没有待处理条目')
        return EXIT_OK

    q = Quark(cookie)
    state_writer = core.StateWriter(layout.state)
    stats = {'ok': 0, 'fail': 0}
    stat_lock = threading.Lock()

    # 离线注入（仅 --fake-download 时生效；生产路径不变）
    fake_dl = fake_up = None
    if getattr(args, 'fake_download', False):
        try:
            _install_fakes(args, layout, log)
        except Exception as e:
            progress.fail_start(str(e))
            return die('离线注入初始化失败：%s' % e, log=log)
        fake_dl, fake_up = _fake_download, _fake_upload
        q = _FakeQuark()

    # 每个 worker 独立的 Bili 会话，避免 wbi key 的竞态。
    # 凭据统一从 credentials 读（BILI_COOKIE 环境变量优先，文件回退），再注入会话。
    # 注意：cookie 文件可能根本不存在（纯环境变量模式，插件就是这么用的），
    # 所以这里直接把已读到的 cookie 字符串交给 BiliDownloader —— 它的构造会立刻
    # 解析这个参数，传 None 会让 open(None) 抛 TypeError 导致进程启动即退出。
    n_session = max(1, args.concurrency)
    cookie_arg = layout.bili_cookie if os.path.exists(layout.bili_cookie) else cookie_bili
    sessions = [BiliDownloader(cookie_arg, layout.tmp, log=log)
                for _ in range(n_session)]
    for s in sessions:
        s.b = Bili(cookie_bili)                    # 用统一读取到的 cookie
        _ = s.b.wbi                                # 预取 wbi key

    def handle(t, slot):
        bvid = t['bvid']
        tag = 'W%d' % (slot + 1)
        # 错开启动，避免并发同时首传触发服务端连接限流（实测 Errno 10053）
        time.sleep(1.5 + slot * 2.5 + random.random() * 2)
        progress.start_item(bvid, t.get('title'))
        try:
            _handle_one(t, tag, slot)
        except BaseException as e:
            # 兜底：任何意外都不能让整批挂掉
            tb = traceback.format_exc()
            log('%s 未捕获异常 %s：%s: %s' % (tag, bvid, type(e).__name__, e))
            log('%s 堆栈:\n%s' % (tag, tb))
            state_writer.append({'bvid': bvid, 'title': t.get('title'),
                                 'status': 'failed',
                                 'error': '%s: %s' % (type(e).__name__, e),
                                 'traceback': tb[-1500:],
                                 'finished': time.strftime('%F %T')})
            with stat_lock:
                stats['fail'] += 1
            progress.finish_item(bvid, False, '%s: %s' % (type(e).__name__, e))

    def _handle_one(t, tag, slot):
        bvid = t['bvid']
        dl = sessions[slot % len(sessions)]
        cid, page_tag = pick_map.get(bvid, (t.get('cid'), None))
        rec = {'bvid': bvid, 'title': t.get('title'),
               'cid': cid, 'page': page_tag, 'max_height': max_height,
               'started': time.strftime('%F %T')}
        path = None
        try:
            # --- 1) 下载（带 Referer + Range 断点续传；ffmpeg 合并）
            progress.stage_sync(bvid, 'download', 0.0)

            def dl_cb(percent, detail, done_b, total_b, speed):
                # 「合并中」单独算一段（merge），免得进度条在合并时空转
                progress.item(bvid, 'merge' if detail == '合并中' else 'download',
                              percent, detail=detail, done_bytes=done_b,
                              total_bytes=total_b, speed=speed)

            t0 = time.time()
            if fake_dl is not None:
                path, info = fake_dl(bvid, cid, t.get('title'),
                                     layout.downloads, prefer_avc=True,
                                     on_progress=dl_cb, page=page_tag)
            else:
                path, info = dl.download(bvid, cid, t.get('title'),
                                         layout.downloads, prefer_avc=True,
                                         on_progress=dl_cb,
                                         max_height=max_height, page=page_tag)
            rec['dl_seconds'] = round(time.time() - t0, 1)
            rec['file'] = os.path.basename(path)
            rec['bytes'] = os.path.getsize(path)
            if info:
                rec['res'] = '%sx%s' % (info.get('width'), info.get('height'))
                rec['vcodec'] = info.get('vcodec')
                rec['duration'] = info.get('duration')
            log('%s 下载完成 %.1f MB (%s) %s'
                % (tag, rec['bytes'] / 2**20, rec.get('res'), bvid))
            progress.stage_sync(bvid, 'download', 100.0)

            # --- 2) 上传（默认单分片 PUT 整文件；>900MB 自动回退多分片）
            progress.stage_sync(bvid, 'upload', 0.0)

            def up_cb(done_b, total_b, speed):
                pct = (done_b * 100.0 / total_b) if total_b else 0.0
                progress.item(bvid, 'upload', pct, detail='上传中',
                              done_bytes=done_b, total_bytes=total_b, speed=speed)

            t1 = time.time()
            if fake_up is not None:
                res = fake_up(path, fid, log=lambda m: log('%s %s' % (tag, m)),
                              concurrency=args.part_concurrency, on_progress=up_cb)
            else:
                res = q.upload(path, fid, log=lambda m: log('%s %s' % (tag, m)),
                               concurrency=args.part_concurrency, on_progress=up_cb)
            rec['upload_seconds'] = round(time.time() - t1, 1)
            rec['task_id'] = res['task_id']
            # 收尾任务的诊断信息：目录里找不到文件时，靠这几个字段定位是
            # 「提交失败」还是「索引延迟/被改名」。
            rec['obj_key'] = res.get('obj_key') or ''
            rec['finish_status'] = res.get('finish_status')
            progress.stage_sync(bvid, 'upload', 100.0)

            # --- 3) 校验：文件必须出现在目标目录且大小一致
            #     目录列表有索引延迟 → 递进重试（2/3/5/8/12/20 秒），
            #     先用 find_recent 按更新时间倒序查前几页，再回退全目录 find_file。
            progress.stage_sync(bvid, 'verify', 0.0)
            found = None
            local_name = os.path.basename(path)
            waits = FAKE_VERIFY_WAITS if fake_up else (2, 3, 5, 8, 12, 20)
            for i, wait in enumerate(waits):
                time.sleep(wait)
                found = q.find_recent(fid, local_name, pages=2)
                if found:
                    break
                # 等到第 3 轮还没出现：数据其实早传完了，问题多半出在收尾没落盘。
                # 重新提交一次收尾只要两个 API 调用，成功能省下重传几百 MB。
                if i == 2 and fake_up is None:
                    q.recommit(res.get('task_id'), res.get('obj_key'),
                               log=lambda m: log('%s %s' % (tag, m)))
                # 等待目录索引同步是有进度的（等第几轮），上报给面板免得看着像卡死
                progress.item(bvid, 'verify', (i + 1) * 100.0 / len(waits),
                              detail='等待目录同步 %d/%d' % (i + 1, len(waits)))
                log('%s 目录尚未同步，%ss 后重试…' % (tag, wait))
            if not found:
                # 兜底：全目录查找。顺带用 BV 号做子串匹配 —— 夸克遇到同名文件
                # 可能自动改名（存成 xxx(1).mp4），只认精确名会误判成「找不到」。
                found, loose = q.find_file_ex(fid, local_name, bvid=bvid)
                if not found and loose:
                    log('%s 精确名未命中，但目录里有同 BV 的 %d 个文件：%s'
                        % (tag, len(loose),
                           '、'.join((x.get('file_name') or '')[:60] for x in loose[:3])))
                    if len(loose) == 1:
                        found = loose[0]
            if not found:
                raise QuarkError(
                    '上传后目标目录找不到该文件（已重试 50s + 全目录查找）；'
                    'task_id=%s bucket=%s obj_key=%s 收尾任务=%s/status=%s'
                    % (str(res.get('task_id'))[:12], res.get('bucket'),
                       str(res.get('obj_key'))[:16],
                       str(res.get('finish_task'))[:12], res.get('finish_status')))
            if int(found.get('size') or 0) != rec['bytes']:
                raise QuarkError('大小不一致 远端=%s 本地=%s（远端文件名 %s）'
                                 % (found.get('size'), rec['bytes'],
                                    found.get('file_name') or ''))
            rec['remote_fid'] = found.get('fid')
            rec['status'] = 'uploaded'
            log('%s 已上传并校验通过 %s (远端 %d 字节)' % (tag, bvid, rec['bytes']))
            progress.stage_sync(bvid, 'verify', 100.0)

            # --- 4) 删本地（Windows 文件占用要重试 6 次）
            if not keep:
                progress.stage_sync(bvid, 'delete', 0.0)
                _remove_local(path, log, tag)
                rec['local_deleted'] = True
                progress.stage_sync(bvid, 'delete', 100.0)

            rec['finished'] = time.strftime('%F %T')
            state_writer.append(rec)
            with stat_lock:
                stats['ok'] += 1
            progress.finish_item(bvid, True)
        except Exception as e:
            rec['status'] = 'failed'
            rec['error'] = '%s: %s' % (type(e).__name__, e)
            rec['finished'] = time.strftime('%F %T')
            state_writer.append(rec)
            log('%s 失败 %s：%s' % (tag, bvid, rec['error']))
            with stat_lock:
                stats['fail'] += 1
            progress.finish_item(bvid, False, rec['error'])
        finally:
            with stat_lock:
                n = stats['ok'] + stats['fail']
            log('进度 %d/%d（成功 %d 失败 %d）' % (
                n, len(todo), stats['ok'], stats['fail']))

    if args.concurrency <= 1:
        for i, t in enumerate(todo):
            handle(t, i)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency,
                                thread_name_prefix='vid') as pool:
            list(pool.map(lambda pair: handle(pair[1], pair[0]),
                          list(enumerate(todo))))

    log('全部结束：成功 %d，失败 %d' % (stats['ok'], stats['fail']))
    progress.finish('finished')
    if args.json_out:
        # 真跑也支持 --json-out：只写文件，不往 stdout 打 JSON（保持原 CLI 行为）
        core.atomic_write_json(args.json_out, {
            'mid': mid, 'workdir': layout.workdir, 'quark_dir_fid': fid,
            'total': len(ts), 'processed': len(todo),
            'ok': stats['ok'], 'fail': stats['fail'],
            'finished': time.strftime('%F %T'),
            'state': 'finished',
            'progress_file': layout.progress,
        })
    return EXIT_OK if stats['fail'] == 0 else EXIT_FAILED


# ---------------------------------------------------------------- verify

def cmd_verify(args):
    """以夸克远端实际文件列表为准做最终核对（文件名 + 精确字节数）。"""
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    vl = core.load_video_list(layout.video_list)
    if vl is None:
        return die('清单不存在：%s（先跑 fetch --mid <MID>）' % layout.video_list, log=log)
    try:
        cookie = credentials.load_quark(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)

    fid = args.quark_dir or (core.load_config(layout) or {}).get('quark_dir_fid')
    if not fid:
        return die('未指定目标目录：传 --quark-dir <FID> 或先跑 `set-dir --fid <FID>`',
                   log=log)

    ex = resolve_exclude(args.exclude)
    sel = core.select_targets(core.list_items(vl),
                              vertical_only=args.vertical_only, exclude=ex,
                              include_horizontal=core.DEFAULT_INCLUDE_HORIZONTAL)
    ts = sel['targets']
    if args.mid is not None and vl.get('mid') is not None \
            and int(vl['mid']) != int(args.mid):
        return die('清单 mid=%s 与 --mid %s 不一致' % (vl.get('mid'), args.mid), log=log)
    state = core.load_state(layout.state)

    q = Quark(cookie)
    try:
        items = core.remote_files(q, fid)          # 完整分页，绝不依赖 _total
    except Exception as e:
        return die('远端列表获取失败：%s: %s' % (type(e).__name__, e), log=log)
    remote = {}
    for it in items:
        remote.setdefault(it.get('file_name'), it)
    log('远端目录实际文件数：%d' % len(remote))

    def hits_of(title, bvid):
        """远端里属于这条 BV 的文件名（前缀 = ``标题 [BV号]``）。

        同一 BV 的任意分集（[P2]）/任意分辨率（[1080p]）文件名都以它开头，
        所以「这条在不在网盘里」不会被后缀骗过去。但前缀后面必须紧跟 `.` 或 `[`，
        否则 BV1a 会把 BV1ab 也算进来。
        """
        prefix = name_prefix(title or '', bvid)
        return [n for n in remote
                if n.startswith(prefix) and n[len(prefix):][:1] in ('[', '.')]

    confirmed, missing_items, mismatch_items = 0, [], []
    remote_total = 0
    claimed = set()
    check_items = []
    for t in ts:
        bvid = t['bvid']
        hits = hits_of(t.get('title'), bvid)
        srec = state.get(bvid) or {}
        # 期望名按 state 里记的分集算（没记录就是第 1 集）
        expect_name = safe_name(t.get('title') or '', bvid, page=srec.get('page'))
        ssize = int(srec.get('bytes') or 0)
        if not hits:
            missing_items.append(bvid)
            check_items.append({'bvid': bvid, 'title': t.get('title'),
                                'status': 'missing', 'ok': False,
                                'expect_name': expect_name})
            continue
        claimed.update(hits)
        # 命中的多个文件里，优先拿「期望名」那个来比字节
        target_name = expect_name if expect_name in hits else sorted(hits)[0]
        rsize = int((remote.get(target_name) or {}).get('size') or 0)
        remote_total += rsize
        note = '' if target_name == expect_name else ('命中的是别的分集/分辨率：%s' % target_name)
        if ssize and ssize != rsize:
            mismatch_items.append({'bvid': bvid, 'expected': ssize, 'remote': rsize,
                                   'name': target_name})
            check_items.append({'bvid': bvid, 'title': t.get('title'),
                                'status': 'size', 'ok': False, 'name': target_name,
                                'bytes': rsize, 'expect_bytes': ssize,
                                'note': note, 'files': hits})
            continue
        confirmed += 1
        check_items.append({'bvid': bvid, 'title': t.get('title'),
                            'status': 'ok', 'ok': True, 'name': target_name,
                            'bytes': rsize, 'expect_bytes': ssize,
                            'note': note, 'files': hits})

    extra = sorted(set(remote) - claimed)
    res_dir = layout.downloads
    local_residue = []
    if os.path.isdir(res_dir):
        for fn in sorted(os.listdir(res_dir)):
            fp = os.path.join(res_dir, fn)
            if os.path.isfile(fp):
                try:
                    local_residue.append({'name': fn, 'size': os.path.getsize(fp)})
                except OSError:
                    local_residue.append({'name': fn, 'size': None})

    # 落盘「远端存在性快照」：面板靠它把夸克上还缺的条目放回「待处理」。
    # 这里对**全量清单**逐条判定（不受竖屏/排除名单影响），并且与上面的核对
    # 共用同一份 remote 映射和同一个 safe_name —— 不会出现两套口径。
    presence = []
    for it in core.list_items(vl):
        bvid = it.get('bvid')
        if not bvid:
            continue
        hits = hits_of(it.get('title'), bvid)
        presence.append({
            'bvid': bvid,
            'name': name_prefix(it.get('title') or '', bvid),
            'present': bool(hits),
            'size': max([int((remote.get(n) or {}).get('size') or 0) for n in hits] or [0]),
            'files': hits,
        })
    missing_all = [p['bvid'] for p in presence if not p['present']]
    core.atomic_write_json(layout.remote_index, {
        'mid': vl.get('mid'),
        'checked_at': core.now_str(),
        'quark_dir_fid': fid,
        'remote_files': len(remote),
        'list_total': len(presence),
        'missing': len(missing_all),
        'items': presence,
    })
    log('远端存在性快照已写入：%s（清单 %d 条，缺 %d 条）'
        % (layout.remote_index, len(presence), len(missing_all)))

    out_json({
        'mid': vl.get('mid'),
        'quark_dir_fid': fid,
        'target': len(ts),
        'confirmed': confirmed,
        'missing': len(missing_items),
        'size_mismatch': len(mismatch_items),
        'remote_total_bytes': remote_total,
        'remote_total_gb': round(remote_total / 2**30, 2),
        'extra_files': [{'name': n, 'size': int((remote[n] or {}).get('size') or 0)}
                        for n in extra],
        'local_residue': local_residue,
        'missing_items': missing_items,
        'mismatch_items': mismatch_items,
        'items': check_items,
    }, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- check-src

def cmd_check_src(args):
    """拿 B 站源流的**真实字节数**，跟夸克网盘里的文件逐个比对。

    和 verify 的区别：verify 只比「远端 vs 本机 state 记录」，本机没记录就无从判断；
    这里直接问 B 站 CDN（Range: bytes=0-0 拿 Content-Range），所以哪怕换了机器、
    state 丢了、文件是别人传的，也能判断网盘里那份是不是这个视频、这个分辨率。

    合并成 mp4 会比两路流之和多一点封装开销，因此默认给 1% 容差（--tolerance 可调）。
    """
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)
    vl = core.load_video_list(layout.video_list)
    if vl is None:
        return die('清单不存在：%s（先跑 fetch --mid <MID>）' % layout.video_list, log=log)
    try:
        quark_cookie = credentials.load_quark(workdir)
        bili_cookie = credentials.load_bili(workdir)
    except credentials.CredentialError as e:
        return die(str(e), log=log)

    fid = args.quark_dir or (core.load_config(layout) or {}).get('quark_dir_fid')
    if not fid:
        return die('未指定目标目录：传 --quark-dir <FID> 或先跑 `set-dir --fid <FID>`',
                   log=log)
    if args.mid is not None and vl.get('mid') is not None \
            and int(vl['mid']) != int(args.mid):
        return die('清单 mid=%s 与 --mid %s 不一致' % (vl.get('mid'), args.mid), log=log)

    ex = resolve_exclude(args.exclude)
    sel = core.select_targets(core.list_items(vl),
                              vertical_only=args.vertical_only, exclude=ex,
                              include_horizontal=core.DEFAULT_INCLUDE_HORIZONTAL)
    ts = sel['targets']

    page_pick = {}
    if args.only:
        got = parse_bv_list(args.only)
        only = set()
        for raw in got or []:
            bv, _, pg = str(raw).partition(':')
            bv = bv.strip()
            if not bv:
                continue
            only.add(bv)
            if pg.strip().isdigit():
                page_pick[bv] = int(pg.strip())
        # 点名了就只看这几条，且不做方向过滤
        ts = [t for t in core.list_items(vl) if t['bvid'] in only]
        log('按 --only 核对 %d 条' % len(ts))

    max_height = getattr(args, 'max_height', None) or None
    tol = abs(float(getattr(args, 'tolerance', 0.01) or 0.01))

    q = Quark(quark_cookie)
    try:
        items = core.remote_files(q, fid)          # 完整分页
    except Exception as e:
        return die('远端列表获取失败：%s: %s' % (type(e).__name__, e), log=log)
    remote = {}
    for it in items:
        remote.setdefault(it.get('file_name'), it)
    log('远端目录实际文件数：%d；容差 %.2f%%' % (len(remote), tol * 100))

    dl = BiliDownloader(layout.bili_cookie if os.path.exists(layout.bili_cookie)
                        else bili_cookie, layout.tmp, log=log)
    dl.b = Bili(bili_cookie)
    _ = dl.b.wbi

    results = []
    ok_n = bad_n = miss_n = err_n = 0
    for i, t in enumerate(ts, 1):
        bvid = t['bvid']
        cid, page_tag = _pick_cid(t, page_pick.get(bvid)
                                  or getattr(args, 'page', None) or 1)
        prefix = name_prefix(t.get('title') or '', bvid)
        hits = [n for n in remote
                if n.startswith(prefix) and n[len(prefix):][:1] in ('[', '.')]
        row = {'bvid': bvid, 'title': t.get('title'), 'page': page_tag}
        if not hits:
            row.update({'status': 'missing', 'ok': False, 'expect_name': prefix + '.mp4'})
            miss_n += 1
            results.append(row)
            log('[%2d/%d] %s 网盘里没有' % (i, len(ts), bvid))
            continue
        try:
            info = dl.stream_sizes(bvid, cid, max_height=max_height)
        except Exception as e:
            row.update({'status': 'error', 'ok': False,
                        'error': '%s: %s' % (type(e).__name__, e)})
            err_n += 1
            results.append(row)
            log('[%2d/%d] %s 取 B 站流大小失败：%s' % (i, len(ts), bvid, e))
            continue

        expect = int(info.get('total_bytes') or 0)
        best = None
        for n in hits:
            sz = int((remote.get(n) or {}).get('size') or 0)
            d = abs(sz - expect) if expect else 0
            if best is None or d < best[0]:
                best = (d, n, sz)
        _d, name, rsize = best
        diff = rsize - expect
        ratio = (abs(diff) / expect) if expect else 0.0
        good = bool(expect) and ratio <= tol
        row.update({
            'status': 'ok' if good else 'size', 'ok': good, 'name': name,
            'remote_bytes': rsize, 'expect_bytes': expect, 'diff_bytes': diff,
            'diff_percent': round(ratio * 100, 2),
            'width': info.get('width'), 'height': info.get('height'),
            'codecs': info.get('codecs'), 'files': hits,
        })
        if good:
            ok_n += 1
        else:
            bad_n += 1
        results.append(row)
        log('[%2d/%d] %s %s 网盘 %.1fMB / B站 %.1fMB（差 %+.2f%%）'
            % (i, len(ts), bvid, '一致' if good else '不一致',
               rsize / 2**20, expect / 2**20, (diff / expect * 100) if expect else 0))

    out_json({
        'mid': vl.get('mid'),
        'quark_dir_fid': fid,
        'checked': len(results),
        'ok': ok_n, 'size_mismatch': bad_n, 'missing': miss_n, 'error': err_n,
        'tolerance_percent': round(tol * 100, 2),
        'max_height': max_height,
        'items': results,
    }, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- check-cred

def cmd_check_cred(args):
    """验证 B 站 / 夸克凭据是否还有效，并给出能拿到的过期时间。

    cookie 是浏览器整行复制的，本身多半不带过期时间；能拿到的是：
      * B 站：nav 接口（是否登录、用户名）+ cookie 里的 bili_ticket_expires 时间戳
      * 夸克：member 接口（能不能调通、容量）；它不带过期时间，只能给「保存时间」
    """
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    out = {'workdir': workdir, 'checked_at': core.now_str()}

    try:
        cookie = credentials.load_bili(workdir)
        b = Bili(cookie)
        nav = b.get('https://api.bilibili.com/x/web-interface/nav')
        data = nav.get('data') or {}
        out['bili'] = {
            'ok': bool(data.get('isLogin')),
            'code': nav.get('code'),
            'uname': data.get('uname') or '',
            'mid': data.get('mid') or 0,
            'vip': bool(data.get('vipStatus')),
            'source': 'file' if os.path.exists(layout.bili_cookie) else 'env',
            'saved_at': _file_time(layout.bili_cookie),
            'expires_at': _cookie_expires(cookie, 'bili_ticket_expires'),
            'has_sessdata': 'SESSDATA' in (cookie or ''),
        }
        if not data.get('isLogin'):
            out['bili']['error'] = '未登录或 cookie 已失效（code=%s）' % nav.get('code')
    except Exception as e:
        out['bili'] = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}

    try:
        cookie = credentials.load_quark(workdir)
        q = Quark(cookie)
        m = q._ok(q.api('GET', '/1/clouddrive/member'))
        d = (m or {}).get('data') or {}
        out['quark'] = {
            'ok': True,
            'member_type': d.get('member_type') or '',
            'capacity_gb': round((d.get('total_capacity') or 0) / 2**30, 2),
            'used_gb': round((d.get('use_capacity') or 0) / 2**30, 2),
            'source': 'file' if os.path.exists(layout.quark_cookie) else 'env',
            'saved_at': _file_time(layout.quark_cookie),
            'expires_at': '',
            'has_puus': '__puus' in (cookie or ''),
        }
    except Exception as e:
        out['quark'] = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}

    out_json(out, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- repair

def cmd_repair(args):
    """修复大小不符的条目：删除夸克端文件 → 重置 state → 输出待重跑 BV 列表。

    前端拿到返回的 bvids 后，再调 run --only <bvids> --force 重新下载上传。
    """
    workdir = os.path.abspath(args.workdir or core.default_project_dir())
    layout = core.Layout(workdir, args.video_list, args.state_file)
    log = Logger(layout.runlog)

    # 1. 加载清单
    if not os.path.exists(layout.video_list):
        return die('清单不存在：%s（先跑 fetch --mid <MID>）' % layout.video_list, log=log)
    try:
        vl = json.loads(open(layout.video_list, encoding='utf-8').read())
    except Exception as e:
        return die(str(e), log=log)

    mid = args.mid or vl.get('mid')
    if not mid:
        return die('repair 必须给 --mid <MID>', log=log)

    # 2. 解析夸克目录
    quark_fid = args.quark_dir
    if not quark_fid:
        try:
            cfg = json.loads(open(layout.config, encoding='utf-8').read())
            quark_fid = cfg.get('quark_dir_fid')
        except Exception:
            pass
    if not quark_fid:
        return die('未设定夸克目标目录，先跑 set-dir --fid <FID>', log=log)

    # 3. 确定要修复的 BV 号
    only_raw = []
    for chunk in (args.only or []):
        for bv in chunk.split(','):
            bv = bv.strip()
            if bv:
                only_raw.append(bv)
    only_bvids = [x.split(':')[0] for x in only_raw]
    if not only_bvids:
        return die('repair 必须给 --only <BV号>（至少一个）', log=log)

    # 4. 获取远端文件列表，找到对应 fid
    cookie_q = credentials.load_quark(workdir)
    q = Quark(cookie_q)
    remote = {}
    try:
        for f in q.list_all(quark_fid):
            name = f.get('file_name') or ''
            if name:
                remote[name] = f
    except Exception as e:
        return die('获取远端文件列表失败：%s' % e, log=log)

    # 5. 匹配文件名 → 收集要删除的 fid
    targets = {t['bvid']: t for t in vl.get('videos', [])}
    to_delete_fids = []
    to_delete_names = []
    not_found = []
    for bvid in only_bvids:
        t = targets.get(bvid)
        if not t:
            not_found.append(bvid)
            continue
        page = int(t.get('page') or 1)
        base = core.safe_filename(t.get('title') or bvid)
        suffix = '' if page <= 1 else ' [P%d]' % page
        fname = '%s%s.mp4' % (base, suffix)
        hit = remote.get(fname)
        if hit:
            to_delete_fids.append(hit['fid'])
            to_delete_names.append(fname)
            log('找到远端文件 %s fid=%s' % (fname[:60], hit['fid'][:12]))
        else:
            not_found.append(bvid)
            log('远端未找到 %s（%s），跳过删除' % (bvid, fname[:60]))

    # 6. 批量删除
    deleted_count = 0
    delete_task_id = None
    if to_delete_fids:
        try:
            delete_task_id = q.delete(to_delete_fids, log=log)
            status = q.wait_task(delete_task_id, timeout=120, log=log)
            if status == 2:
                deleted_count = len(to_delete_fids)
                log('删除完成 %d 个文件' % deleted_count)
            else:
                log('删除任务异常 status=%s，继续重置 state' % status)
        except Exception as e:
            log('删除失败：%s，继续重置 state' % e)

    # 7. 重置 state：为每个 bvid 追加一条 pending 记录（last-write-wins）
    state_writer = core.StateWriter(layout.state)
    reset_bvids = []
    for bvid in only_bvids:
        t = targets.get(bvid)
        title = t.get('title') if t else ''
        state_writer.append({
            'bvid': bvid,
            'title': title,
            'status': 'pending',
            'repaired_at': core.now_str(),
        })
        reset_bvids.append(bvid)
        log('已重置 state %s → pending' % bvid)

    out_json({
        'ok': True,
        'deleted': deleted_count,
        'delete_task_id': delete_task_id,
        'reset_bvids': reset_bvids,
        'not_found_on_remote': not_found,
        'next_step': 'run --mid %d --only %s --force' % (
            mid, ','.join(reset_bvids)),
    }, args.json_out)
    return EXIT_OK


# ---------------------------------------------------------------- parser

class _Parser(argparse.ArgumentParser):
    """参数错误 → 退出码 1（argparse 默认是 2，与 run 的「有失败」冲突）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('%s: error: %s\n' % (self.prog, message))
        sys.exit(EXIT_ARGS)


def build_parser(with_top=True):
    ap = _Parser(prog='python -m bili_quark.cli',
                 description='B 站 UP 主竖屏视频 → 夸克网盘 流水线（可完整参数化 CLI）')
    if with_top:
        add_top_level(ap)
    sub = ap.add_subparsers(dest='command', metavar='<command>')

    p = add_common(sub.add_parser('fetch', help='抓取 UP 主全部投稿并逐条探测真实分辨率/时长'))
    p.add_argument('--mid', default=None,
                   help='UP 主 mid（也可以写在子命令之前）；必填')
    p.add_argument('--season-id', default=None,
                   help='只抓该合集内的视频（来自 seasons 命令的 season_id）；不给则抓全部投稿')
    p.add_argument('--limit', type=int, default=None, metavar='N',
                   help='本次最多探测 N 条（分批抓取用）：投稿多的 UP 一次抓完会超过宿主超时，'
                        '上层可反复调用直到结果里的 pending 变成 0')
    p.add_argument('--refresh', action='store_true',
                   help='重新拉取投稿列表（忽略 workdir 里的枚举缓存），并重新检查此前被剔除的条目')
    p.add_argument('--probe-concurrency', type=int, default=4, metavar='K',
                   help='探测并发数（默认 4；调太高容易被 B 站风控 412）')
    p.add_argument('--include-horizontal', action='store_true',
                   help='清单本身就包含全部条目（含横屏），此参数仅为显式说明')
    p.add_argument('--json', action='store_true', help='结果 JSON 打到 stdout')
    add_json_out(p)
    p.set_defaults(func=cmd_fetch)

    p = add_common(sub.add_parser('seasons', help='列出 UP 主的合集/系列（只读接口，不探测）'))
    p.add_argument('--mid', default=None, help='UP 主 mid；必填')
    p.add_argument('--json', action='store_true', help='结果 JSON 打到 stdout')
    add_json_out(p)
    p.set_defaults(func=cmd_seasons)

    p = add_common(sub.add_parser('set-dir', help='把夸克目标目录写入 <workdir>/config.json'))
    p.add_argument('--fid', required=True, help='夸克目录 fid')
    p.add_argument('--name', default='', help='目录名（备注用）')
    add_json_out(p)
    p.set_defaults(func=cmd_set_dir)

    p = add_common(sub.add_parser('resolve-dir', help='解析夸克目标目录，输出 JSON'))
    p.add_argument('--path', help='形如 /A/B 或 A\\B，逐级查找/创建')
    p.add_argument('--fid', help='已有目录 fid，反查并校验')
    p.add_argument('--no-create', action='store_true', help='不创建缺失目录，直接报错')
    add_json_out(p)
    p.set_defaults(func=cmd_resolve_dir)

    p = add_common(sub.add_parser('status', help='目标/进度/磁盘 JSON'))
    p.add_argument('--mid', type=int, default=None, help='UP 主 mid（仅用于回显/校验）')
    add_filter(p)
    add_json_out(p)
    p.set_defaults(func=cmd_status)

    p = add_common(sub.add_parser('list', help='人类可读清单'))
    p.add_argument('--mid', type=int, default=None)
    add_filter(p)
    p.add_argument('--json', action='store_true', help='改为输出 JSON')
    add_json_out(p)
    p.set_defaults(func=cmd_list)

    p = add_common(sub.add_parser('run', help='执行流水线：下载→上传→校验→删本地'))
    p.add_argument('--mid', default=None,
                   help='UP 主 mid（确认清单匹配；也可以写在子命令之前）；必填')
    p.add_argument('--quark-dir', metavar='FID',
                   help='目标目录 fid；不传则读 config.json 的 quark_dir_fid')
    p.add_argument('--concurrency', type=int, default=core.DEFAULT_CONCURRENCY,
                   help='同时处理几条视频（默认 %d）' % core.DEFAULT_CONCURRENCY)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--delete-local', dest='delete_local', action='store_true',
                   default=True, help='上传校验通过后删除本地副本（默认）')
    g.add_argument('--keep-local', dest='delete_local', action='store_false',
                   help='保留本地副本')
    add_filter(p)
    p.add_argument('--only', action='append', default=[], metavar='BV1,BV2',
                   help='只跑这些 BV 号（可重复/逗号分隔）')
    p.add_argument('--retry-failed', action='store_true',
                   help='把状态为 failed 的条目也当作待处理')
    p.add_argument('--force', action='store_true',
                   help='忽略 state 的已完成判定，强制重跑选中的条目'
                        '（补传夸克上缺失的文件时用）')
    p.add_argument('--part-concurrency', type=int,
                   default=core.DEFAULT_PART_CONCURRENCY,
                   help='单条上传时的分片并发数（默认 %d）' % core.DEFAULT_PART_CONCURRENCY)
    p.add_argument('--disk-min-gb', type=float, default=core.DEFAULT_DISK_MIN_GB,
                   help='启动前要求的最小剩余磁盘 GB（默认 %.0f）' % core.DEFAULT_DISK_MIN_GB)
    p.add_argument('--limit', type=int, default=None, help='只处理前 N 条（小批量试跑）')
    p.add_argument('--max-height', type=int, default=None, metavar='PX',
                   help='分辨率上限（如 1080 / 720）；不传就是最高画质（4K）')
    p.add_argument('--page', type=int, default=None, metavar='N',
                   help='默认下载第几集（多分集视频）；单条可用 --only BV号:N 覆盖')
    p.add_argument('--dry-run', action='store_true',
                   help='只筛选 + 磁盘检查 + 打印待处理清单（JSON 到 stdout），不下载不上传')
    p.add_argument('--table', action='store_true',
                   help='--dry-run 时改为人类可读表格（默认输出 JSON）')
    add_json_out(p)
    g2 = p.add_argument_group('离线注入（仅供测试，不下载不上传真实文件）')
    g2.add_argument('--fake-download', action='store_true',
                    help='用合成小 mp4 + 假上传器替代真实下载/上传（验证 run 编排）')
    g2.add_argument('--fake-src', metavar='DIR',
                    help='--fake-download 使用的合成 mp4 所在目录')
    g2.add_argument('--fake-transient-fail', action='store_true',
                    help='--fake-download 时模拟「首次上传失败 + 目录索引延迟」')
    p.set_defaults(func=cmd_run)

    p = add_common(sub.add_parser('verify', help='以夸克远端实际文件列表为准做最终核对'))
    p.add_argument('--mid', type=int, default=None)
    p.add_argument('--quark-dir', metavar='FID', help='目标目录 fid')
    add_filter(p)
    add_json_out(p)
    p.set_defaults(func=cmd_verify)

    p = add_common(sub.add_parser(
        'check-src', help='拿 B 站源流真实字节数，核对夸克网盘里的文件大小是否一致'))
    p.add_argument('--mid', type=int, default=None)
    p.add_argument('--quark-dir', metavar='FID', help='目标目录 fid（不传则读 config.json）')
    p.add_argument('--only', action='append', default=[], metavar='BV1,BV2',
                   help='只核对这些 BV 号（支持 BV号:分集序号）')
    p.add_argument('--page', type=int, default=None, metavar='N',
                   help='默认按第几集核对（多分集视频）')
    p.add_argument('--max-height', type=int, default=None, metavar='PX',
                   help='按这个分辨率上限算 B 站侧的期望大小（默认最高画质）')
    p.add_argument('--tolerance', type=float, default=0.01, metavar='RATIO',
                   help='允许的相对误差（默认 0.01 = 1%%，合并封装开销）')
    add_filter(p)
    add_json_out(p)
    p.set_defaults(func=cmd_check_src)

    p = add_common(sub.add_parser(
        'check-cred', help='验证 B 站 / 夸克凭据是否还有效，并给出过期时间'))
    add_json_out(p)
    p.set_defaults(func=cmd_check_cred)

    p = add_common(sub.add_parser(
        'repair', help='修复大小不符条目：删除夸克端文件并重置 state（配合 run --force 重传）'))
    p.add_argument('--mid', type=int, default=None)
    p.add_argument('--quark-dir', metavar='FID', help='目标目录 fid（不传则读 config.json）')
    p.add_argument('--only', action='append', default=[], metavar='BV1,BV2',
                   help='要修复的 BV 号（必填，支持逗号分隔多个）')
    add_json_out(p)
    p.set_defaults(func=cmd_repair)

    return ap


# 可以写在子命令之前的公共选项（argparse 的 subparsers 不会去前面找它们，
# 所以 main() 会先做一次重排，把「<command> 之前的这些选项」挪到 command 后面）。
GLOBAL_OPTS = ('--workdir', '--video-list', '--state-file', '--mid', '--quark-dir')


def reorder_global_opts(argv, commands):
    """把写在子命令之前的公共选项移到子命令之后。

    `--mid X status` / `--workdir W status` 这类调用形式在上层宿主里很自然，
    但 argparse 只会在子解析器里认这些选项；这里先做一次纯文本重排（语义不变）。
    重排只在「子命令前面的部分全部由选项及其值组成」时进行，避免误伤位置参数。
    """
    cmd_idx = None
    for i, tok in enumerate(argv):
        if tok in commands:
            cmd_idx = i
            break
    if cmd_idx is None:
        return argv

    def takes_value(tok):
        """这个选项后面是否还跟一个值（--json/--dry-run 等是布尔开关）。"""
        name = tok.partition('=')[0]
        if name in GLOBAL_OPTS or name in ('--json-out', '--only', '--exclude',
                                           '--fake-src'):
            return True
        return False

    pre, rest = argv[:cmd_idx], argv[cmd_idx:]
    if not pre:
        return argv                     # 子命令已经在最前面，无需重排
    moved, i = [], 0
    while i < len(pre):
        tok = pre[i]
        if tok.partition('=')[0] in GLOBAL_OPTS:
            if '=' in tok:
                moved.append(tok)
                i += 1
                continue
            if i + 1 < len(pre):
                moved += [tok, pre[i + 1]]
                i += 2
                continue
        moved.append(tok)
        if takes_value(tok) and i + 1 < len(pre) and not pre[i + 1].startswith('-'):
            moved.append(pre[i + 1])
            i += 2
            continue
        i += 1
    return moved + rest


def main(argv=None):
    setup_io()
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = reorder_global_opts(argv, COMMAND_NAMES)

    # 顶层也定义一份公共参数，这样 `... --mid <MID> status` 这种「选项写在子命令
    # 之前」的调用形式也能被 argparse 接受；下面再把值补进子命令的命名空间。
    top = _Parser(add_help=False, prog='python -m bili_quark.cli')
    top.add_argument('--workdir', default=None)
    top.add_argument('--video-list', default=None)
    top.add_argument('--state-file', default=None)
    top.add_argument('--mid', default=None)
    top.add_argument('--quark-dir', default=None)
    try:
        top_ns, _ = top.parse_known_args(argv)
    except SystemExit:
        top_ns = None

    ap = build_parser(with_top=True)
    # 子命令里显式给的值优先于「写在子命令之前」的值
    pre = {}
    if top_ns is not None:
        for name in ('workdir', 'video_list', 'state_file', 'quark_dir'):
            v = getattr(top_ns, name, None)
            if v is not None:
                pre[name] = v
        if getattr(top_ns, 'mid', None) is not None:
            pre['mid'] = top_ns.mid

    args = ap.parse_args(argv)
    for k, v in pre.items():
        if not hasattr(args, k) or getattr(args, k) is None:
            if k == 'mid':
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    pass
            setattr(args, k, v)

    if not getattr(args, 'command', None):
        ap.print_help()
        return EXIT_ARGS
    fn = getattr(args, 'func', None)
    if fn is None:
        ap.print_help()
        return EXIT_ARGS
    try:
        rc = fn(args)
    except KeyboardInterrupt:
        sys.stderr.write('中断\n')
        return EXIT_ARGS
    return EXIT_OK if rc is None else int(rc)


if __name__ == '__main__':
    sys.exit(main())
