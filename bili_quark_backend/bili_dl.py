"""B 站视频下载器：直取 playurl，选最高画质(4K/AOD 优先 AVC)，用 ffmpeg 合并成 mp4。

比 yt-dlp 可靠：yt-dlp 在本账号下只能看到 1080p，直连接口能拿到 qn=120 的 2160x3840。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bili_api import Bili, load_cookie, enc_wbi
import http_util


def _candidate_dirs():
    """按平台返回 ffmpeg / ffprobe 的常见安装目录（是否真的存在由 _find_tool 判断）。"""
    if os.name == 'nt':
        pf = os.environ.get('ProgramFiles') or r'C:\Program Files'
        return [r'D:\Shotcut',                                        # 本机原有默认值，保持兼容
                r'C:\ffmpeg\bin',
                os.path.join(pf, 'ffmpeg', 'bin')]
    if sys.platform == 'darwin':
        return ['/opt/homebrew/bin',      # Apple Silicon 版 Homebrew
                '/usr/local/bin',         # Intel 版 Homebrew
                '/opt/local/bin']         # MacPorts
    return ['/usr/bin',                   # Linux / 其他 POSIX
            '/usr/local/bin',
            '/snap/bin']


def _find_tool(cmd, env_var, dirs=None):
    """查找 ffmpeg / ffprobe，优先级：环境变量 → PATH → 常见安装位置。

    返回可执行文件路径；都找不到时返回裸命令名（如 'ffmpeg'），
    交给子进程再去 PATH 里试一次，报错信息由调用方给出。
    本函数不抛异常——模块级导入不能因为找不到 ffmpeg 而失败。
    """
    val = os.environ.get(env_var)
    if val:
        return val
    found = shutil.which(cmd)
    if found:
        return found
    exe = (cmd + '.exe') if os.name == 'nt' else cmd
    for d in (dirs if dirs is not None else _candidate_dirs()):
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return cmd


# 可被环境变量覆盖（只新增，不改签名/行为）：BILI_FFMPEG / BILI_FFPROBE
# 解析顺序：环境变量 → PATH(shutil.which) → 各平台常见安装位置 → 裸命令名 'ffmpeg'
FFMPEG = _find_tool('ffmpeg', 'BILI_FFMPEG')
FFPROBE = _find_tool('ffprobe', 'BILI_FFPROBE')
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title, bvid, page=None, quality=None, maxlen=120):
    """远端与本地统一的文件名：``标题 [BV号][P3][1080p].mp4``。

    page / quality 都不传时与旧版**逐字节一致** —— 存量已上传的文件不能改名。
    多分集传 page、非最高分辨率传 quality。
    """
    t = BAD.sub('_', title or '').strip().rstrip('. ')
    t = re.sub(r'\s+', ' ', t)
    if len(t) > maxlen:
        t = t[:maxlen].rstrip()
    tag = ''
    if page:
        tag += '[P%d]' % int(page)
    if quality:
        tag += '[%dp]' % int(quality)
    return '%s [%s]%s.mp4' % (t, bvid, tag)


def name_prefix(title, bvid, maxlen=120):
    """``标题 [BV号]``。核对远端时用它做前缀匹配 —— 同一 BV 的任意分集/任意分辨率
    文件名都以它开头，因此「这个视频在不在网盘里」不会被 [P2]/[1080p] 后缀骗过去。
    """
    t = BAD.sub('_', title or '').strip().rstrip('. ')
    t = re.sub(r'\s+', ' ', t)
    if len(t) > maxlen:
        t = t[:maxlen].rstrip()
    return '%s [%s]' % (t, bvid)


def short_side(v):
    """一路流的「短边」像素数 —— B 站的清晰度档位就是按短边定义的。

    1080P 横屏是 1920x1080、竖屏是 1080x1920，两者短边都是 1080。
    所以挑分辨率必须看 short_side，只看 height 会把竖屏的 480P（480x854，height=854）
    当成 854P 直接过滤掉，最后掉到最低档。
    """
    w = v.get('width') or 0
    h = v.get('height') or 0
    if w and h:
        return min(w, h)
    return h or w or 0


def pick_formats(data, prefer_avc=True, max_height=None):
    """返回 (video_dict, audio_dict)。

    默认挑最高分辨率（同分辨率优先 AVC）；给了 max_height 就只在
    ``短边 <= max_height`` 的流里挑最高的。若一个都不满足（片源本身就比要求低），
    退回**最低**的那一路，免得"选了 720p 反而下 4K"。
    """
    dash = data.get('dash') or {}
    vids = dash.get('video') or []
    if not vids:
        raise RuntimeError('playurl 未返回 dash 视频流')
    if max_height:
        cap = int(max_height)
        capped = [v for v in vids if short_side(v) <= cap]
        vids = capped or [min(vids, key=lambda x: (short_side(x), x.get('bandwidth') or 0))]
    audio = dash.get('audio') or []
    flac = (dash.get('flac') or {}).get('audio')
    if flac:
        audio = [flac] + audio

    def rank(v):
        codec = v.get('codecs', '')
        if prefer_avc:
            cpref = 1 if codec.startswith('avc') else 0
        else:
            cpref = 1 if codec.startswith('av01') else 0
        return (short_side(v), cpref, v.get('bandwidth', 0))

    best_v = max(vids, key=rank)
    best_a = max(audio, key=lambda a: a.get('bandwidth', 0)) if audio else None
    return best_v, best_a


def remote_size(url, referer='https://www.bilibili.com/', timeout=30, retries=3):
    """不下载内容，只问 CDN 这一路流有多少字节（``Range: bytes=0-0`` → Content-Range）。

    校验「网盘里的文件跟 B 站上的是不是一个东西」时用它算期望大小。拿不到返回 None。
    """
    hdrs = {'User-Agent': UA, 'Referer': referer,
            'Origin': 'https://www.bilibili.com', 'Accept': '*/*',
            'Range': 'bytes=0-0'}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with http_util.opener().open(req, timeout=timeout) as r:
                cr = r.headers.get('Content-Range') or ''
                m = re.search(r'/(\d+)\s*$', cr)
                if m:
                    return int(m.group(1))
                cl = r.headers.get('Content-Length')
                if cl:
                    return int(cl)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def download(url, dest, referer='https://www.bilibili.com/', log=print,
             label='', retries=12, on_progress=None):
    """带断点续传的下载。返回文件大小。

    B 站 CDN 在限速时会「喂一段就 FIN」——连接被正常关闭、但字节数不够。
    所以这里把「被截断」当常态：每轮都带 Range 从已有字节续传，只要本轮比上轮
    多下了字节就继续重试，**连续 3 轮零进展**才放弃（避免对真死的 URL 空转）。

    :param on_progress: 可选回调 ``on_progress(done_bytes, total_bytes, speed)``，
        约每 0.8s 调一次（上限），给上层写实时进度用；抛异常会被忽略，
        进度上报绝不能影响下载本身。
    """
    part = dest + '.part'
    have = os.path.getsize(part) if os.path.exists(part) else 0
    hdrs = {'User-Agent': UA, 'Referer': referer,
            'Origin': 'https://www.bilibili.com', 'Accept': '*/*'}
    t0 = time.time()
    # 0 而不是 t0：第一个 chunk 就上报一次，短下载/快网也不会「进度条一直 0」
    last_cb = 0.0
    total = None
    stale = 0
    for attempt in range(retries):
        ta = time.time()
        before = have
        if have:
            hdrs['Range'] = 'bytes=%d-' % have
        else:
            hdrs.pop('Range', None)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with http_util.opener().open(req, timeout=60) as r:
                cl = r.headers.get('Content-Length')
                total = (int(cl) + have) if cl else None
                mode = 'ab' if (have and r.status == 206) else 'wb'
                if mode == 'wb':
                    # 服务端忽略了 Range（回 200），只能从头重下
                    have = 0
                    before = 0
                done = have
                last = ta
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last > 2.0 and total:
                            sp = (done - before) / max(now - ta, 0.001)
                            log('      %s %.1f%% (%.0f/%.0f MB, %.1f MB/s)' % (
                                label, done * 100.0 / total, done / 2**20,
                                total / 2**20, sp / 2**20))
                            last = now
                        if on_progress and total and (now - last_cb > 0.8 or done >= total):
                            last_cb = now
                            try:
                                on_progress(done, total,
                                            (done - before) / max(now - ta, 0.001))
                            except Exception:
                                pass
            if total and os.path.getsize(part) < total:
                raise RuntimeError('下载不完整 %d/%d' % (os.path.getsize(part), total))
            os.replace(part, dest)
            return os.path.getsize(dest)
        except Exception as e:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if attempt == retries - 1:
                raise
            stale = 0 if have > before else stale + 1
            if stale >= 3:
                raise RuntimeError(
                    '下载连续 %d 轮零进展（%d/%s 字节，共试 %d 次）: %s' % (
                        stale, have, total if total else '?', attempt + 1, e))
            log('      %s 重试 %d/%d: %s（本轮 +%.1f MB，已续传 %.1f/%s MB）' % (
                label, attempt + 1, retries, e, (have - before) / 2**20,
                have / 2**20, ('%.1f' % (total / 2**20)) if total else '?'))
            time.sleep(min(1.5 * (attempt + 1), 8))
    raise RuntimeError('unreachable')


def merge(vpath, apath, out, log=print):
    args = [FFMPEG, '-y', '-loglevel', 'error', '-i', vpath]
    if apath:
        args += ['-i', apath]
    args += ['-c', 'copy', '-movflags', '+faststart']
    if apath:
        args += ['-map', '0:v:0', '-map', '1:a:0']
    args += [out]
    try:
        p = subprocess.run(args, capture_output=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            'ffmpeg 合并失败: 未找到 ffmpeg，请安装或设置 BILI_FFMPEG 环境变量'
            '（实际使用路径: %s；%s）' % (FFMPEG, e))
    if p.returncode != 0:
        raise RuntimeError('ffmpeg 合并失败 (ffmpeg=%s, 返回码 %d): %s' % (
            FFMPEG, p.returncode, p.stderr.decode('utf-8', 'replace')[-400:]))


def probe(path):
    args = [FFPROBE, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,codec_name',
            '-show_entries', 'format=duration',
            '-of', 'json', path]
    try:
        p = subprocess.run(args, capture_output=True)
    except FileNotFoundError as e:
        sys.stderr.write('警告: 未找到 ffprobe（实际使用路径: %s），无法探测 %s: %s\n'
                         % (FFPROBE, path, e))
        return {}
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip()
        sys.stderr.write('警告: ffprobe 探测失败（ffprobe=%s, 返回码 %d）%s: %s\n'
                         % (FFPROBE, p.returncode, path, err[-400:] or '(无 stderr 输出)'))
        return {}
    d = json.loads(p.stdout.decode('utf-8', 'replace'))
    s = (d.get('streams') or [{}])[0]
    return {'width': s.get('width'), 'height': s.get('height'),
            'vcodec': s.get('codec_name'),
            'duration': float(d.get('format', {}).get('duration', 0) or 0)}


def _resolve_cookie(cookie_path):
    """把「cookie 文件路径 / cookie 字符串 / None」统一成 cookie 字符串。

    兼容三种调用：
      - 文件路径（存在就读文件，保持原有行为）
      - 直接传入的整行 cookie 字符串（含 '=' 且不是已存在的文件）
      - None：返回空串，交给调用方随后注入（不再让 open(None) 直接抛 TypeError）
    """
    if not cookie_path:
        return ''
    if os.path.exists(cookie_path):
        return load_cookie(cookie_path)
    return str(cookie_path)


class BiliDownloader:
    def __init__(self, cookie_path, workdir, log=print):
        # 参数名保留 cookie_path 以兼容既有调用；实际也接受 cookie 字符串
        self.b = Bili(_resolve_cookie(cookie_path))
        self.workdir = workdir
        self.log = log
        os.makedirs(workdir, exist_ok=True)

    def get_playinfo(self, bvid, cid):
        # 走 Bili.signed()：412/429 风控时重新取 WBI key 并重新签名再试，
        # 否则下载阶段一旦撞上封禁窗口就会整条失败。
        d = self.b.signed(
            'https://api.bilibili.com/x/player/wbi/playurl',
            {'avid': 0, 'bvid': bvid, 'cid': cid, 'qn': 127, 'fnval': 4048,
             'fourk': 1, 'platform': 'pc', 'web_location': 1315873},
            referer='https://www.bilibili.com/video/%s' % bvid,
            attempts=3)
        if d.get('code') != 0:
            raise RuntimeError('playurl code=%s msg=%s' % (d.get('code'), d.get('message')))
        return d['data']

    def stream_sizes(self, bvid, cid, max_height=None):
        """只探大小不下载：返回该集在指定分辨率下的流字节数与尺寸信息。"""
        data = self.get_playinfo(bvid, cid)
        v, a = pick_formats(data, max_height=max_height)
        vurl = v.get('baseUrl') or v.get('base_url')
        aurl = (a.get('baseUrl') or a.get('base_url')) if a else None
        vs = remote_size(vurl)
        as_ = remote_size(aurl) if aurl else 0
        total = (vs or 0) + (as_ or 0)
        return {
            'video_bytes': vs, 'audio_bytes': as_, 'total_bytes': total or None,
            'width': v.get('width'), 'height': v.get('height'),
            'quality': short_side(v),
            'codecs': v.get('codecs'), 'bandwidth': v.get('bandwidth'),
            'duration': data.get('timelength', 0) // 1000 or None,
        }

    def download(self, bvid, cid, title, out_dir, prefer_avc=True, on_progress=None,
                 max_height=None, page=None):
        """下载并合并单条视频（某一集）。

        :param on_progress: 可选回调
            ``on_progress(percent, detail, done_bytes, total_bytes, speed)``。
            percent 是**整条下载阶段**的百分比：视频流占 0→97%（无音频流时 0→100%），
            音频流占 97→100%，合并时 detail='合并中' 且 percent=100。
        :param max_height: 分辨率上限（如 1080）。不给就是最高画质。
        :param page: 分集序号（P1/P2…）。多分集视频会体现在文件名里。
        """
        data = self.get_playinfo(bvid, cid)
        all_v = (data.get('dash') or {}).get('video') or []
        top = max([short_side(x) for x in all_v] or [0])
        v, a = pick_formats(data, prefer_avc, max_height=max_height)
        vs = short_side(v)
        # 只有「确实降了画质」才在文件名里标分辨率，默认最高画质保持老名字
        quality = vs if (max_height and vs and top and vs < top) else None
        self.log('    画质 %sx%s（%dp）%s %.1f Mbps%s' % (
            v.get('width'), v.get('height'), vs, v.get('codecs'),
            v.get('bandwidth', 0) / 1e6,
            ('（限 %dp）' % max_height) if max_height else ''))
        dur = data.get('timelength', 0) / 1000.0
        name = safe_name(title, bvid, page=page, quality=quality)
        out = os.path.join(out_dir, name)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            info = probe(out)
            if info.get('duration') and abs(info['duration'] - dur) < 3:
                self.log('    已存在且完整，跳过下载')
                return out, info
        vtmp = os.path.join(self.workdir, bvid + '.video.m4s')
        atmp = os.path.join(self.workdir, bvid + '.audio.m4s')
        vurl = v.get('baseUrl') or v.get('base_url')

        def beam(lo, hi, detail):
            """把某一路流的字节进度线性映射到 [lo, hi] 百分比上。"""
            def cb(done, total, speed):
                pct = (done * 100.0 / total) if total else 0.0
                if on_progress:
                    try:
                        on_progress(lo + (hi - lo) * pct / 100.0, detail, done, total, speed)
                    except Exception:
                        pass
            return cb

        # 音频一般只有几 MB，给 3% 的区间足够看出动静
        vhi = 97.0 if a else 100.0
        download(vurl, vtmp, log=self.log, label='视频流',
                 on_progress=beam(0.0, vhi, '视频流'))
        apath = None
        if a:
            aurl = a.get('baseUrl') or a.get('base_url')
            download(aurl, atmp, log=self.log, label='音频流',
                     on_progress=beam(vhi, 100.0, '音频流'))
            apath = atmp
        self.log('    合并中…')
        if on_progress:
            try:
                on_progress(100.0, '合并中', None, None, None)
            except Exception:
                pass
        merge(vtmp, apath, out, log=self.log)
        for f in (vtmp, atmp):
            if os.path.exists(f):
                os.remove(f)
        info = probe(out)
        self.log('    完成 %sx%s %s %.1fs %.1f MB' % (
            info.get('width'), info.get('height'), info.get('vcodec'),
            info.get('duration', 0), os.path.getsize(out) / 2**20))
        return out, info


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('bvid')
    ap.add_argument('--title', default='test')
    ap.add_argument('--out', default=r'D:\1\摄影\deepseek_bilibili\_test')
    ap.add_argument('--work', default=r'D:\1\摄影\deepseek_bilibili\_tmp')
    args = ap.parse_args()
    d = BiliDownloader(r'D:\1\摄影\deepseek_bilibili\bili_cookie.txt', args.work)
    pl = d.b.get('https://api.bilibili.com/x/player/pagelist?bvid=%s' % args.bvid)
    cid = pl['data'][0]['cid']
    print(d.download(args.bvid, cid, args.title, args.out)[0])
