"""最终核对：本地清单 vs 夸克远端实际文件，逐条比对文件名与字节数。"""
import json
import os
import sys

sys.path.insert(0, r'D:\1\摄影\deepseek_bilibili')
from run_all import (read_cookie, COOKIE_QUARK, CONFIG, DL_DIR,  # noqa: E402
                     targets, safe_name, load_state)
from quark_upload import Quark  # noqa: E402

cfg = json.load(open(CONFIG, encoding='utf-8'))
fid = cfg['quark_dir_fid']
q = Quark(read_cookie(COOKIE_QUARK))

items = q.list_all(fid, size=100, sub_dirs=0)
remote = {it['file_name']: it for it in items if not it.get('dir')}
print('夸克目录「%s」实际文件数: %d' % (cfg.get('quark_dir_name'), len(remote)))

ts = targets()
state = load_state()
ok, miss, size_bad = [], [], []
for t in ts:
    name = safe_name(t['title'], t['bvid'])
    r = remote.get(name)
    if not r:
        miss.append((t, name))
        continue
    rsize = int(r.get('size') or 0)
    ssize = int(state.get(t['bvid'], {}).get('bytes') or 0)
    if ssize and ssize != rsize:
        size_bad.append((t, name, ssize, rsize))
    ok.append((t, name, rsize))

print()
print('目标条数        : %d' % len(ts))
print('远端已确认      : %d' % len(ok))
print('远端缺失        : %d' % len(miss))
print('字节数不一致    : %d' % len(size_bad))
print('远端合计        : %.2f GB' % (sum(s for _, _, s in ok) / 2**30))
print()
for t, name in miss:
    print('  缺失: %s %s' % (t['bvid'], name[:60]))
for t, name, a, b in size_bad:
    print('  大小不符: %s 本地记录=%d 远端=%d' % (t['bvid'], a, b))

# 远端是否有不属于本项目的多余文件
want = {safe_name(t['title'], t['bvid']) for t in ts}
extra = sorted(set(remote) - want)
print()
print('远端多余文件    : %d' % len(extra))
for n in extra:
    print('  多余: %s (%.1f MB)' % (n[:60], remote[n]['size'] / 2**20))

# 本地残留
locals_ = [f for f in os.listdir(DL_DIR)] if os.path.isdir(DL_DIR) else []
print()
print('本地残留文件    : %d' % len(locals_))
for f in locals_:
    print('  残留: %s' % f[:60])

json.dump({'ok': len(ok), 'miss': [t['bvid'] for t, _ in miss],
           'size_bad': [t['bvid'] for t, _, _, _ in size_bad],
           'total_bytes': sum(s for _, _, s in ok)},
          open(r'D:\1\摄影\deepseek_bilibili\final_report.json', 'w'), ensure_ascii=False, indent=1)
