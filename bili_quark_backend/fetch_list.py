"""拉取 UP 主全部投稿 + 逐条探测真实分辨率/时长，输出 UTF-8 JSON 供后续筛选。"""
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bili_api import Bili, load_cookie

HERE = os.path.dirname(os.path.abspath(__file__))
MID = 3546918307236545


def main():
    b = Bili(load_cookie(os.path.join(HERE, 'bili_cookie.txt')))

    # 1) 全量投稿列表
    all_items = []
    page = 1
    while True:
        v = b.videos(MID, page, 30)
        if v.get('code') != 0:
            print('list error page=%d code=%s msg=%s' % (page, v.get('code'), v.get('message')))
            break
        d = v['data']
        lst = d.get('list', {}).get('vlist', [])
        total = d.get('page', {}).get('count', 0)
        all_items.extend(lst)
        print('page %d: +%d (累计 %d / 总 %d)' % (page, len(lst), len(all_items), total))
        if len(all_items) >= total or not lst:
            break
        page += 1
        time.sleep(0.8)

    print('列表完成，共 %d 条' % len(all_items))

    # 2) 逐条探测尺寸
    out = []
    for i, it in enumerate(all_items, 1):
        bvid = it['bvid']
        try:
            pl = b.get('https://api.bilibili.com/x/player/pagelist?bvid=%s' % bvid)
            cid = pl['data'][0]['cid'] if pl.get('code') == 0 else None
        except Exception as e:
            cid = None
            print('  pagelist fail %s: %s' % (bvid, e))

        info = None
        if cid:
            for attempt in range(3):
                try:
                    info = b.playurl(bvid, cid)
                    break
                except Exception as e:
                    time.sleep(2 + attempt * 3)
        rec = {
            'bvid': bvid,
            'aid': it['aid'],
            'cid': cid,
            'title': it['title'],
            'created': it['created'],
            'date': time.strftime('%Y-%m-%d', time.localtime(it['created'])),
            'length': it.get('length'),
            'description': (it.get('description') or '')[:200],
            'pic': it.get('pic'),
            'width': info['width'] if info else None,
            'height': info['height'] if info else None,
            'duration': info['duration'] if info else None,
        }
        if rec['width'] and rec['height']:
            rec['vertical'] = rec['height'] > rec['width']
            rec['ratio'] = round(rec['width'] / rec['height'], 4)
        else:
            rec['vertical'] = None
            rec['ratio'] = None
        out.append(rec)
        print('[%2d/%d] %s %sx%s %s %s' % (
            i, len(all_items), bvid, rec['width'], rec['height'],
            '竖屏' if rec['vertical'] else ('横屏' if rec['vertical'] is False else '未知'),
            rec['title'][:28]))
        time.sleep(0.5)

    with open(os.path.join(HERE, 'video_list.json'), 'w', encoding='utf-8') as f:
        json.dump({'mid': MID, 'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'count': len(out), 'items': out}, f, ensure_ascii=False, indent=1)

    ver = [x for x in out if x['vertical']]
    hor = [x for x in out if x['vertical'] is False]
    unk = [x for x in out if x['vertical'] is None]
    print()
    print('竖屏 %d 条 / 横屏 %d 条 / 未知 %d 条' % (len(ver), len(hor), len(unk)))
    for x in ver:
        print('  竖屏 %s %sx%s %s' % (x['bvid'], x['width'], x['height'], x['title'][:34]))
    for x in hor:
        print('  横屏 %s %sx%s %s' % (x['bvid'], x['width'], x['height'], x['title'][:34]))
    for x in unk:
        print('  未知 %s %s' % (x['bvid'], x['title'][:34]))


if __name__ == '__main__':
    main()
