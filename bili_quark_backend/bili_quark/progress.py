"""progress.json：给上层做实时进度用的原子写进度文件。

结构：
    {"state":"running"|"finished"|"failed_start", "mid":.., "total":49, "done":30,
     "ok":28, "fail":2,
     "current":[{"bvid":..,"title":..,"stage":"download","percent":42.5,
                 "overall":25.5,"detail":"视频流","done_bytes":123,"total_bytes":456,
                 "speed":1.5,"updated":"..."}],
     "started":"...", "updated":"...", "last_error":null, "pid":12345}

字段口径（上层面板直接按这些显示）：
  * percent  —— **当前阶段内部**的百分比（下载/上传是字节进度，校验是等待轮次，
                 删除是 0/100）
  * overall  —— 该条目在整个「下载→合并→上传→校验→删除」流程里的百分比，
                 由 STAGE_SPAN 把各阶段拼起来
  * detail   —— 阶段里的细分说明（视频流 / 音频流 / 合并中 / 等待目录同步 …）
  * done_bytes / total_bytes / speed —— 有字节数时给面板显示与测速用

多线程下更新必须加锁；每次写盘都走「先写 .tmp 再 os.replace」的原子替换。
写盘带节流（FLUSH_MIN_INTERVAL），避免高频进度回调把磁盘打满；
阶段切换、完成、结束这类关键点用 flush(force=True) 立刻落盘。
"""
import copy
import os
import threading
import time

from bili_quark.core import atomic_write_json, now_str

# 各阶段在整个条目流程里占的区间 (起点, 跨度)，加起来正好 100。
# 权重按实测耗时分配：下载最久，上传次之，校验是等目录索引，删本地很快。
STAGE_SPAN = {
    'download': (0.0, 60.0),
    'merge': (60.0, 5.0),
    'upload': (65.0, 32.0),
    'verify': (97.0, 2.0),
    'delete': (99.0, 1.0),
}

FLUSH_MIN_INTERVAL = 0.5


def overall_of(stage, percent):
    """把「阶段内百分比」折算成条目整体百分比。"""
    base, span = STAGE_SPAN.get(stage, (0.0, 100.0))
    p = max(0.0, min(100.0, float(percent or 0.0)))
    return round(base + span * p / 100.0, 1)


class Progress:
    def __init__(self, path, mid, total, state='running'):
        self.path = path
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._data = {
            'state': state,
            'mid': mid,
            'total': total,
            'done': 0,
            'ok': 0,
            'fail': 0,
            'current': [],
            'started': now_str(),
            'updated': now_str(),
            'last_error': None,
            'pid': os.getpid(),
        }
        self.flush(force=True)

    # --- 内部
    def _find(self, bvid):
        for c in self._data['current']:
            if c['bvid'] == bvid:
                return c
        return None

    def flush(self, force=False):
        """落盘。默认节流到 FLUSH_MIN_INTERVAL 一次；关键节点传 force=True。"""
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_flush < FLUSH_MIN_INTERVAL:
                return
            self._last_flush = now
            self._data['updated'] = now_str()
            atomic_write_json(self.path, self._data)

    # --- 对外
    def set_total(self, total):
        with self._lock:
            self._data['total'] = total
        self.flush(force=True)

    def start_item(self, bvid, title):
        with self._lock:
            if self._find(bvid) is None:
                self._data['current'].append({
                    'bvid': bvid, 'title': title, 'stage': 'download',
                    'percent': 0.0, 'overall': 0.0, 'detail': '',
                    'done_bytes': 0, 'total_bytes': 0, 'speed': 0.0,
                    'updated': now_str(),
                })
        self.flush(force=True)

    def item(self, bvid, stage, percent=0.0, detail=None, done_bytes=None,
             total_bytes=None, speed=None):
        """更新某条目的阶段进度。进度回调会高频调用它，所以默认走节流落盘。"""
        with self._lock:
            c = self._find(bvid)
            if c is None:
                return
            if c['stage'] != stage:
                # 换阶段了：上一阶段的细分信息（视频流/上传中、字节数、速度）不再适用
                c['detail'] = ''
                c['done_bytes'] = 0
                c['total_bytes'] = 0
                c['speed'] = 0.0
            c['stage'] = stage
            c['percent'] = round(float(percent or 0.0), 1)
            c['overall'] = overall_of(stage, percent)
            c['updated'] = now_str()
            if detail is not None:
                c['detail'] = str(detail)
            if done_bytes is not None:
                c['done_bytes'] = int(done_bytes)
            if total_bytes is not None:
                c['total_bytes'] = int(total_bytes)
            if speed is not None:
                c['speed'] = round(float(speed), 2)
        self.flush()

    def stage(self, bvid, stage, percent=0.0):
        self.item(bvid, stage, percent)

    def stage_sync(self, bvid, stage, percent=0.0):
        """阶段切换（或阶段内大跳变）：更新后立刻落盘。"""
        self.item(bvid, stage, percent)
        self.flush(force=True)

    def finish_item(self, bvid, ok, error=None):
        with self._lock:
            c = self._find(bvid)
            if c is not None:
                self._data['current'].remove(c)
            self._data['done'] += 1
            if ok:
                self._data['ok'] += 1
            else:
                self._data['fail'] += 1
                self._data['last_error'] = error
        self.flush(force=True)

    def finish(self, state='finished'):
        with self._lock:
            self._data['state'] = state
            self._data['current'] = []
        self.flush(force=True)

    def fail_start(self, error):
        with self._lock:
            self._data['state'] = 'failed_start'
            self._data['last_error'] = error
            self._data['current'] = []
        self.flush(force=True)

    def snapshot(self):
        with self._lock:
            d = copy.deepcopy(self._data)
        d['updated'] = now_str()
        return d
