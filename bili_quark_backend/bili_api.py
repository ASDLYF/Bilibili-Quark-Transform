"""B 站接口小工具：WBI 签名 + 投稿列表 + 清晰度/尺寸探测。只用标准库。"""
import urllib.request
import urllib.parse
import json
import time
import hashlib
import os
import random
import re

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 412/429（风控）退避阶梯（秒）。B 站的封禁窗口实测可持续数分钟，
# 早先固定 5/10/15 秒重试三次必然穿不过去，所以拉长到最长约 1.5 分钟一次。
RETRY_BACKOFF = (5, 15, 30, 60, 90)

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

    def get(self, url, raw=False, referer=None, retries=4):
        headers = {
            'User-Agent': UA,
            'Referer': referer or 'https://www.bilibili.com/',
            'Origin': 'https://www.bilibili.com',
            'Accept': 'application/json, text/plain, */*',
        }
        if self.cookie:
            headers['Cookie'] = self.cookie
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, headers=headers)
            try:
                body = urllib.request.urlopen(req, timeout=30).read()
                return body if raw else json.loads(body)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (412, 429) and attempt < retries - 1:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    time.sleep(wait + random.uniform(0, 3))
                    continue
                raise
            except Exception:
                if attempt < retries - 1:
                    time.sleep(3 + random.uniform(0, 2))
                    continue
                raise
        if last:
            raise last

    def signed(self, api, params, referer=None, attempts=5):
        """WBI 签名请求；遇 412/429 时重新拉取 WBI key 并重新签名再试。

        单次 ``get()`` 重试复用同一个 ``w_rid``（``wts`` 被冻结在第一次签名里），
        在风控窗口内几乎必然继续 412。这里每一轮都重新取 key、重新签名，
        才有可能穿过封禁窗口。
        """
        last = None
        for i in range(attempts):
            img_key, sub_key = self.wbi
            q = enc_wbi(params, img_key, sub_key)
            try:
                return self.get('%s?%s' % (api, q), referer=referer, retries=1)
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (412, 429):
                    raise
                self._wbi = None            # 下一轮强制重新拉 nav 取新 key
                wait = RETRY_BACKOFF[min(i, len(RETRY_BACKOFF) - 1)]
                time.sleep(wait + random.uniform(0, 3))
        if last:
            raise last

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
        return self.signed('https://api.bilibili.com/x/space/wbi/acc/info',
                           {'mid': mid, 'platform': 'web', 'web_location': 1550101},
                           referer='https://space.bilibili.com/%s' % mid)

    def videos(self, mid, page=1, size=30, order='pubdate'):
        """UP 主投稿列表（含竖屏投稿，使用空间投稿接口）。"""
        params = {
            'mid': mid, 'ps': size, 'pn': page, 'tid': 0, 'keyword': '',
            'order': order, 'platform': 'web', 'web_location': 1550101,
            'index': 0, 'order_avoided': 'true',
        }
        return self.signed('https://api.bilibili.com/x/space/wbi/arc/search', params,
                           referer='https://space.bilibili.com/%s' % mid)

    def seasons(self, mid, page=1, size=20):
        """UP 主的合集/系列列表（只读，不探测）。

        返回 ``data.items_lists.seasons_list``（合集）与 ``series_list``（系列），
        每项形如 ``{'meta': {'season_id', 'name', 'total', 'cover', 'description'}}``。
        这个接口不带 WBI 也能通，用普通 get 即可（412 退避在 get 里）。
        """
        url = ('https://api.bilibili.com/x/polymer/web-space/seasons_series_list'
               '?mid=%s&page_num=%d&page_size=%d&web_location=333.1387'
               % (mid, page, size))
        return self.get(url, referer='https://space.bilibili.com/%s' % mid)

    def season_archives(self, mid, season_id, page=1, size=30, sort_reverse=False):
        """某个合集内的视频列表（只读，不探测）。

        ``data.archives`` 每项含 bvid/aid/title/pic/pubdate/duration/stat，
        注意**没有 cid**：分集信息仍要靠 x/player/pagelist 逐条取。
        """
        url = ('https://api.bilibili.com/x/polymer/web-space/seasons_archives_list'
               '?mid=%s&season_id=%s&page_num=%d&page_size=%d&sort_reverse=%s'
               '&web_location=333.1387'
               % (mid, season_id, page, size, 'true' if sort_reverse else 'false'))
        return self.get(url, referer='https://space.bilibili.com/%s' % mid)

    def playurl(self, bvid, cid):
        """取播放地址，返回 (width, height, duration, formats)。"""
        params = {
            'avid': 0, 'bvid': bvid, 'cid': cid, 'qn': 127, 'fnval': 4048,
            'fourk': 1, 'platform': 'pc', 'web_location': 1315873,
        }
        # 逐条探测时调用量最大，退避别拖太久：3 轮封顶
        d = self.signed('https://api.bilibili.com/x/player/wbi/playurl', params,
                        referer='https://www.bilibili.com/video/%s' % bvid,
                        attempts=3)
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
