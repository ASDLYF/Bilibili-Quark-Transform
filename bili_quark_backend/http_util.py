"""极简 HTTP 客户端（标准库），供夸克/B 站接口使用。

本机沙箱下 commands 无法用 schannel 建 HTTPS，但 Python 的 ssl 正常，
所以全部网络请求都走这里，不依赖 requests。
"""
import json
import os
import ssl
import time
import urllib.request
import urllib.error

_CTX = ssl.create_default_context()

_OPENER = None


def opener():
    """全局复用的 urllib opener，**不走系统代理**。

    macOS 上 urllib 默认会读系统代理（``getproxies()``）。本机 Clash 的
    127.0.0.1:7897 是给 GitHub 用的，实测它让 B 站 API 慢 6~7 倍
    （TLS 握手 0.11s → 0.75s），下载视频流时还会间歇性抛
    ``_ssl.c:1112: The handshake operation timed out``。
    所以国内站点（B 站 / 夸克）一律直连；确实需要走代理时设
    环境变量 ``BILI_QUARK_PROXY_MODE=1``。

    注意变量名**不能以 _PROXY 结尾**：``getproxies_environment()`` 会把任何
    以 ``_proxy`` 结尾的环境变量当成代理配置（实测 ``BILI_QUARK_USE_PROXY=1``
    会让 ``getproxies()`` 变成 ``{'bili_quark_use': '1'}``，反而把真正的
    系统代理配置挤掉）。
    """
    global _OPENER
    if _OPENER is None:
        if os.environ.get('BILI_QUARK_PROXY_MODE') == '1':
            h = urllib.request.ProxyHandler()
        else:
            h = urllib.request.ProxyHandler({})
        _OPENER = urllib.request.build_opener(
            h, urllib.request.HTTPSHandler(context=_CTX))
    return _OPENER

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


class CaseInsensitiveDict(dict):
    """HTTP 响应头大小写不敏感（OSS 返回的是 'ETag'，取 'etag' 也要能拿到）。"""

    def __init__(self, data=None):
        super().__init__()
        if data:
            for k, v in dict(data).items():
                self[k] = v

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = CaseInsensitiveDict(headers)
        self.body = body

    @property
    def text(self):
        return self.body.decode('utf-8', 'replace')

    def json(self):
        return json.loads(self.body.decode('utf-8', 'replace'))


def request(method, url, headers=None, data=None, timeout=60,
            retries=3, backoff=2.0, expect_json=True):
    hdrs = {'User-Agent': DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/json')
    elif isinstance(body, str):
        body = body.encode('utf-8')

    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener().open(req, timeout=timeout) as r:
                return Resp(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            # 4xx 里 412/429 是限流，值得重试
            if e.code in (412, 429) and attempt < retries:
                last = 'HTTP %d' % e.code
                time.sleep(backoff * (attempt + 1))
                continue
            return Resp(e.code, dict(e.headers or {}), raw)
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, e)
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError('请求失败 %s %s (%s)' % (method, url, last))
    raise RuntimeError('请求失败 %s %s (%s)' % (method, url, last))
