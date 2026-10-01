/**
 * 面板的 Host 侧数据接口。
 *
 * 客户端运行在浏览器里，读不到磁盘，所以要由 Host 起一条只读的 HTTP 路由，
 * 客户端轮询它拿进度。路由挂在 DSH 自己的 webServer 上（仅回环地址）。
 *
 * 跨平台注意：这里所有路径拼接都用 node:path，不出现写死的分隔符；
 * 目录枚举与文件读取只用 node:fs，Windows / macOS / Linux 行为一致。
 */
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const MAX_BODY = 256 * 1024;

/** 凭据文件名（写进后端目录，@bili_quark/cli 的 credentials 会优先读环境变量再读它们）。 */
const BILI_COOKIE_FILE = 'bili_cookie.txt';
const QUARK_COOKIE_FILE = 'quark_cookie.txt';

/** 取整行 cookie 里某个字段的值。 */
function cookieField(text, key) {
  for (const part of String(text || '').split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i).trim() === key) return part.slice(i + 1).trim();
  }
  return '';
}

/** 毫秒时间戳 → 本地可读时间（拿不到就空串）。 */
function fmtTime(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '';
  try {
    return new Date(n).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return '';
  }
}

/**
 * B 站 cookie 里带真实过期时间戳的字段（bili_ticket_expires，Unix 秒）。
 * 夸克那边 cookie 不带过期信息，只能给「保存时间」。
 */
function expiresFromCookie(text) {
  const v = cookieField(text, 'bili_ticket_expires');
  return /^\d+$/.test(v) ? fmtTime(Number(v) * 1000) : '';
}

/**
 * 当前凭据状态。**绝不返回 cookie 内容**，只回可判断的布尔值、来源与时间，
 * 避免把凭据经由 HTTP 暴露给页面。
 */
function credentialsStatus(cfg, resolvePaths) {
  const dir = resolvePaths(cfg).scriptDir || '';
  const check = (file, marker, envName) => {
    const p = dir ? join(dir, file) : '';
    const envVal = process.env[envName] || '';
    const base = { file: p, envSet: Boolean(envVal) };
    if (!p || !existsSync(p)) {
      // 文件没有但环境变量有：照样能报出过期时间（B 站 cookie 自带）
      return Object.assign(base, {
        present: Boolean(envVal),
        hasMarker: envVal ? !marker || envVal.includes(marker) : false,
        source: envVal ? 'env' : 'none',
        savedAt: '',
        expiresAt: envVal ? expiresFromCookie(envVal) : '',
      });
    }
    let text = '';
    try {
      text = readFileSync(p, 'utf8');
    } catch {
      return Object.assign(base, {
        present: true, hasMarker: false, source: 'file', savedAt: '', expiresAt: '',
      });
    }
    let savedAt = '';
    try {
      savedAt = fmtTime(statSync(p).mtimeMs);
    } catch {
      /* 忽略 */
    }
    const has = marker ? text.includes(marker) : text.trim().length > 0;
    return Object.assign(base, {
      present: true,
      hasMarker: has,
      bytes: Buffer.byteLength(text, 'utf8'),
      source: 'file',
      savedAt,
      expiresAt: expiresFromCookie(text),
    });
  };
  return {
    ok: true,
    scriptDir: dir,
    bili: check(BILI_COOKIE_FILE, 'SESSDATA', 'BILI_COOKIE'),
    quark: check(QUARK_COOKIE_FILE, '__puus', 'QUARK_COOKIE'),
  };
}

/** 只保留非注释、非空行并拼成一行；与后端 credentials 的规整方式一致。 */
function normalizeCookieText(raw) {
  return String(raw || '')
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'))
    .join(' ');
}

/**
 * 保存凭据到后端目录。只接受非空值（空值视为"不修改"）。
 * @returns {{ok:boolean, results?:object, error?:string}}
 */
function saveCredentials(cfg, resolvePaths, body) {
  const dir = resolvePaths(cfg).scriptDir;
  if (!dir) return { ok: false, error: '还没配置 scriptDir，无法确定要写入哪个目录' };
  const wanted = [];
  const bili = normalizeCookieText(body && body.biliCookie);
  const quark = normalizeCookieText(body && body.quarkCookie);
  if (bili) {
    if (!bili.includes('SESSDATA')) {
      return { ok: false, error: 'B 站 cookie 里没找到 SESSDATA，请复制浏览器里完整的 Cookie 整行' };
    }
    wanted.push([BILI_COOKIE_FILE, bili]);
  }
  if (quark) {
    if (!quark.includes('__puus')) {
      return { ok: false, error: '夸克 cookie 里没找到 __puus，请复制浏览器里完整的 Cookie 整行' };
    }
    wanted.push([QUARK_COOKIE_FILE, quark]);
  }
  if (!wanted.length) return { ok: false, error: '两个输入框都是空的，没有需要保存的内容' };

  const results = {};
  for (const [file, text] of wanted) {
    const p = join(dir, file);
    try {
      // 结尾留换行：后端按行读、忽略 # 注释行
      writeFileSync(p, text + '\n', { encoding: 'utf8' });
      results[file] = { saved: true, file: p, bytes: Buffer.byteLength(text, 'utf8') };
    } catch (e) {
      results[file] = { saved: false, file: p, error: (e && e.message) || String(e) };
      return { ok: false, error: `写入 ${file} 失败：${(e && e.message) || e}`, results };
    }
  }
  return { ok: true, results };
}

/** 从 URL 里取查询参数（不用 URL 类，避免对畸形输入抛错）。 */
function queryOf(rawUrl) {
  const out = {};
  const q = String(rawUrl || '').indexOf('?');
  if (q < 0) return out;
  for (const part of String(rawUrl).slice(q + 1).split('&')) {
    if (!part) continue;
    const eq = part.indexOf('=');
    const k = eq < 0 ? part : part.slice(0, eq);
    const v = eq < 0 ? '' : part.slice(eq + 1);
    try {
      out[decodeURIComponent(k)] = decodeURIComponent(v.replace(/\+/g, ' '));
    } catch {
      out[k] = v;
    }
  }
  return out;
}

function sendJson(res, status, payload) {
  let body;
  try {
    body = Buffer.from(JSON.stringify(payload), 'utf8');
  } catch {
    body = Buffer.from('{"ok":false,"error":"序列化失败"}', 'utf8');
    status = 500;
  }
  try {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(body.length),
      'cache-control': 'no-store',
    });
    res.end(body);
  } catch {
    /* 连接可能已断开 */
  }
}

function readBody(req) {
  return new Promise((done) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        done(null);
        try {
          req.destroy();
        } catch {
          /* ignore */
        }
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) {
        done({});
        return;
      }
      try {
        const v = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        done(v && typeof v === 'object' ? v : {});
      } catch {
        done(null);
      }
    });
    req.on('error', () => done(null));
  });
}

function readJson(path) {
  try {
    if (!existsSync(path)) return null;
    return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return null;
  }
}

function dirSize(path, depth = 0) {
  let total = 0;
  let count = 0;
  if (depth > 3) return { bytes: 0, files: 0 };
  try {
    for (const e of readdirSync(path, { withFileTypes: true })) {
      const full = join(path, e.name);
      try {
        if (e.isDirectory()) {
          const sub = dirSize(full, depth + 1);
          total += sub.bytes;
          count += sub.files;
        } else if (e.isFile()) {
          total += statSync(full).size;
          count += 1;
        }
      } catch {
        /* 跳过读不到的条目 */
      }
    }
  } catch {
    /* 目录不存在 */
  }
  return { bytes: total, files: count };
}

function tailText(work, lines = 60) {
  try {
    const p = join(work, 'run.log');
    if (!existsSync(p)) return '';
    const raw = readFileSync(p, 'utf8');
    const arr = raw.split(/\r?\n/).filter((l) => l.length);
    return arr.slice(-Math.min(Math.max(lines, 1), 300)).join('\n');
  } catch {
    return '';
  }
}

/** 枚举 workRoot 下所有 UP 主的工作目录。 */
export function discover(cfg, resolvePaths, isAlive) {
  const { workRoot } = resolvePaths(cfg);
  if (!workRoot || !existsSync(workRoot)) return [];
  let entries = [];
  try {
    entries = readdirSync(workRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const out = [];
  for (const e of entries) {
    // 每个 UP 主一个以 UID 命名的子目录；其余（_logs 等）跳过
    if (!e.isDirectory() || !/^\d{4,20}$/.test(e.name)) continue;
    const work = join(workRoot, e.name);
    const progress = readJson(join(work, 'progress.json'));
    const session = readJson(join(work, 'session.json'));
    const configFile = readJson(join(work, 'config.json'));
    const list = readJson(join(work, 'video_list.json'));

    const pid = (progress && progress.pid) || (session && session.pid) || 0;
    const alive = Boolean(pid) && isAlive(pid);
    let state = (progress && progress.state) || (session ? 'unknown' : 'idle');
    if (state === 'running' && !alive) state = 'stalled';
    if (state === 'starting' && !alive) state = 'failed_start';

    const total = (progress && progress.total) || 0;
    const done = (progress && progress.done) || 0;
    const current = (progress && progress.current) || [];
    /*
     * 总进度把「正在处理的几条」按 overall（条目整体百分比）折算进来。
     * 只用 done/total 的话，并发跑的时候数字要等每条结束才跳一下，看起来像卡住。
     */
    const inflight = current.reduce((s, c) => s + (Number(c.overall) || 0) / 100, 0);
    const local = dirSize(join(work, 'downloads'));

    out.push({
      mid: e.name,
      workDir: work,
      state,
      alive,
      pid,
      total,
      done,
      ok: (progress && progress.ok) || 0,
      fail: (progress && progress.fail) || 0,
      percent: total ? Math.min(100, Math.round(((done + inflight) / total) * 100)) : 0,
      inflight: Math.round(inflight * 100) / 100,
      current,
      started: (progress && progress.started) || (session && session.startedAt) || '',
      updated: (progress && progress.updated) || '',
      lastError: (progress && progress.last_error) || '',
      jobId: (session && session.jobId) || '',
      dirName: (configFile && configFile.quark_dir_name) || '',
      // 输入框要用完整路径预填；配置里没存路径就退回目录名
      dirPath: (configFile && configFile.quark_dir_path) || (configFile && configFile.quark_dir_name) || '',
      dirFid: (configFile && configFile.quark_dir_fid) || '',
      videoTotal: list ? (list.items || []).length : 0,
      videoVertical: list ? (list.items || []).filter((x) => x.vertical === true).length : 0,
      localBytes: local.bytes,
      localFiles: local.files,
    });
  }
  // 正在运行的排前面，其余按更新时间倒序
  out.sort((a, b) => {
    const rank = (x) => (x.alive ? 0 : x.state === 'stalled' ? 1 : 2);
    const d = rank(a) - rank(b);
    if (d !== 0) return d;
    return String(b.updated || b.started).localeCompare(String(a.updated || a.started));
  });
  return out;
}

/**
 * 组装某个 UP 主的视频清单（供面板的分页列表用）。
 * 附带「是否已上传」与「远端是否存在」（后者来自 verify 落盘的 remote_index.json），
 * 并把 B 站封面地址升成 https —— https 页面里加载 http 图会被拦。
 */
export function buildVideoList(cfg, resolvePaths, mid) {
  const root = resolvePaths(cfg).workRoot;
  if (!root || !mid) return { ok: false, error: '缺少 mid 或未配置 workRoot' };
  const work = join(root, String(mid));
  const list = readJson(join(work, 'video_list.json'));
  if (!list) {
    return {
      ok: false,
      mid: String(mid),
      workDir: work,
      error:
        `该 UP 主还没有投稿清单（${join(work, 'video_list.json')}），` +
        '先用「添加 UP 主」抓取一次。若刚抓过，请点「刷新」再看。',
      items: [],
      total: 0,
      uploadedCount: 0,
    };
  }
  // state.jsonl 逐行 JSON，同一 bvid 以最后一条为准（留着整条记录，面板要看分集）
  const uploadedRec = new Map();
  try {
    const statePath = join(work, 'state.jsonl');
    if (existsSync(statePath)) {
      for (const line of readFileSync(statePath, 'utf8').split(/\r?\n/)) {
        const t = line.trim();
        if (!t) continue;
        let rec = null;
        try {
          rec = JSON.parse(t);
        } catch {
          continue;
        }
        if (!rec || !rec.bvid) continue;
        if (rec.status === 'uploaded') uploadedRec.set(rec.bvid, rec);
        else uploadedRec.delete(rec.bvid); // 重跑时后一条记录优先
      }
    }
  } catch {
    /* state 读不到就当全部未上传 */
  }

  /*
   * 远端存在性快照：由 bili_quark_verify 落盘（<workdir>/remote_index.json）。
   * 有了它，「待处理」就以夸克**实际文件列表**为准 —— state.jsonl 说传过、
   * 但目标目录里其实没有的条目会重新变成待处理，可以勾选补传。
   * 换了目标目录（fid 不一致）时旧快照作废，退回按 state 判断并提示重新核对。
   */
  const remoteIndex = readJson(join(work, 'remote_index.json'));
  const configFile = readJson(join(work, 'config.json'));
  const indexFid = (remoteIndex && remoteIndex.quark_dir_fid) || '';
  const currentFid = (configFile && configFile.quark_dir_fid) || '';
  const remoteStale = Boolean(remoteIndex) && Boolean(indexFid) && Boolean(currentFid) && indexFid !== currentFid;
  const remoteUsable = Boolean(remoteIndex) && !remoteStale;
  const remote = new Map();
  if (remoteUsable && Array.isArray(remoteIndex.items)) {
    for (const r of remoteIndex.items) {
      if (r && r.bvid) remote.set(String(r.bvid), r);
    }
  }

  const raw = (list.items || []).map((x) => {
    const pic = String(x.pic || '');
    const hit = remote.get(String(x.bvid));
    const rec = uploadedRec.get(x.bvid) || {};
    // 分集（P）列表：面板给多分集视频一个下拉，让用户选具体下哪一集
    const pages = Array.isArray(x.pages)
      ? x.pages.map((p) => ({
          cid: p.cid,
          page: p.page,
          part: p.part || '',
          duration: p.duration || 0,
        }))
      : [];
    return {
      bvid: x.bvid,
      title: x.title || '',
      pic: pic.startsWith('http://') ? 'https://' + pic.slice(7) : pic,
      created: x.created || 0,
      duration: x.duration || 0,
      width: x.width || 0,
      height: x.height || 0,
      vertical: x.vertical === true,
      uploaded: uploadedRec.has(x.bvid),
      // 本机记着上传的是哪一集/多大（重跑时用来预选下拉）
      statePage: rec.page || null,
      stateBytes: rec.bytes || 0,
      pages,
      pageCount: pages.length || x.page_count || 1,
      // 远端核对结果：remoteChecked=false 表示这条没进过快照，此时按 uploaded 判断
      remoteChecked: Boolean(hit),
      remotePresent: Boolean(hit && hit.present === true),
      remoteSize: hit && hit.size ? Number(hit.size) || 0 : 0,
      remoteName: (hit && hit.name) || '',
      remoteFiles: (hit && hit.files) || [],
    };
  });
  const pendingOf = (v) => (v.remoteChecked ? !v.remotePresent : !v.uploaded);
  return {
    ok: true,
    mid: String(mid),
    items: raw,
    total: raw.length,
    uploadedCount: raw.filter((x) => x.uploaded).length,
    // 面板的「待处理」= 夸克上还缺的（有核对结果时以远端为准，否则按 state）
    pendingCount: raw.filter(pendingOf).length,
    remoteCheckedAt: remoteUsable ? (remoteIndex.checked_at || '') : '',
    remoteCheckedCount: remoteUsable ? raw.filter((x) => x.remoteChecked).length : 0,
    remoteMissingCount: remoteUsable ? raw.filter((x) => x.remoteChecked && !x.remotePresent).length : 0,
    remoteFiles: remoteUsable ? Number(remoteIndex.remote_files) || 0 : 0,
    remoteDirFid: remoteUsable ? indexFid : '',
    remoteStale,
    // 清单来源合集（抓取时记在 video_list.json 里）：0/空 = 全部投稿
    seasonId: list.season_id || 0,
    seasonName: list.season_name || '',
  };
}

/**
 * 注册面板数据路由。
 *
 * @param ctx      宿主上下文（用于 ctx.inject）
 * @param deps     { cfg, resolvePaths, isAlive, tools, killTree }
 * @returns void；路由随 ctx 生命周期释放
 */
export function registerPanelRoutes(ctx, deps) {
  const { cfg, resolvePaths, isAlive, tools } = deps;

  const callTool = async (name, args) => {
    const def = tools.get(name);
    if (!def) return { ok: false, error: `工具不存在：${name}` };
    try {
      return await def.execute(args, {});
    } catch (e) {
      return { ok: false, error: (e && e.message) || String(e) };
    }
  };

  const payloadOf = (value) => {
    if (value && value.ok === false) return { ok: false, error: String(value.error || '失败') };
    return { ok: true, data: value };
  };

  /**
   * 请求处理核心：只依赖 pathname/method/body，返回 { status, json }。
   * 两条注册路径（connection.fetch 与 webServer）共用它，逻辑只写一份。
   */
  async function buildReply({ pathname, method, query, body }) {
    if (method === 'GET' && pathname === '/bili-quark/api/data') {
      const sessions = discover(cfg, resolvePaths, isAlive);
      const active = sessions.find((s) => s.alive) || sessions[0] || null;
      return {
        status: 200,
        json: {
          ok: true,
          now: new Date().toISOString(),
          workRoot: resolvePaths(cfg).workRoot || '',
          sessions,
          activeMid: active ? active.mid : '',
          logTail: active ? tailText(active.workDir, 80) : '',
        },
      };
    }

    if (method === 'GET' && pathname === '/bili-quark/api/log') {
      const root = resolvePaths(cfg).workRoot;
      return {
        status: 200,
        json: {
          ok: true,
          mid: (query && query.mid) || '',
          text: tailText(
            query && query.mid && root ? join(root, String(query.mid)) : '',
            Number(query && query.lines) || 120,
          ),
        },
      };
    }

    if (method === 'GET' && pathname === '/bili-quark/api/videos') {
      const mid = (query && query.mid) || '';
      if (!mid) return { status: 400, json: { ok: false, error: '缺少 mid（B 站 UID）' } };
      return { status: 200, json: buildVideoList(cfg, resolvePaths, mid) };
    }

    if (method === 'GET' && pathname === '/bili-quark/api/credentials') {
      // 只回状态，不回 cookie 内容
      return { status: 200, json: credentialsStatus(cfg, resolvePaths) };
    }

    if (method === 'POST' && pathname === '/bili-quark/api/action') {
      if (body === null) return { status: 400, json: { ok: false, error: '请求体不是合法 JSON' } };
      const action = String((body && body.action) || '');
      const mid = body && body.mid ? String(body.mid) : '';
      const needsMid = ['targets', 'verify', 'runDry', 'run', 'stop', 'fetchList', 'saveDir', 'addUp', 'checkSrc', 'repair', 'seasons'];
      if (!mid && needsMid.includes(action)) {
        return { status: 400, json: { ok: false, error: '缺少 mid（B 站 UID）' } };
      }
      let result;
      switch (action) {
        case 'targets':
          result = await callTool('bili_quark_targets', { mid });
          break;
        case 'verify':
          result = await callTool('bili_quark_verify', { mid });
          break;
        case 'runDry':
          result = await callTool('bili_quark_run', {
            mid,
            dryRun: true,
            force: body.force === true,
            maxHeight: body.maxHeight ? Number(body.maxHeight) : undefined,
            page: body.page ? Number(body.page) : undefined,
            only: Array.isArray(body.only) ? body.only.map(String) : undefined,
          });
          break;
        case 'run':
          // only = 面板里勾选的 BV 号，可带分集（`BV1xxx:2`）。有勾选时宿主会自动
          // 清空默认排除名单、并按选择处理（横屏竖屏一视同仁）。
          // force = 勾选即处理：连 state 里记着「已上传」的条目也重跑，
          // 这样「夸克上其实缺」的条目点一下就能补上。
          result = await callTool('bili_quark_run', {
            mid,
            concurrency: body.concurrency,
            deleteLocal: body.deleteLocal !== false,
            includeHorizontal: body.includeHorizontal === true,
            force: body.force === true,
            maxHeight: body.maxHeight ? Number(body.maxHeight) : undefined,
            page: body.page ? Number(body.page) : undefined,
            only: Array.isArray(body.only) && body.only.length ? body.only.map(String) : undefined,
            dryRun: false,
          });
          break;
        case 'checkSrc':
          // 与 B 站源核对大小（拿 CDN 的真实字节数，不下载）
          result = await callTool('bili_quark_check', {
            mid,
            only: Array.isArray(body.only) && body.only.length ? body.only.map(String) : undefined,
            page: body.page ? Number(body.page) : undefined,
            maxHeight: body.maxHeight ? Number(body.maxHeight) : undefined,
            tolerance: body.tolerance != null ? Number(body.tolerance) : undefined,
          });
          break;
        case 'repair':
          // 修复大小不符条目：删除夸克端文件 + 重置 state，返回待重跑 BV 列表
          result = await callTool('bili_quark_repair', {
            mid,
            only: Array.isArray(body.only) && body.only.length ? body.only.map(String) : undefined,
            quarkDir: body.quarkDir ? String(body.quarkDir) : undefined,
          });
          break;
        case 'checkCred':
          // 凭据是否还有效（B 站 nav / 夸克 member），顺带回过期时间
          result = await callTool('bili_quark_auth', {});
          break;
        case 'resolve':
          result = await callTool('bili_quark_resolve', {
            mid,
            path: body.path ? String(body.path) : undefined,
            fid: body.fid ? String(body.fid) : undefined,
            create: body.create !== false,
          });
          break;
        case 'seasons':
          // 只列合集（一次只读接口，秒回），给面板做"选哪个合集"用
          result = await callTool('bili_quark_seasons', { mid });
          break;
        case 'fetchList':
          // 抓取/刷新投稿清单（只读接口，不下载）；给了 seasonId 就只抓该合集
          result = await callTool('bili_quark_fetch', {
            mid,
            seasonId: body.seasonId ? Number(body.seasonId) : undefined,
          });
          break;
        case 'saveDir':
          // 只保存目标目录，不抓清单（给已有会话换目录用）
          result = await callTool('bili_quark_resolve', {
            mid,
            path: body.path ? String(body.path) : undefined,
            create: body.create !== false,
          });
          break;
        case 'addUp': {
          // 面板里新增一个 UP 主：只抓投稿清单（不下载视频），所以可以放心在这里做。
          // 目标目录不在这里设——面板只有「夸克目标目录」卡片一个入口（saveDir / resolve）。
          // 兼容：调用方若仍传了 path，就顺手解析并保存，省一次往返。
          const root = resolvePaths(cfg).workRoot || '';
          const listPath = root ? join(root, String(mid), 'video_list.json') : '';
          const steps = [];
          const seasonId = body.seasonId ? Number(body.seasonId) : undefined;
          // 指定了合集就一定重抓（用户是明确来换合集的），否则沿用"清单已有就不重抓"
          if (seasonId || !existsSync(listPath)) {
            const fetched = await callTool('bili_quark_fetch', { mid, seasonId });
            steps.push({ step: 'fetch', ok: fetched.ok !== false, error: fetched.error || '' });
            if (fetched.ok === false) {
              result = { ok: false, error: `抓取投稿清单失败：${fetched.error}`, steps };
              break;
            }
          }
          const dirPath = body.path ? String(body.path) : '';
          if (dirPath) {
            const resolved = await callTool('bili_quark_resolve', { mid, path: dirPath, create: true });
            steps.push({ step: 'resolve', ok: resolved.ok !== false, error: resolved.error || '' });
            if (resolved.ok === false) {
              result = { ok: false, error: `解析目标目录失败：${resolved.error}`, steps };
              break;
            }
            result = { ok: true, mid, dirName: resolved.name || '', fid: resolved.fid || '', steps };
          } else {
            // 正常路径：清单已（或已存在）就绪，目标目录留给面板的目录卡片设置
            result = { ok: true, mid, dirName: '', fid: '', steps };
          }
          break;
        }
        case 'saveCred': {
          // 保存 B 站 / 夸克 cookie 到后端目录（面板的凭据输入框用）。
          // 只回结果摘要，**不回**凭据状态内容 —— 任何 cookie 片段都不该过 HTTP。
          const saved = saveCredentials(cfg, resolvePaths, body);
          result = saved.ok
            ? { ok: true, saved: true, files: Object.keys(saved.results || {}), hint: '保存成功；前端会重新拉一次状态' }
            : { ok: false, error: saved.error };
          break;
        }
        case 'account':
          result = await callTool('bili_quark_status', { mid: mid || '0', mode: 'quark-info' });
          break;
        case 'stop': {
          const sessions = discover(cfg, resolvePaths, isAlive);
          const target = sessions.find((s) => s.mid === mid);
          if (!target || !target.pid || !isAlive(target.pid)) {
            result = { ok: false, error: '没有正在运行的作业' };
          } else {
            const killed = deps.killTree ? deps.killTree({ kill: () => false }, target.pid) : false;
            result = killed
              ? { ok: true, stopped: true, pid: target.pid }
              : { ok: false, error: '结束进程失败，请手动处理' };
          }
          break;
        }
        default:
          return { status: 400, json: { ok: false, error: `未知操作：${action}` } };
      }
      return { status: 200, json: payloadOf(result) };
    }

    return { status: 404, json: { ok: false, error: `未知接口：${pathname}` } };
  }

  const pathnameOf = (rawUrl) => {
    try {
      return new URL(rawUrl || '/', 'http://127.0.0.1').pathname;
    } catch {
      return String(rawUrl || '/').split('?')[0];
    }
  };

  const guarded = async (fn) => {
    try {
      return await fn();
    } catch (e) {
      return { status: 500, json: { ok: false, error: (e && e.message) || String(e) } };
    }
  };

  // 首选：Connection 的认证围栏内注册（官方推荐姿势）。拿不到就退回 webserver。
  ctx.inject(['connection'], (hostCtx) => {
    const conn = hostCtx.connection || hostCtx.get?.('connection');
    const reg = conn && conn.fetch && typeof conn.fetch.register === 'function' ? conn.fetch : null;
    if (!reg) return;
    hostCtx.effect(
      () =>
        reg.register({
          path: '/bili-quark/api',
          methods: ['GET', 'POST'],
          requestBody: 'buffered',
          fetch: async (request) => {
            const url = (request && request.url) || '/';
            const pathname = pathnameOf(url);
            let body = null;
            if (String((request && request.method) || 'GET').toUpperCase() === 'POST') {
              try {
                body = await request.json();
              } catch {
                body = null;
              }
            }
            const reply = await guarded(() =>
              buildReply({
                pathname,
                method: String((request && request.method) || 'GET').toUpperCase(),
                query: queryOf(url),
                body,
              }),
            );
            return new Response(JSON.stringify(reply.json), {
              status: reply.status,
              headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
            });
          },
        }),
      'bili-quark: panel api route (connection)',
    );
  });

  // 兜底：直接挂 webserver（签名是用源码确认过的）。两条都成功也无妨——
  // connection 在前缀更长处优先命中；即便如此，这里也不会重复处理同一请求。
  ctx.inject(['webServer'], (hostCtx) => {
    const webServer = hostCtx.webServer || hostCtx.get?.('webServer');
    if (!webServer || typeof webServer.register !== 'function') return;

    hostCtx.effect(
      () =>
        webServer.register({
          kind: 'prefix',
          path: '/bili-quark/api',
          async handler(req, res) {
            const method = String(req.method || 'GET').toUpperCase();
            const pathname = pathnameOf(req.url);
            const body = method === 'POST' ? await readBody(req) : undefined;
            const reply = await guarded(() =>
              buildReply({ pathname, method, query: queryOf(req.url), body }),
            );
            sendJson(res, reply.status, reply.json);
          },
        }),
      'bili-quark: panel api route (webserver)',
    );
  });
}
