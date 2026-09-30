"""夸克网盘上传器（纯标准库实现）。

接口流程（网盘 PC 端 API，参照 quark-pan-uploader 的实现）：
  1. POST /1/clouddrive/file/upload/pre      建任务，拿 task_id / upload_id / bucket / auth_info / obj_key / callback
  2. POST /1/clouddrive/file/update/hash     回传 md5 + sha1（秒传判定）
  3. POST /1/clouddrive/file/upload/auth     取 OSS 分片签名（auth_meta 是按 OSS 规范拼的待签串）
  4. PUT  https://{bucket}.pds.quark.cn/{obj_key}?partNumber=N&uploadId=...  分片
  5. POST 同一 URL（uploadId 不带 partNumber）  合并
  6. POST /1/clouddrive/file/upload/finish   收尾
"""
import base64
import hashlib
import json
import mimetypes
import os
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import http_util

BASE_URL = 'https://drive-pc.quark.cn'
BASE_PARAMS = {'pr': 'ucpro', 'fr': 'pc', 'uc_param_str': ''}

OSS_UA = 'aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)'
COMPLETE_UA = 'aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit'

SINGLE_PART_MAX = 900 * 1024 * 1024   # 实测 501MB 单分片 PUT 正常；超过则走多分片
CHUNK = 4 * 1024 * 1024


class QuarkError(Exception):
    pass


class _ProgressReader:
    """把本地文件的某一段按块喂给 urllib，顺便回报已发送字节数。

    单分片 PUT 是「一个请求发整个文件」，不套这层就完全没有上传进度可看。
    read() 故意忽略传入的 n、每次都返回 BLOCK 大小：http.client 内部是按
    blocksize(8KB) 循环 read + sendall 的，返回大块能把 syscall 次数压下来。
    """

    BLOCK = 1024 * 1024

    def __init__(self, path, offset, size, on_progress=None):
        self._f = open(path, 'rb')
        self._f.seek(offset)
        self._left = int(size)
        self._total = int(size)
        self._done = 0
        self._cb = on_progress
        self._t0 = time.time()
        self._last_cb = 0.0

    def read(self, _n=-1):
        if self._left <= 0:
            return b''
        data = self._f.read(min(self.BLOCK, self._left))
        if not data:
            self._left = 0
            return b''
        self._left -= len(data)
        self._done += len(data)
        if self._cb:
            now = time.time()
            if self._left == 0 or now - self._last_cb > 0.8:
                self._last_cb = now
                try:
                    self._cb(self._done, self._total,
                             self._done / max(now - self._t0, 0.001))
                except Exception:
                    pass
        return data

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


def _utc_now():
    return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')


# ---------------------------------------------------------------- 哈希 / 分片上下文

_SHA1_INIT = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)


def _sha1_blocks(state, data):
    h0, h1, h2, h3, h4 = state
    for i in range(0, len(data) - len(data) % 64, 64):
        block = data[i:i + 64]
        w = list(struct.unpack('>16I', block))
        for t in range(16, 80):
            v = w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]
            w.append(((v << 1) | (v >> 31)) & 0xFFFFFFFF)
        a, b, c, d, e = h0, h1, h2, h3, h4
        for t in range(80):
            if t < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif t < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif t < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            temp = (((a << 5) | (a >> 27)) + f + e + k + w[t]) & 0xFFFFFFFF
            e = d
            d = c
            c = ((b << 30) | (b >> 2)) & 0xFFFFFFFF
            b = a
            a = temp
        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF
    return (h0, h1, h2, h3, h4)


def _encode_ctx(state, processed_bits):
    ctx = {
        'hash_type': 'sha1',
        'h0': str(state[0]), 'h1': str(state[1]), 'h2': str(state[2]),
        'h3': str(state[3]), 'h4': str(state[4]),
        'Nl': str(processed_bits), 'Nh': '0', 'data': '', 'num': '0',
    }
    return base64.b64encode(json.dumps(ctx, separators=(',', ':')).encode()).decode()


def hash_file(path, progress=None, need_contexts=False):
    """返回 (md5, sha1, [分片 hash_ctx...])。

    注意：hash_ctx 需要 OSS 的 SHA1 中间态，hashlib 不暴露，只能用纯 Python 状态机
    （实测约 0.9 MB/s，比上传还慢）。所以只有确实要走多分片时才计算它；单分片上传
    用不到 ctx，就用纯 C 的 hashlib 跑，速度快两个数量级。
    """
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    size = os.path.getsize(path)
    ctxs = []
    if not need_contexts:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                md5.update(chunk)
                sha1.update(chunk)
        return md5.hexdigest(), sha1.hexdigest(), []

    state = _SHA1_INIT
    done = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            full = len(chunk) - len(chunk) % 64
            if full:
                state = _sha1_blocks(state, chunk[:full])
            done += len(chunk)
            ctxs.append(_encode_ctx(state, done * 8))
            if progress:
                progress(done, size)
    if len(ctxs) != (size + CHUNK - 1) // CHUNK:
        raise QuarkError('hash context 数量与分片数不一致')
    if ctxs:
        ctxs.pop()
    return md5.hexdigest(), sha1.hexdigest(), ctxs


# ---------------------------------------------------------------- 会话

class Quark:
    def __init__(self, cookie, timeout=900):
        self.cookie = cookie
        self.timeout = timeout
        self._lock = threading.Lock()

    def api(self, method, path, params=None, payload=None, timeout=None):
        q = dict(BASE_PARAMS)
        if params:
            q.update(params)
        url = BASE_URL + path + '?' + '&'.join(
            '%s=%s' % (k, v) for k, v in q.items())
        r = http_util.request(
            method, url,
            headers={'Cookie': self.cookie,
                     'Accept': 'application/json, text/plain, */*',
                     'Referer': 'https://pan.quark.cn/'},
            data=payload, timeout=timeout or self.timeout)
        try:
            return r.json()
        except Exception:
            raise QuarkError('接口返回非 JSON（HTTP %s）：%s' % (r.status, r.text[:300]))

    @staticmethod
    def _ok(d):
        if d.get('status') not in (200, 0) or d.get('code') not in (0, None):
            raise QuarkError('接口错误 status=%s code=%s message=%s' % (
                d.get('status'), d.get('code'), d.get('message')))
        return d

    # --- 账号 / 目录
    def member(self):
        return self.api('GET', '/1/clouddrive/config/config', params={'pr': 'ucpro'})

    def info(self):
        return self.api('GET', '/1/clouddrive/member')

    def list_dir(self, pdir_fid='0', size=100, page=1, sub_dirs=1):
        d = self.api('GET', '/1/clouddrive/file/sort', params={
            'pdir_fid': pdir_fid, '_page': page, '_size': size,
            '_fetch_total': 1, '_fetch_sub_dirs': sub_dirs,
            '_sort': 'updated_at:desc'})
        return self._ok(d).get('data', {})

    def list_all(self, pdir_fid='0', size=100, sub_dirs=0):
        """列出目录全部条目。

        注意：夸克的 _total 字段不可靠（实测常返回 0/None），**不能**用它判断末页，
        否则会把「不是末页」误判成末页而漏掉后面的文件。这里以「本页返回条数 < 请求
        条数」作为终止条件。
        """
        out = []
        seen = set()
        page = 1
        while True:
            d = self.list_dir(pdir_fid, size=size, page=page, sub_dirs=sub_dirs)
            lst = d.get('list') or []
            new = 0
            for it in lst:
                key = it.get('fid') or it.get('file_name')
                if key not in seen:
                    seen.add(key)
                    out.append(it)
                    new += 1
            if len(lst) < size or new == 0:
                break
            page += 1
            if page > 100:
                break
            time.sleep(0.3)
        return out

    def mkdir(self, pdir_fid, name):
        d = self.api('POST', '/1/clouddrive/file', payload={
            'pdir_fid': pdir_fid, 'file_name': name,
            'dir_init_lock': False, 'dir_path': ''})
        return self._ok(d)

    def ensure_dir(self, pdir_fid, name):
        for it in self.list_all(pdir_fid, size=100, sub_dirs=1):
            if it.get('file_name') == name and it.get('dir'):
                return it['fid']
        self.mkdir(pdir_fid, name)
        # 建完再查一次拿 fid
        for _ in range(5):
            time.sleep(1.0)
            for it in self.list_all(pdir_fid, size=100, sub_dirs=1):
                if it.get('file_name') == name and it.get('dir'):
                    return it['fid']
        raise QuarkError('目录已创建但找不到 fid: %s' % name)

    def find_file(self, pdir_fid, name):
        """在目录内查找文件。分页遍历全部条目，避免大目录漏查。"""
        for it in self.list_all(pdir_fid, size=100, sub_dirs=0):
            if it.get('file_name') == name and not it.get('dir'):
                return it
        return None

    def find_recent(self, pdir_fid, name, pages=1, size=100):
        """只需确认「刚上传的文件在不在」，按更新时间倒序查前几页即可，
        不必翻遍整个目录（目录很大时 find_file 会明显变慢）。"""
        for page in range(1, pages + 1):
            d = self.list_dir(pdir_fid, size=size, page=page, sub_dirs=0)
            for it in (d.get('list') or []):
                if it.get('file_name') == name and not it.get('dir'):
                    return it
        return None

    # --- 删除
    def delete(self, fids, log=None):
        """删除一个或多个文件/文件夹（移入回收站）。

        fids: str 或 list[str]，要删除的 fid。
        返回 task_id；删除是异步操作，可用 wait_task() 等待完成。
        """
        if isinstance(fids, str):
            fids = [fids]
        d = self.api('POST', '/1/clouddrive/file/delete', payload={
            'action_type': 2, 'filelist': fids, 'exclude_fids': []})
        r = self._ok(d)
        task_id = (r.get('data') or {}).get('task_id')
        if log:
            log('已提交删除 %d 个文件 task_id=%s' % (len(fids), task_id))
        return task_id

    def wait_task(self, task_id, timeout=60, interval=0.5, log=None):
        """轮询异步任务直到完成或超时。返回最终 status（2=完成, 3=失败）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = self.api('GET', '/1/clouddrive/task', params={'task_id': task_id})
            status = (d.get('data') or {}).get('status')
            if status in (2, 3):
                if log:
                    log('任务 %s 结束 status=%s' % (task_id[:12], status))
                return status
            time.sleep(interval)
        if log:
            log('任务 %s 超时 %.0fs' % (task_id[:12], timeout))
        return None

    # --- 上传
    def upload(self, path, pdir_fid, name=None, concurrency=4, log=print, on_progress=None):
        """上传一个文件到指定目录。

        :param on_progress: 可选回调 ``on_progress(done_bytes, total_bytes, speed)``，
            单分片（流式 PUT）与多分片都会用它回报真实字节进度。
        """
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        now_ms = int(time.time() * 1000)
        # 单分片 PUT 整文件：省掉昂贵的 hash 状态机。超过阈值才走多分片。
        one_shot = size <= SINGLE_PART_MAX

        t0 = time.time()
        md5, sha1, ctxs = hash_file(path, need_contexts=not one_shot)
        log('    hash 完成 %.1fs md5=%s… size=%.1fMB%s' % (
            time.time() - t0, md5[:10], size / 2**20,
            '（单分片）' if one_shot else '（多分片，已算 %d 个上下文）' % len(ctxs)))

        pre = self._ok(self.api('POST', '/1/clouddrive/file/upload/pre', payload={
            'ccp_hash_update': True, 'parallel_upload': True,
            'pdir_fid': pdir_fid, 'dir_name': '', 'size': size,
            'file_name': name, 'format_type': mime,
            'l_updated_at': now_ms, 'l_created_at': now_ms}))
        d = pre.get('data', {})
        task_id = d.get('task_id', '')
        upload_id = d.get('upload_id', '')
        bucket = d.get('bucket', 'ul-zb')
        auth_info = d.get('auth_info', '')
        obj_key = d.get('obj_key', '')
        callback = d.get('callback') or {}
        if not task_id:
            raise QuarkError('pre 未返回 task_id: %s' % json.dumps(pre, ensure_ascii=False)[:300])
        log('    任务已建 task_id=%s… bucket=%s' % (task_id[:12], bucket))

        self._ok(self.api('POST', '/1/clouddrive/file/update/hash',
                          payload={'task_id': task_id, 'md5': md5, 'sha1': sha1}))

        # 服务端秒传命中：auth_info 为空即无需真正上传
        if not auth_info or not obj_key or not upload_id:
            log('    服务端已有该文件（秒传命中），跳过 OSS 上传')
            if on_progress:
                try:
                    on_progress(size, size, 0.0)
                except Exception:
                    pass
        else:
            done_ok = False
            if one_shot:
                try:
                    self._upload_one_part(path, task_id, auth_info, obj_key, upload_id,
                                          bucket, mime, callback, log, on_progress=on_progress)
                    done_ok = True
                except QuarkError as e:
                    log('    单分片失败，回退多分片：%s' % e)
                    ctxs = hash_file(path, need_contexts=True)[2]
            if not done_ok:
                self._upload_parts(path, task_id, auth_info, obj_key, upload_id,
                                   bucket, mime, callback, ctxs, concurrency, log,
                                   on_progress=on_progress)

        fin = self._ok(self.api('POST', '/1/clouddrive/file/upload/finish',
                                payload={'task_id': task_id, 'obj_key': obj_key}))
        return {'task_id': task_id, 'finish': fin, 'name': name, 'size': size}

    # --- OSS 交互
    def _auth_meta_put(self, mime, oss_date, bucket, obj_key, upload_id, part, hash_ctx=''):
        lines = ['PUT', '', mime, oss_date, 'x-oss-date:%s' % oss_date]
        if hash_ctx:
            lines.append('x-oss-hash-ctx:%s' % hash_ctx)
        lines.append('x-oss-user-agent:%s' % OSS_UA)
        lines.append('/%s/%s?partNumber=%d&uploadId=%s' % (bucket, obj_key, part, upload_id))
        return '\n'.join(lines)

    def _auth_meta_complete(self, oss_date, bucket, obj_key, upload_id, xml_data, callback):
        xml_md5 = base64.b64encode(hashlib.md5(xml_data.encode()).digest()).decode()
        cb = base64.b64encode(json.dumps(callback, separators=(',', ':')).encode()).decode()
        return '\n'.join([
            'POST', xml_md5, 'application/xml', oss_date,
            'x-oss-callback:%s' % cb, 'x-oss-date:%s' % oss_date,
            'x-oss-user-agent:%s' % COMPLETE_UA,
            '/%s/%s?uploadId=%s' % (bucket, obj_key, upload_id)])

    def _get_auth(self, task_id, auth_info, auth_meta):
        d = self._ok(self.api('POST', '/1/clouddrive/file/upload/auth', payload={
            'task_id': task_id, 'auth_info': auth_info, 'auth_meta': auth_meta}))
        return d.get('data', {}).get('auth_key', '')

    def _oss_put(self, path, url, headers, offset, size, on_progress=None):
        """PUT 一段数据到 OSS。默认走**流式** body 以便回报上传进度；
        流式这条路万一不被接受（签名/Content-Length 之类），退回一次性整块 PUT，
        保证「能看到进度」这件事不会反而把原本能成的上传搞挂。

        :param on_progress: ``on_progress(done_bytes, total_bytes, speed)``
        """
        if on_progress is None:
            return self._oss_put_block(path, url, headers, offset, size)

        # 显式给 Content-Length：urllib 对 file-like body 不会自己算长度，
        # 缺了会退化成 chunked，OSS 不接受。
        hdrs = dict(headers)
        hdrs['Content-Length'] = str(size)
        last = None
        # 重试必须重建 reader —— body 已经被读空，复用同一个对象会发出空 body
        for attempt in range(2):
            reader = _ProgressReader(path, offset, size, on_progress)
            try:
                r = http_util.request('PUT', url, headers=hdrs, data=reader,
                                      timeout=self.timeout, retries=0)
            except Exception as e:
                last = '%s: %s' % (type(e).__name__, e)
                reader.close()
                time.sleep(2)
                continue
            reader.close()
            if r.status == 200:
                return r.headers.get('etag', '').strip('"')
            last = 'HTTP %s: %s' % (r.status, r.text[:300])
            if r.status not in (429, 500, 502, 503, 504):
                break
            time.sleep(2)
        # 流式失败 → 老办法（整块 bytes）再来一次
        return self._oss_put_block(path, url, headers, offset, size)

    def _oss_put_block(self, path, url, headers, offset, size):
        """一次性把整段读进内存再 PUT（原实现，作为流式失败时的兜底）。"""
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read(size)
        r = http_util.request('PUT', url, headers=headers, data=data,
                              timeout=self.timeout, retries=3)
        if r.status != 200:
            raise QuarkError('OSS 分片失败 HTTP %s: %s' % (r.status, r.text[:300]))
        return r.headers.get('etag', '').strip('"')

    def _complete(self, url, headers, xml_data, attempts=3):
        last = None
        for i in range(attempts):
            try:
                r = http_util.request('POST', url, headers=headers, data=xml_data,
                                      timeout=self.timeout, retries=2)
                if r.status in (200, 204):
                    return
                last = 'HTTP %s: %s' % (r.status, r.text[:200])
            except Exception as e:
                last = str(e)
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
        raise QuarkError('OSS 合并失败（重试 %d 次）：%s' % (attempts, last))

    def _upload_one_part(self, path, task_id, auth_info, obj_key, upload_id,
                         bucket, mime, callback, log, attempts=3, on_progress=None):
        """单分片 PUT 整个文件。大文件长连接容易被中断（Errno 10053），
        所以每次重试都重新取签名，避免 auth_key 过期或被服务端丢弃。"""
        size = os.path.getsize(path)
        url = 'https://%s.pds.quark.cn/%s?partNumber=1&uploadId=%s' % (bucket, obj_key, upload_id)
        etag = ''
        last = None
        for i in range(attempts):
            oss_date = _utc_now()
            meta = self._auth_meta_put(mime, oss_date, bucket, obj_key, upload_id, 1)
            auth_key = self._get_auth(task_id, auth_info, meta)
            headers = {'Content-Type': mime, 'x-oss-date': oss_date,
                       'x-oss-user-agent': OSS_UA, 'authorization': auth_key}
            try:
                etag = self._oss_put(path, url, headers, 0, size, on_progress=on_progress)
                break
            except Exception as e:
                last = '%s: %s' % (type(e).__name__, e)
                if i < attempts - 1:
                    wait = 3 * (i + 1)
                    log('    单分片第 %d 次失败（%s），%ds 后重试…' % (i + 1, last[:90], wait))
                    time.sleep(wait)
        if not etag:
            raise QuarkError('单分片上传失败（重试 %d 次）：%s' % (attempts, last))
        log('    单分片上传完成 etag=%s' % etag[:16])

        xml_data = self._build_complete_xml([etag])
        c_date = _utc_now()
        c_meta = self._auth_meta_complete(c_date, bucket, obj_key, upload_id, xml_data, callback)
        c_key = self._get_auth(task_id, auth_info, c_meta)
        xml_md5 = base64.b64encode(hashlib.md5(xml_data.encode()).digest()).decode()
        cb = base64.b64encode(json.dumps(callback, separators=(',', ':')).encode()).decode()
        c_headers = {'Content-Type': 'application/xml', 'x-oss-date': c_date,
                     'x-oss-user-agent': COMPLETE_UA, 'x-oss-callback': cb,
                     'Content-MD5': xml_md5, 'authorization': c_key}
        self._complete('https://%s.pds.quark.cn/%s?uploadId=%s' % (bucket, obj_key, upload_id),
                       c_headers, xml_data)

    @staticmethod
    def _build_complete_xml(etags):
        parts = '\n'.join('<Part><PartNumber>%d</PartNumber><ETag>"%s"</ETag></Part>'
                          % (i, e) for i, e in enumerate(etags, 1))
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n'
                + parts + '\n</CompleteMultipartUpload>')

    def _upload_parts(self, path, task_id, auth_info, obj_key, upload_id,
                     bucket, mime, callback, ctxs, concurrency, log, on_progress=None):
        size = os.path.getsize(path)
        part_total = (size + CHUNK - 1) // CHUNK
        log('    分片上传 %d 片 × 4MB，并发 %d' % (part_total, concurrency))
        etags = {}
        done = [0]
        sent = [0]          # 所有分片已发出的字节（含正在传的片内进度）

        def part_progress(pn):
            """把某片的片内字节进度折算进整文件进度（并发下靠 sent 累加，单调不回退）。"""
            state = {'last': 0}

            def cb(done_in_part, _total_part, speed):
                if done_in_part < state['last']:
                    state['last'] = 0            # 这片重试了，重新计数
                with self._lock:
                    sent[0] += done_in_part - state['last']
                    state['last'] = done_in_part
                    n = sent[0]
                if on_progress:
                    try:
                        on_progress(min(n, size), size, speed)
                    except Exception:
                        pass
            return cb

        def do_part(pn):
            offset = (pn - 1) * CHUNK
            psize = min(CHUNK, size - offset)
            hash_ctx = ctxs[pn - 2] if pn > 1 else ''
            url = ('https://%s.pds.quark.cn/%s?partNumber=%d&uploadId=%s'
                   % (bucket, obj_key, pn, upload_id))
            etag = ''
            last = None
            for attempt in range(3):
                # 每次重试都重新取签名（连接中断后旧签名可能已失效）
                oss_date = _utc_now()
                meta = self._auth_meta_put(mime, oss_date, bucket, obj_key, upload_id, pn, hash_ctx)
                auth_key = self._get_auth(task_id, auth_info, meta)
                headers = {'Content-Type': mime, 'x-oss-date': oss_date,
                           'x-oss-user-agent': OSS_UA, 'authorization': auth_key}
                if hash_ctx:
                    headers['X-Oss-Hash-Ctx'] = hash_ctx
                try:
                    etag = self._oss_put(path, url, headers, offset, psize,
                                         on_progress=part_progress(pn))
                    break
                except Exception as e:
                    last = '%s: %s' % (type(e).__name__, e)
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
            if not etag:
                raise QuarkError('分片 %d 上传失败（重试 3 次）：%s' % (pn, last))
            with self._lock:
                done[0] += 1
                log('      片 %d/%d 完成 (%d/%d)' % (pn, part_total, done[0], part_total))
            return pn, etag

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(do_part, pn) for pn in range(1, part_total + 1)]
            for f in as_completed(futs):
                pn, etag = f.result()
                etags[pn] = etag

        xml_data = self._build_complete_xml([etags[i] for i in range(1, part_total + 1)])
        c_date = _utc_now()
        c_meta = self._auth_meta_complete(c_date, bucket, obj_key, upload_id, xml_data, callback)
        c_key = self._get_auth(task_id, auth_info, c_meta)
        xml_md5 = base64.b64encode(hashlib.md5(xml_data.encode()).digest()).decode()
        cb = base64.b64encode(json.dumps(callback, separators=(',', ':')).encode()).decode()
        c_headers = {'Content-Type': 'application/xml', 'x-oss-date': c_date,
                     'x-oss-user-agent': COMPLETE_UA, 'x-oss-callback': cb,
                     'Content-MD5': xml_md5, 'authorization': c_key}
        self._complete('https://%s.pds.quark.cn/%s?uploadId=%s' % (bucket, obj_key, upload_id),
                       c_headers, xml_data)
        log('    分片合并完成')


if __name__ == '__main__':
    cookie_path = r'D:\1\摄影\deepseek_bilibili\quark_cookie.txt'
    with open(cookie_path, encoding='utf-8-sig') as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
    cookie = ' '.join(lines)
    if '__puus' not in cookie or '__pus' not in cookie:
        print('quark_cookie.txt 里没有 __pus / __puus，请重新复制浏览器 cookie')
        sys.exit(1)
    q = Quark(cookie)
    print('=== 账号信息 ===')
    r = q.api('GET', '/1/clouddrive/member')
    print(json.dumps(r, ensure_ascii=False)[:1500])
    print()
    print('=== 根目录 ===')
    r = q.api('GET', '/1/clouddrive/file/sort', params={
        'pdir_fid': '0', '_page': 1, '_size': 20,
        '_fetch_total': 1, '_fetch_sub_dirs': 1, '_sort': 'updated_at:desc'})
    print(json.dumps(r, ensure_ascii=False)[:2000])
