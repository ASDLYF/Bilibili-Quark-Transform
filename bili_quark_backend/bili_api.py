"""B 站接口小工具：WBI 签名 + 投稿列表 + 清晰度/尺寸探测。只用标准库。"""
import urllib.request
import urllib.parse
import json
import time
import hashlib
import os
import re

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def load_cookie(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        txt = f.read()
    lines = [l.strip() for l in txt.splitlines()
             if l.strip() and not l.strip().startswith('#')]
    return ' '.join(lines)


def get_mixin_key(orig):
    return ''.join(orig[i] for i in MIXIN_TAB)[:32]


def enc_wbi(params, img_key, sub_key):
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params['wts'] = int(time.time())
    params = dict(sorted(params.items()))
    params = {k: ''.join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    params['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return urllib.parse.urlencode(params)


class Bili:
    def __init__(self, cookie=None, cache_dir='.'):
        self.cookie = cookie
        self.cache_dir = cache_dir
        self._wbi = None

    def get(self, url, raw=False):
        headers = {
            'User-Agent': UA,
            'Referer': 'https://www.bilibili.com/',
            'Origin': 'https://www.bilibili.com',
            'Accept': 'application/json, text/plain, */*',
        }
        if self.cookie:
            headers['Cookie'] = self.cookie
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(4):
            try:
                body = urllib.request.urlopen(req, timeout=30).read()
                return body if raw else json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code in (412, 429) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except Exception:
                if attempt < 3:
                    time.sleep(3)
                    continue
                raise

    @property
    def wbi(self):
        if self._wbi is None:
            nav = self.get('https://api.bilibili.com/x/web-interface/nav')
            wbi = nav['data']['wbi_img']
            img_key = wbi['img_url'].rsplit('/', 1)[1].split('.')[0]
            sub_key = wbi['sub_url'].rsplit('/', 1)[1].split('.')[0]
            self._wbi = (img_key, sub_key)
        return self._wbi

    def card(self, mid):
        return self.get('https://api.bilibili.com/x/web-interface/card?mid=%d' % mid)

    def space_info(self, mid):
        img_key, sub_key = self.wbi
        q = enc_wbi({'mid': mid, 'platform': 'web', 'web_location': 1550101},
                    img_key, sub_key)
        return self.get('https://api.bilibili.com/x/space/wbi/acc/info?%s' % q)

    def videos(self, mid, page=1, size=30, order='pubdate'):
        """UP 主投稿列表（含竖屏投稿，使用空间投稿接口）。"""
        img_key, sub_key = self.wbi
        params = {
            'mid': mid, 'ps': size, 'pn': page, 'tid': 0, 'keyword': '',
            'order': order, 'platform': 'web', 'web_location': 1550101,
            'index': 0, 'order_avoided': 'true',
        }
        q = enc_wbi(params, img_key, sub_key)
        return self.get('https://api.bilibili.com/x/space/wbi/arc/search?%s' % q)

    def playurl(self, bvid, cid):
        """取播放地址，返回 (width, height, duration, formats)。"""
        img_key, sub_key = self.wbi
        params = {
            'avid': 0, 'bvid': bvid, 'cid': cid, 'qn': 127, 'fnval': 4048,
            'fourk': 1, 'platform': 'pc', 'web_location': 1315873,
        }
        q = enc_wbi(params, img_key, sub_key)
        api = 'https://api.bilibili.com/x/player/wbi/playurl?%s' % q
        d = self.get(api)
        if d.get('code') != 0:
            return None
        data = d['data']
        best = None
        for v in data.get('dash', {}).get('video', []):
            key = (v.get('height', 0), v.get('width', 0), v.get('bandwidth', 0))
            if best is None or key > best[0]:
                best = (key, v)
        dim = data.get('dimension') or {}
        if best:
            v = best[1]
            w, h = v.get('width'), v.get('height')
        else:
            w, h = dim.get('width'), dim.get('height')
        return {
            'width': w, 'height': h,
            'duration': data.get('timelength', 0) // 1000 or dim.get('rotate'),
            'accept': data.get('accept_description'),
        }


if __name__ == '__main__':
    import sys
    mid = 3546918307236545
    b = Bili(load_cookie(r'D:\1\摄影\deepseek_bilibili\bili_cookie.txt'))
    print('=== 账号 nav ===')
    nav = b.get('https://api.bilibili.com/x/web-interface/nav')
    print('code', nav.get('code'), 'uname', nav['data'].get('uname'),
          'vip', nav['data'].get('vipStatus'))
    print()
    print('=== UP 主名片 mid=%d ===' % mid)
    c = b.card(mid)
    print('code', c.get('code'), c.get('message'))
    cd = c.get('data', {}).get('card', {})
    for k in ['mid', 'name', 'sign', 'fans', 'attention', 'archive_count', 'likes']:
        print('  %-14s %s' % (k, cd.get(k)))
    print()
    print('=== 投稿列表第 1 页 ===')
    v = b.videos(mid, 1, 30)
    print('code', v.get('code'), v.get('message'))
    if v.get('code') == 0:
        d = v['data']
        print('total 字段:', d.get('page'))
        lst = d.get('list', {}).get('vlist', [])
        print('本页条数:', len(lst))
        for it in lst[:8]:
            print('  %s | aid=%s bvid=%s | %s' % (
                time.strftime('%Y-%m-%d', time.localtime(it['created'])),
                it['aid'], it['bvid'], it['title'][:40]))
        if lst:
            print()
            print('=== 抽样探测尺寸（前 3 条）===')
            for it in lst[:3]:
                info = b.playurl(it['bvid'], it['cid'])
                print('  %s -> %s  %sx%s  %ss' % (
                    it['bvid'], it['title'][:20],
                    info['width'] if info else '?', info['height'] if info else '?',
                    info['duration'] if info else '?'))
