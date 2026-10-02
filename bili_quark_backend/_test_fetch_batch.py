# -*- coding: utf-8 -*-
"""离线端到端测试：大 UP 分批抓取（--limit / 续抓 / 合并 / --refresh 捡回）。

用假 Bili 顶掉真实接口：
  * BV01..BV08  正常，playurl 回 3 档流
  * BVec09      带 is_charging_arc，playurl 回 0 档流 -> 剔除，原因标"充电专属"
  * BVlk10      无标记，playurl 回 0 档流          -> 剔除，原因标"无可用流"
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'bili_quark'))

import bili_quark.cli as cli  # noqa: E402

NORMAL = ['BV%02d' % i for i in range(1, 9)]
CHARGING = 'BVec09'
NOMARK = 'BVlk10'
ITEMS = []
for _bv in NORMAL:
    ITEMS.append({'bvid': _bv, 'aid': 1, 'title': '正常 ' + _bv, 'created': 1700000000,
                  'length': '01:00', 'description': '', 'pic': '', 'is_charging_arc': False})
ITEMS.append({'bvid': CHARGING, 'aid': 1, 'title': '充电专属', 'created': 1700000000,
              'length': '01:00', 'description': '', 'pic': '', 'is_charging_arc': True,
              'elec_arc_type': 1, 'elec_arc_badge': '充电专属'})
ITEMS.append({'bvid': NOMARK, 'aid': 1, 'title': '无流但没标记', 'created': 1700000000,
              'length': '01:00', 'description': '', 'pic': '', 'is_charging_arc': False})

STREAMLESS = {CHARGING, NOMARK}
CALLS = {'videos': 0, 'playurl': {}}


class FakeBili(object):
    def __init__(self, cookie=None, cache_dir='.'):
        self.cookie = cookie

    def videos(self, mid, page=1, size=30, order='pubdate'):
        CALLS['videos'] += 1
        if page > 1:
            return {'code': 0, 'data': {'list': {'vlist': []}, 'page': {'count': len(ITEMS)}}}
        return {'code': 0, 'data': {'list': {'vlist': list(ITEMS)},
                                    'page': {'count': len(ITEMS)}}}

    def season_archives(self, *a, **k):
        raise AssertionError('不该走合集分支')

    def get(self, url, raw=False, referer=None, retries=4):
        bvid = url.split('bvid=')[1].split('&')[0]
        return {'code': 0, 'data': [{'cid': 100, 'page': 1, 'part': 'P1', 'duration': 60}]}

    def playurl(self, bvid, cid):
        CALLS['playurl'][bvid] = CALLS['playurl'].get(bvid, 0) + 1
        if bvid in STREAMLESS:
            return {'width': None, 'height': None, 'duration': 60, 'accept': [], 'streams': 0}
        return {'width': 720, 'height': 1280, 'duration': 60, 'accept': [], 'streams': 3}


cli.Bili = FakeBili
cli.credentials.load_bili = lambda workdir: 'fake-cookie'

WORK = tempfile.mkdtemp(prefix='fetchbatch_')
LIST = os.path.join(WORK, 'video_list.json')
RAW = os.path.join(WORK, 'video_raw.json')

FAILS = []


def check(name, got, want):
    if got == want:
        print('  ok   %-42s %r' % (name, got))
    else:
        print('  FAIL %-42s got=%r want=%r' % (name, got, want))
        FAILS.append(name)


def run(limit=0, refresh=False, concurrency=4):
    # out_json 是 indent=1 的多行 JSON，直接从 json_out 文件读更稳
    jout = os.path.join(WORK, '_result.json')
    if os.path.exists(jout):
        os.remove(jout)
    args = argparse.Namespace(
        workdir=WORK, video_list=None, state_file=None, mid='123',
        season_id=None, json=True, json_out=jout,
        limit=limit, refresh=refresh, probe_concurrency=concurrency,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_fetch(args)
    with open(jout, 'r', encoding='utf-8') as fh:
        res = json.load(fh)
    return rc, res


def listing():
    with open(LIST, 'r', encoding='utf-8') as fh:
        return json.load(fh)


print('--- 第 1 批（--limit 4，只枚举一次）')
rc, res = run(limit=4)
check('exit code', rc, cli.EXIT_OK)
check('processed', res.get('processed'), 4)
check('pending', res.get('pending'), 6)
check('enum_total', res.get('enum_total'), 10)
check('has_more', res.get('has_more'), True)
check('videos() 调用次数', CALLS['videos'], 1)
check('video_raw.json 已落盘', os.path.exists(RAW), True)
check('磁盘上的 pending 也记了', listing().get('pending'), 6)

print('--- 第 2 批（--limit 4，不重新枚举）')
rc, res = run(limit=4)
check('processed', res.get('processed'), 4)
check('pending', res.get('pending'), 2)
check('videos() 仍只调用 1 次（走了缓存）', CALLS['videos'], 1)

print('--- 第 3 批（--limit 4，只剩 2 条）')
rc, res = run(limit=4)
check('processed', res.get('processed'), 2)
check('pending', res.get('pending'), 0)
check('has_more', res.get('has_more'), False)

lst = listing()
check('清单条数（10 条里剔掉 2 条）', len(lst['items']), 8)
check('清单顺序 = 枚举顺序', [x['bvid'] for x in lst['items']], NORMAL)
check('剔除条数', lst.get('locked'), 2)
check('剔除的都是那两条',
      sorted(x['bvid'] for x in lst['locked_items']), sorted([CHARGING, NOMARK]))
reasons = {x['bvid']: x.get('reason', '') for x in lst['locked_items']}
check('充电那条原因带"充电专属"', '充电专属' in reasons.get(CHARGING, ''), True)
check('无标记那条原因走通用文案', '无可用流' in reasons.get(NOMARK, ''), True)
check('竖屏数', sum(1 for x in lst['items'] if x['vertical'] is True), 8)
check('没有未知条目', sum(1 for x in lst['items'] if x['vertical'] is None), 0)

print('--- --refresh：模拟"给 UP 充完电"，被剔的 2 条重新探测')
before = dict(CALLS['playurl'])
rc, res = run(limit=0, refresh=True)
check('processed（只重探被剔的 2 条）', res.get('processed'), 2)
check('pending', res.get('pending'), 0)
check('充电条被重新探测', CALLS['playurl'].get(CHARGING, 0) > before.get(CHARGING, 0), True)
check('正常条没被重探', CALLS['playurl'].get('BV01', 0), before.get('BV01', 0))
check('刷新后清单仍 8 条', len(listing()['items']), 8)

print()
if FAILS:
    print('FAILED: ' + ', '.join(FAILS))
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1)
print('ALL ASSERTIONS PASSED — 分批抓取、续抓、合并、--refresh 均正确')
shutil.rmtree(WORK, ignore_errors=True)
