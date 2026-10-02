"""主流水线：下载 → 上传夸克 → 校验 → 删本地，可断点续跑。

用法：
  python run_all.py --list                列出待处理清单
  python run_all.py --quark-info          查看夸克账号/目录（需 quark_cookie.txt）
  python run_all.py --quark-dir <fid>     设定夸克目标目录 fid
  python run_all.py --run [--only BVxxx]  执行（默认全部）
"""
import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bili_dl import BiliDownloader, safe_name, probe, FFMPEG  # noqa: E402
from quark_upload import Quark, QuarkError  # noqa: E402

VIDEO_JSON = os.path.join(HERE, 'video_list.json')
STATE = os.path.join(HERE, 'state.jsonl')
CONFIG = os.path.join(HERE, 'config.json')
DL_DIR = os.path.join(HERE, 'downloads')
TMP_DIR = os.path.join(HERE, '_tmp')
COOKIE_BILI = os.path.join(HERE, 'bili_cookie.txt')
COOKIE_QUARK = os.path.join(HERE, 'quark_cookie.txt')

SKIP_BVID = {'BV1nm3w6uEAJ'}          # 784x544「扒舞自用」
INCLUDE_HORIZONTAL = {'BV1g1he6bEAF'}  # 横屏正式投稿，用户确认要


_LOG_LOCK = threading.Lock()


def log(msg):
    """多线程写日志：加锁 + 容错。并发下 open 同一文件在 Windows 会抛 OSError。"""
    line = '[%s][%s] %s' % (time.strftime('%H:%M:%S'),
                            threading.current_thread().name, msg)
    with _LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(os.path.join(HERE, 'run.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def load_config():
    if os.path.exists(CONFIG):
        return json.load(open(CONFIG, encoding='utf-8'))
    return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def targets():
    items = json.load(open(VIDEO_JSON, encoding='utf-8'))['items']
    out = []
    for x in items:
        if x['bvid'] in SKIP_BVID:
            continue
        # 充电专属/大会员专享这类"接口回了但没有可用流"的条目绝不能当目标。
        # 正常路径下 cli.py 的 fetch 已经把它们剔出清单，这里是兜底。
        if x.get('lock'):
            continue
        if x['vertical'] or x['bvid'] in INCLUDE_HORIZONTAL:
            out.append(x)
    return out


def load_state():
    done = {}
    if os.path.exists(STATE):
        for line in open(STATE, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            done[r['bvid']] = r
    return done


def append_state(rec):
    with open(STATE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def read_cookie(path):
    with open(path, encoding='utf-8-sig') as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
    return ' '.join(lines)


def cmd_list():
    ts = targets()
    done = load_state()
    print('目标 %d 条，已完成 %d 条' % (len(ts), len([t for t in ts if t['bvid'] in done])))
    for i, t in enumerate(ts, 1):
        st = done.get(t['bvid'], {}).get('status', '-')
        mark = {'uploaded': '✓', 'failed': '✗'}.get(st, ' ')
        print('%s %2d. %s %-7s %4.0fs %sx%s  %s' % (
            mark, i, t['bvid'], t['date'][5:], t['duration'] or 0,
            t['width'], t['height'], t['title'][:40]))


def cmd_quark_info():
    cookie = read_cookie(COOKIE_QUARK)
    if '__puus' not in cookie:
        print('quark_cookie.txt 还没有有效 cookie（需含 __pus 和 __puus）')
        return 1
    q = Quark(cookie)
    r = q.api('GET', '/1/clouddrive/member')
    d = r.get('data', {})
    print('账号: %s   会员: %s   容量: %.1f GB / 已用 %.1f GB' % (
        d.get('nickname'), d.get('member_type'),
        (d.get('total_capacity') or 0) / 2**30, (d.get('use_capacity') or 0) / 2**30))
    print()
    print('根目录文件夹:')
    data = q.list_dir('0', size=200)
    for it in data.get('list', []):
        if it.get('dir'):
            print('  %-40s fid=%s' % (it.get('file_name'), it.get('fid')))
    cfg = load_config()
    if cfg.get('quark_dir_fid'):
        print()
        print('当前配置的目标目录: %s (%s)' % (
            cfg.get('quark_dir_name'), cfg.get('quark_dir_fid')))
        print('目录内文件数:', data.get('_total'))
    return 0


def cmd_set_dir(fid, name=''):
    cfg = load_config()
    cfg['quark_dir_fid'] = fid
    cfg['quark_dir_name'] = name
    save_config(cfg)
    print('已设定目标目录 fid=%s name=%s' % (fid, name))
    return 0


def cmd_run(only=None, workers=5, keep=False, part_concurrency=8):
    """workers = 同时处理的视频条数（各自独立走 下载→上传→校验→删本地）。
    part_concurrency = 单条视频上传时的分片并发数。"""
    cfg = load_config()
    fid = cfg.get('quark_dir_fid')
    if not fid:
        print('未设定夸克目标目录，先跑 --quark-info 再用 --quark-dir <fid> 设定')
        return 1
    cookie = read_cookie(COOKIE_QUARK)
    if '__puus' not in cookie:
        print('quark_cookie.txt 无效')
        return 1
    q = Quark(cookie)
    os.makedirs(DL_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    done = load_state()

    todo = [t for t in targets()
            if (only is None or t['bvid'] == only)
            and done.get(t['bvid'], {}).get('status') != 'uploaded']
    log('待处理 %d 条，并发 %d' % (len(todo), workers))

    # 每个 worker 独立的 Bili 会话，避免 wbi key 的竞态
    sessions = [BiliDownloader(COOKIE_BILI, TMP_DIR, log=log) for _ in range(max(1, workers))]
    for s in sessions:
        _ = s.b.wbi   # 预取 wbi key

    stats = {'ok': 0, 'fail': 0}
    stat_lock = threading.Lock()
    state_lock = threading.Lock()
    log_lock = threading.Lock()

    def handle(t, slot):
        bvid = t['bvid']
        tag = 'W%d' % (slot + 1)
        # 错开启动，避免 5 条同时首传触发服务端连接限流（实测会 Errno 10053）
        time.sleep(1.5 + slot * 2.5 + random.random() * 2)
        try:
            _handle_one(t, tag, slot)
        except BaseException as e:
            # 兜底：任何意外都不能让整批挂掉
            tb = traceback.format_exc()
            log('%s 未捕获异常 %s：%s: %s' % (tag, bvid, type(e).__name__, e))
            log('%s 堆栈:\n%s' % (tag, tb))
            with state_lock:
                append_state({'bvid': bvid, 'title': t['title'], 'status': 'failed',
                              'error': '%s: %s' % (type(e).__name__, e),
                              'traceback': tb[-1500:],
                              'finished': time.strftime('%F %T')})
            with stat_lock:
                stats['fail'] += 1

    def _handle_one(t, tag, slot):
        bvid = t['bvid']
        dl = sessions[slot % len(sessions)]
        rec = {'bvid': bvid, 'title': t['title'], 'started': time.strftime('%F %T')}
        try:
            t0 = time.time()
            path, info = dl.download(bvid, t['cid'], t['title'], DL_DIR, prefer_avc=True)
            rec['dl_seconds'] = round(time.time() - t0, 1)
            size = os.path.getsize(path)
            rec.update({'file': os.path.basename(path), 'bytes': size,
                        'res': '%sx%s' % (info.get('width'), info.get('height')),
                        'vcodec': info.get('vcodec'),
                        'duration': info.get('duration'),
                        'dl_seconds': round(time.time() - t0, 1)})
            log('%s 下载完成 %.1f MB (%s) %s' % (tag, size / 2**20, rec['res'], bvid))

            t1 = time.time()
            res = q.upload(path, fid, log=lambda m: log('%s %s' % (tag, m)),
                           concurrency=part_concurrency)
            rec['upload_seconds'] = round(time.time() - t1, 1)
            rec['task_id'] = res['task_id']

            # 上传后校验：文件必须出现在目标目录且大小一致。
            # 夸克目录列表有索引延迟，用「更新时间倒序的前几页」快速查找并重试。
            found = None
            local_name = os.path.basename(path)
            for i, wait in enumerate((2, 3, 5, 8, 12, 20)):
                time.sleep(wait)
                found = q.find_recent(fid, local_name, pages=2)
                if found:
                    break
                # 第 3 轮还没出现：数据早传完了，多半是收尾没落盘。重提一次收尾
                # 只要两个 API 调用，成功能省下重传整份文件。
                if i == 2:
                    q.recommit(res.get('task_id'), res.get('obj_key'),
                               log=lambda m: log('%s %s' % (tag, m)))
                log('%s 目录尚未同步，%ds 后重试…' % (tag, wait))
            if not found:
                # 兜底：全目录查找，并用 BV 号做子串匹配（夸克遇同名可能自动改名）
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
            if int(found.get('size') or 0) != size:
                raise QuarkError('大小不一致 远端=%s 本地=%s（远端文件名 %s）'
                                 % (found.get('size'), size,
                                    found.get('file_name') or ''))
            rec['remote_fid'] = found.get('fid')
            rec['status'] = 'uploaded'
            log('%s 已上传并校验通过 %s (远端 %d 字节)' % (tag, bvid, size))

            if not keep:
                # Windows 上刚读完的文件句柄可能还没释放，删除要重试
                for attempt in range(6):
                    try:
                        os.remove(path)
                        rec['local_deleted'] = True
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(1.5 * (attempt + 1))
            rec['finished'] = time.strftime('%F %T')
            with state_lock:
                append_state(rec)
            with stat_lock:
                stats['ok'] += 1
        except Exception as e:
            rec['status'] = 'failed'
            rec['error'] = '%s: %s' % (type(e).__name__, e)
            rec['finished'] = time.strftime('%F %T')
            with state_lock:
                append_state(rec)
            log('%s 失败 %s：%s' % (tag, bvid, rec['error']))
            with stat_lock:
                stats['fail'] += 1
        finally:
            with stat_lock:
                n = stats['ok'] + stats['fail']
            log('进度 %d/%d（成功 %d 失败 %d）' % (n, len(todo), stats['ok'], stats['fail']))

    if workers <= 1:
        for i, t in enumerate(todo):
            handle(t, i)
    else:
        # pool.map 只传一个参数，这里传 (slot, item) 元组，由 handle 自己解包
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='vid') as pool:
            list(pool.map(lambda pair: handle(pair[1], pair[0]), list(enumerate(todo))))

    log('全部结束：成功 %d，失败 %d' % (stats['ok'], stats['fail']))
    return 0 if stats['fail'] == 0 else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--quark-info', action='store_true')
    ap.add_argument('--quark-dir', metavar='FID')
    ap.add_argument('--quark-dir-name', default='')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--only')
    ap.add_argument('--keep', action='store_true', help='上传后保留本地副本')
    ap.add_argument('--workers', type=int, default=5,
                    help='同时处理几条视频（默认 5）')
    ap.add_argument('--concurrency', type=int, default=8,
                    help='单条视频上传时的分片并发数（默认 8）')
    args = ap.parse_args()
    if args.list:
        return cmd_list()
    if args.quark_info:
        return cmd_quark_info()
    if args.quark_dir:
        return cmd_set_dir(args.quark_dir, args.quark_dir_name)
    if args.run:
        return cmd_run(only=args.only, keep=args.keep, workers=args.workers,
                       part_concurrency=args.concurrency)
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
