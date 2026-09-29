/**
 * Bilibili Quark Transform —— B 站投稿 → 夸克网盘 流水线插件（Host 侧）
 *
 * 设计：本插件不重新实现下载/上传逻辑，而是把参数接进已实测跑通的 Python 后端：
 *   scriptDir = <后端目录，含 bili_quark 包>
 *   入口：python -m bili_quark.cli <command> ...
 *
 * 执行层的硬约束（实测得出）：
 *   宿主所在沙箱**禁止用管道捕获子进程 stdout/stderr**（spawn 管道直接 EPERM），
 *   但允许子进程把结果写进文件。因此所有结构化结果都通过 `--json-out <文件>` 落盘后读取。
 *
 * 八个工具：
 *   bili_quark_fetch    抓取 UP 主投稿并探测分辨率（生成清单，含分集信息）
 *   bili_quark_targets  预览待处理清单 / 体积估算 / 磁盘检查
 *   bili_quark_run      启动流水线（后台作业，立即返回；可限分辨率、可指定分集）
 *   bili_quark_status   读进度（progress.json + 进程存活 + 日志尾部）
 *   bili_quark_verify   以夸克远端实际列表为准做最终核对
 *   bili_quark_check    拿 B 站源流真实字节数，核对网盘里的文件大小是否一致
 *   bili_quark_auth     检查 B 站 / 夸克凭据是否还有效、何时过期
 *   bili_quark_resolve  解析/创建夸克目标目录，拿 fid
 */
import { spawn } from 'node:child_process';
import {
  closeSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve as pathResolve } from 'node:path';
import { registerPanelRoutes } from './api.js';
import { IS_WINDOWS, isAlive, killTree, loadSchemastery, resolvePythonCmd, resolveUserPath, workerEnv } from './platform.js';

/** 跨平台默认值；每台机器都该按自己的布局在插件配置里调 workRoot / scriptDir。 */
const CONFIG_DEFAULTS = {
  workRoot: '',
  scriptDir: '',
  pythonPath: '',
  ffmpegPath: '',
  ffprobePath: '',
  diskMinGb: 12,
  startGraceMs: 2500,
  backendTimeoutMs: 20 * 60 * 1000,
  maxLogTail: 6000,
  maxCaptureLogBytes: 2 * 1024 * 1024,
};

/** 已启动的后台作业： key = mid */
const jobs = new Map();

// ---------------------------------------------------------------- 基础工具

function text(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function block(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : text(value) }];
}

function clampTail(s, max) {
  if (typeof s !== 'string') return '';
  return s.length <= max ? s : s.slice(s.length - max);
}

function parseMid(raw) {
  if (raw === undefined || raw === null) return null;
  const s = String(raw).trim();
  if (!s) return null;
  if (/^\d{4,20}$/.test(s)) return s;
  const m = s.match(/(?:space\.bilibili\.com\/|^)(\d{4,20})(?:[/?#]|$)/i);
  if (m) return m[1];
  const any = s.match(/(\d{4,20})/);
  return any ? any[1] : null;
}

function splitDirArg(dir) {
  const s = String(dir || '').trim();
  if (!s) return { fid: null, path: null };
  if (s.includes('/') || s.includes('\\')) return { fid: null, path: s };
  return { fid: s, path: null };
}

function normalizeCookie(v) {
  if (!v) return null;
  const s = String(v)
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'))
    .join(' ');
  return s || null;
}

/**
 * 取凭据：显式参数 > 后端目录里的 bili_cookie.txt / quark_cookie.txt。
 * 后端把 cookie 文件放在 scriptDir，而工作目录是按 UP 主另建的，
 * 所以这里必须主动读取并注入，否则后端只会去 <workdir> 找而失败。
 */
function loadCookie(explicit, scriptDir, fileName) {
  const given = normalizeCookie(explicit);
  if (given) return given;
  const path = join(scriptDir, fileName);
  try {
    if (!existsSync(path)) return null;
    return normalizeCookie(readFileSync(path, 'utf8'));
  } catch {
    return null;
  }
}

function cookieEnv(cfg, biliCookie, quarkCookie) {
  const env = {};
  const b = loadCookie(biliCookie, backendDir(cfg), 'bili_cookie.txt');
  const q = loadCookie(quarkCookie, backendDir(cfg), 'quark_cookie.txt');
  if (b) env.BILI_COOKIE = b;
  if (q) env.QUARK_COOKIE = q;
  return env;
}

function tryParseJson(s) {
  if (!s) return null;
  const t = String(s).trim();
  const attempts = [t];
  const first = t.indexOf('{');
  const last = t.lastIndexOf('}');
  if (first >= 0 && last > first) attempts.push(t.slice(first, last + 1));
  for (const a of attempts) {
    try {
      const v = JSON.parse(a);
      if (v && typeof v === 'object') return v;
    } catch {
      /* 继续尝试 */
    }
  }
  return null;
}

function readJson(path) {
  try {
    if (!existsSync(path)) return null;
    return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return null;
  }
}

/** 原子写：先写临时文件再 rename，避免读到半截 JSON。 */
function writeJsonAtomic(path, value) {
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(value, null, 1)}\n`, 'utf8');
  renameSync(tmp, path);
}

function tailFile(path, maxBytes = 8000) {
  try {
    if (!existsSync(path)) return '';
    const size = statSync(path).size;
    const start = Math.max(0, size - maxBytes);
    const fd = openSync(path, 'r');
    try {
      const buf = Buffer.alloc(size - start);
      readSync(fd, buf, 0, buf.length, start);
      return clampTail(buf.toString('utf8'), maxBytes);
    } finally {
      closeSync(fd);
    }
  } catch {
    return '';
  }
}

function previewArgs(cmd, args) {
  const quote = (a) => (/[\s"]/.test(String(a)) ? JSON.stringify(String(a)) : String(a));
  return [cmd, ...args].map(quote).join(' ');
}

function openLog(path) {
  try {
    if (existsSync(path) && statSync(path).size > 4 * 1024 * 1024) {
      writeFileSync(path, '', 'utf8');
    }
  } catch {
    /* 忽略 */
  }
  try {
    return openSync(path, 'a');
  } catch {
    return null;
  }
}

function rmQuiet(path) {
  try {
    if (existsSync(path)) unlinkSync(path);
  } catch {
    /* 忽略 */
  }
}

// ---------------------------------------------------------------- 运行时

/** 判断某个目录是不是真正的后端（含 bili_quark/cli.py）。 */
function looksLikeBackend(dir) {
  if (!dir) return false;
  try {
    return existsSync(join(dir, 'bili_quark', 'cli.py'));
  } catch {
    return false;
  }
}

/**
 * 自动找后端目录：解压分发包后，后端目录名可能是 bili_quark_backend / deepseek_bilibili
 * 之类，未必和配置里写的一致。这里在若干候选位置里找「含 bili_quark/cli.py」的那个，
 * 找到就用，省得用户手改 scriptDir。
 */
function discoverBackend(cfg, scriptDir, workRoot) {
  const here = dirname(fileURLToPath(import.meta.url)); // 插件目录
  const candidates = [
    workRoot,
    join(here, '..', 'deepseek_bilibili'),
    join(here, '..', 'bili_quark_backend'),
    dirname(String(scriptDir || '')),
    scriptDir,
    here,
  ].filter(Boolean);
  const seen = new Set();
  for (const c of candidates) {
    const norm = String(c);
    if (seen.has(norm)) continue;
    seen.add(norm);
    if (looksLikeBackend(norm)) return norm;
  }
  return '';
}

/**
 * 解析本机路径配置。每个平台各自指定 workRoot / scriptDir；
 * 只给 scriptDir 时，workRoot 默认取它的同级 `_bili_quark_work`。
 * scriptDir 没配或配的路径不存在时，会尝试自动发现后端目录。
 */
function resolvePaths(cfg) {
  const configured = resolveUserPath(cfg.scriptDir || '');
  const workRoot = resolveUserPath(cfg.workRoot || '')
    || (configured ? join(dirname(configured), '_bili_quark_work') : '');
  const scriptDir = looksLikeBackend(configured) ? configured : discoverBackend(cfg, configured, workRoot);
  return { scriptDir, workRoot };
}

function backendDir(cfg) {
  const { scriptDir } = resolvePaths(cfg);
  if (!scriptDir) {
    throw new Error(
      `还没配置后端目录，也没能自动找到。请在插件配置里把 scriptDir 指向后端目录` +
        `（含 bili_quark 包与 cookie 文件）。`,
    );
  }
  return scriptDir;
}

function assertBackend(cfg) {
  const dir = backendDir(cfg);
  const cli = join(dir, 'bili_quark', 'cli.py');
  if (!existsSync(cli)) {
    throw new Error(
      `后端入口不存在：${cli}\n请把 deepseek_bilibili 放到该位置，或用插件配置 scriptDir 指向它。`,
    );
  }
  return dir;
}

/**
 * 用「fd 重定向 + 写结果文件」的方式运行 Python 并拿到 JSON 结果。
 * 完全不使用管道，因此在禁止管道的沙箱里也能工作。
 */
async function runPythonJson(cfg, dir, cliArgs, opts = {}) {
  const { timeoutMs = cfg.backendTimeoutMs, work, env: extraEnv = {} } = opts;
  const stamp = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const outFile = join(work || dir, `_result-${stamp}.json`);
  // 每次调用独立日志文件：否则会读到上一次调用的残留日志而误判错误原因。
  // 放进 work 的 _logs/ 下，避免污染后端源码目录；用完即删。
  const logDir = join(work || dir, '_logs');
  try {
    mkdirSync(logDir, { recursive: true });
  } catch {
    /* 退化为 dir */
  }
  const logFile = join(logDir, `backend-${stamp}.log`);
  rmQuiet(outFile);

  const args = [...cliArgs, '--json-out', outFile];
  const py = await resolvePython(cfg);
  const full = [...py.pre, ...args];

  const cleanup = () => {
    if (process.env.BQ_DEBUG) return; // 调试模式保留日志与结果文件
    rmQuiet(outFile);
    try {
      if (existsSync(logFile) && statSync(logFile).size === 0) rmQuiet(logFile);
    } catch {
      /* 忽略 */
    }
  };

  const env = workerEnv({
    backendDir: dir,
    ffmpegPath: resolveUserPath(cfg.ffmpegPath || ''),
    ffprobePath: resolveUserPath(cfg.ffprobePath || ''),
    extra: extraEnv,
  });
  const r = await execFd(py.cmd, full, { cwd: dir, timeoutMs, logFile, env });
  if (r.error) {
    cleanup();
    return { ok: false, fatal: true, error: `${r.error}`, logFile, command: previewArgs(py.cmd, full) };
  }

  const parsed = readJson(outFile);
  const log = r.log || '';
  cleanup();
  if (parsed) return { ok: true, value: parsed, code: r.code, logFile, command: previewArgs(py.cmd, full) };

  // 结果文件缺失：把执行日志回传，便于定位（绝不与其它调用共用日志）
  return {
    ok: false,
    fatal: false,
    code: r.code,
    error: `后端未产出结果文件（exit ${r.code}）`,
    log: clampTail(log, 3000),
    logFile,
    command: previewArgs(py.cmd, full),
  };
}

/** 运行子进程，stdout/stderr 全部重定向到 logFile，返回值从文件读回。 */
function execFd(cmd, args, { cwd, timeoutMs = 120000, logFile, env } = {}) {
  return new Promise((done) => {
    const fd = logFile ? openLog(logFile) : null;
    const stdio = fd === null ? ['ignore', 'ignore', 'ignore'] : ['ignore', fd, fd];
    let childEnv = process.env;
    if (env && Object.keys(env).length) {
      childEnv = { ...process.env };
      for (const [k, v] of Object.entries(env)) childEnv[k] = String(v);
    }
    let child;
    try {
      child = spawn(cmd, args, { cwd, windowsHide: true, stdio, env: childEnv });
    } catch (e) {
      if (fd !== null) closeSync(fd);
      done({ code: -1, error: `spawn 失败 ${e.code || ''}: ${e.message}` });
      return;
    }
    if (fd !== null) closeSync(fd);

    let settled = false;
    let timedOut = false;
    const finish = (payload) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const log = logFile ? tailFile(logFile, 12000) : '';
      done({ ...payload, log });
    };
    const timer = setTimeout(() => {
      timedOut = true;
      killTree(child, child.pid);
      finish({ code: -1, error: `超时 ${timeoutMs}ms` });
    }, timeoutMs);

    child.on('error', (e) => finish({ code: -1, error: `spawn 失败 ${e.code || ''}: ${e.message}` }));
    child.on('exit', (code, signal) => {
      if (timedOut) return;
      finish({ code: code ?? -1, signal });
    });
  });
}

/** 找到可用的 Python，缓存第一个成功的调用方式（跨平台：Windows 走 python/py -3，macOS 走 python3）。 */
async function resolvePython(cfg) {
  if (!cfg._pyCache) cfg._pyCache = { value: null };
  if (cfg._pyCache.value) return cfg._pyCache.value;

  const { workRoot } = resolvePaths(cfg);
  let logDir = workRoot || process.cwd();
  try {
    mkdirSync(logDir, { recursive: true });
  } catch {
    logDir = process.cwd();
  }

  const res = await resolvePythonCmd(cfg.pythonPath, (cmd, args) =>
    execFd(cmd, args, { timeoutMs: 25000, logFile: join(logDir, '_pyprobe.log') }),
  );
  if (!res.ok) {
    const err = new Error(
      `找不到可用的 Python（尝试过 ${res.failures.join('；')}）。请在插件配置里设置 pythonPath。`,
    );
    err.pythonMissing = true;
    throw err;
  }
  cfg._pyCache.value = { cmd: res.cmd, pre: res.pre, version: res.version };
  return cfg._pyCache.value;
}

// ---------------------------------------------------------------- 工作目录

function workDirFor(cfg, mid, { create = true } = {}) {
  const { workRoot } = resolvePaths(cfg);
  const dir = workRoot ? join(workRoot, String(mid)) : '';
  if (create) {
    if (!dir) {
      throw new Error(
        '还没配置工作目录。请先在插件配置里设置 scriptDir（workRoot 会默认取它同级的 _bili_quark_work），或直接设置 workRoot。',
      );
    }
    mkdirSync(join(dir, 'downloads'), { recursive: true });
    mkdirSync(join(dir, '_tmp'), { recursive: true });
  }
  return dir;
}

/** 组装一次后端调用的公共参数。cookie 走环境变量（后端不支持 cookie 命令行参数）。 */
function backendBase(cfg, mid, work, withMid = true) {
  const args = ['--workdir', work];
  if (withMid) {
    args.push('--mid', String(mid), '--state-file', join(work, 'state.jsonl'), '--video-list', join(work, 'video_list.json'));
  }
  return args;
}

function cleanupJobs() {
  for (const [mid, job] of jobs) {
    if (!isAlive(job.pid)) jobs.delete(mid);
  }
}

function accountShape(acc) {
  if (!acc) return null;
  return {
    nickname: acc.nickname ?? null,
    member_type: acc.member_type ?? null,
    total_capacity_gb: acc.total_capacity ? Number((acc.total_capacity / 2 ** 30).toFixed(1)) : null,
    use_capacity_gb: acc.use_capacity ? Number((acc.use_capacity / 2 ** 30).toFixed(1)) : null,
  };
}

function pickItems(parsed) {
  const raw = parsed.items || parsed.pending_items || parsed.targets || [];
  return raw.map((x) => ({
    bvid: x.bvid,
    title: x.title || '',
    width: x.width ?? 0,
    height: x.height ?? 0,
    duration: x.duration ?? 0,
  }));
}

// ---------------------------------------------------------------- schema 辅助

const COMMON_PROPS = {
  mid: {
    type: 'string',
    description:
      'B 站 UP 主的 UID（数字），或主页链接，例如 3546918307236545 / https://space.bilibili.com/3546918307236545',
  },
  biliCookie: {
    type: 'string',
    description: 'B 站整行 cookie（含 SESSDATA、bili_jct）。不传则回退读后端目录的 bili_cookie.txt。',
  },
  quarkCookie: {
    type: 'string',
    description: '夸克网盘整行 cookie（必须含 __pus 和 __puus）。不传则回退读后端目录的 quark_cookie.txt。',
  },
};

function schema(props, required) {
  return { type: 'object', properties: props, required, additionalProperties: false };
}

// ---------------------------------------------------------------- apply

export function apply(ctx, config = {}) {
  const cfg = { ...CONFIG_DEFAULTS, ...(config || {}) };
  /** 已注册的工具定义表：面板的 HTTP 接口需要直接调用它们。 */
  const toolDefs = new Map();

  const define = (def) => {
    toolDefs.set(def.name, def);
    return ctx.effect(() => ctx.tools.register(def));
  };

  // ============================================================ fetch
  define({
    name: 'bili_quark_fetch',
    description:
      '抓取某 B 站 UP 主的全部投稿，并逐条调用 playurl 探测真实分辨率/时长，生成清单文件（含竖屏判定）。' +
      '只读接口、不下载。跑完可用 bili_quark_targets 看体积估算。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        biliCookie: COMMON_PROPS.biliCookie,
        includeHorizontal: { type: 'boolean', description: '是否也包含横屏视频（默认 false，只保留竖屏）' },
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          total: { type: 'number' },
          vertical: { type: 'number' },
          horizontal: { type: 'number' },
          unknown: { type: 'number' },
          workDir: { type: 'string' },
          videoList: { type: 'string' },
          command: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok', 'mid', 'workDir'],
      },
      render(_a, v) {
        if (!v.ok) return block(`抓取失败：${v.error}`);
        return block(
          [
            `UP 主 ${v.mid} 投稿抓取完成`,
            `  总投稿 ${v.total} 条 → 竖屏 ${v.vertical} / 横屏 ${v.horizontal} / 未知 ${v.unknown}`,
            `  清单：${v.videoList}`,
            '',
            '下一步：bili_quark_targets 看体积估算与磁盘检查。',
          ].join('\n'),
        );
      },
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效：需要数字 UID 或 space.bilibili.com 主页链接');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const listPath = join(work, 'video_list.json');
      const cliArgs = ['-m', 'bili_quark.cli', 'fetch', ...backendBase(cfg, mid, work)];
      if (args?.includeHorizontal) cliArgs.push('--include-horizontal');

      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
      });
      if (!r.ok) {
        const list = readJson(listPath);
        if (!list) {
          return {
            ok: false,
            mid,
            workDir: work,
            command: r.command,
            error: `${r.error}\n${r.log || ''}`.trim(),
          };
        }
      }
      const list = r.ok ? r.value : readJson(listPath);
      const items = list.items || [];
      return {
        ok: true,
        mid,
        total: items.length,
        vertical: items.filter((x) => x.vertical === true).length,
        horizontal: items.filter((x) => x.vertical === false).length,
        unknown: items.filter((x) => x.vertical === null || x.vertical === undefined).length,
        workDir: work,
        videoList: listPath,
        command: r.command,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: `抓取 B 站 UP 主投稿 ${a?.mid ?? ''}`.trim(),
      kind: 'fetch',
      rawInput: a,
    }),
  });

  // ============================================================ targets
  define({
    name: 'bili_quark_targets',
    description:
      '列出当前筛选条件下真正会被处理的条目（竖屏/横屏、排除名单、已完成条目），给出条数、总时长、' +
      '体积估算与磁盘剩余空间提醒。不下载不上传。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        includeHorizontal: { type: 'boolean', description: '包含横屏（默认 false）' },
        exclude: { type: 'array', items: { type: 'string' }, description: '要排除的 BV 号列表' },
        limit: { type: 'number', description: '只看前 N 条（可选）' },
        biliCookie: COMMON_PROPS.biliCookie,
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          pending: { type: 'number' },
          done: { type: 'number' },
          failed: { type: 'number' },
          totalSeconds: { type: 'number' },
          estGbLow: { type: 'number' },
          estGbHigh: { type: 'number' },
          diskFreeGb: { type: 'number' },
          items: { type: 'array', items: { type: 'object', additionalProperties: true } },
          workDir: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok', 'mid'],
      },
      render(_a, v) {
        if (!v.ok) return block(`读取清单失败：${v.error}`);
        const lines = [
          `UP 主 ${v.mid}：待处理 ${v.pending} 条，已完成 ${v.done} 条，失败 ${v.failed} 条`,
          `待处理总时长：${(v.totalSeconds / 60).toFixed(1)} 分钟`,
          `体积估算：${v.estGbLow} ～ ${v.estGbHigh} GB（竖屏 4K AVC 约 150～200 MB/分钟）`,
          `磁盘剩余：${v.diskFreeGb} GB`,
          '',
        ];
        for (const it of v.items || []) {
          lines.push(
            `  ${it.bvid}  ${it.width}x${it.height}  ${Math.round(it.duration || 0)}s  ${String(it.title).slice(0, 44)}`,
          );
        }
        if (v.diskFreeGb >= 0 && v.diskFreeGb < v.estGbHigh) {
          lines.push('', '⚠ 磁盘余量可能不足，建议先清理再跑。');
        }
        return block(lines.join('\n'));
      },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'run',
        ...backendBase(cfg, mid, work),
        '--dry-run',
        args?.includeHorizontal ? '--include-horizontal' : '--vertical-only',
      ];
      for (const bv of args?.exclude || []) cliArgs.push('--exclude', String(bv));
      if (args?.limit) cliArgs.push('--limit', String(args.limit));

      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        timeoutMs: 180000,
        env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
      });
      if (!r.ok) {
        return {
          ok: false,
          mid,
          workDir: work,
          error: `${r.error}\n${r.log || ''}`.trim(),
        };
      }
      const items = pickItems(r.value);
      const totalSeconds = items.reduce((s, x) => s + (x.duration || 0), 0);
      const minutes = totalSeconds / 60;

      // dry-run 不返回已完成的条数，用 status 补上（本地计算，不联网）
      let done = r.value.done ?? 0;
      let failed = r.value.failed ?? 0;
      let diskFreeGb = r.value.disk_free_gb ?? -1;
      const st = await runPythonJson(
        cfg,
        dir,
        ['-m', 'bili_quark.cli', 'status', ...backendBase(cfg, mid, work)],
        { work, timeoutMs: 60000, env: cookieEnv(cfg, args?.biliCookie, null) },
      );
      if (st.ok) {
        done = st.value.done ?? done;
        failed = st.value.failed ?? failed;
        diskFreeGb = st.value.disk_free_gb ?? diskFreeGb;
      }

      return {
        ok: true,
        mid,
        pending: r.value.todo ?? r.value.pending ?? items.length,
        done,
        failed,
        totalSeconds,
        estGbLow: Number(((minutes * 150) / 1024).toFixed(1)),
        estGbHigh: Number(((minutes * 200) / 1024).toFixed(1)),
        diskFreeGb,
        items,
        workDir: work,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: `预览待处理清单 ${a?.mid ?? ''}`.trim(),
      kind: 'other',
      rawInput: a,
    }),
  });

  // ============================================================ run
  define({
    name: 'bili_quark_run',
    description:
      '启动流水线：逐条 下载最高画质 → 合并单文件 mp4 → 上传夸克 → 校验 → 删本地（流式，下一条传一条删一条）。' +
      '后台运行并立即返回 jobId，用 bili_quark_status 查进度。可中断后原样重跑（进度写在 state.jsonl），' +
      '也可用 only 只重跑某条（传 BV 号）。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        quarkDir: {
          type: 'string',
          description:
            '夸克目标目录：fid（32 位十六进制）或路径（如 /B站竖屏-某某）。不传则用该 UP 主已保存的目录。',
        },
        quarkDirName: { type: 'string', description: '目录显示名（可选，仅记录用）' },
        concurrency: { type: 'number', description: '并发条数（同时处理几个视频，默认 5）' },
        deleteLocal: { type: 'boolean', description: '上传校验成功后删除本地副本（默认 true）' },
        includeHorizontal: { type: 'boolean', description: '包含横屏（默认 false，只要竖屏）' },
        exclude: { type: 'array', items: { type: 'string' }, description: '按 BV 号排除的视频列表' },
        only: { type: 'array', items: { type: 'string' }, description: '只跑这些 BV 号（单条重跑用）' },
        retryFailed: { type: 'boolean', description: '把之前失败的条目也纳入本次处理' },
        force: {
          type: 'boolean',
          description:
            '忽略本机已完成记录（state.jsonl），强制重跑本次选中的条目。' +
            '用于「本机以为传过、夸克目标目录里其实没有」的补传：先用 bili_quark_verify 核对，' +
            '再把缺失的 BV 号交给 only，并带上 force=true。',
        },
        maxHeight: {
          type: 'number',
          description:
            '分辨率上限：只下载 height ≤ 该值的最高一档（如 1080 / 720 / 480）。' +
            '不传就是最高画质（4K 竖屏）。降画质下载时文件名会带 [1080p] 之类的后缀，' +
            '最高画质保持老名字，不会和已上传的文件重名。',
        },
        page: {
          type: 'number',
          description:
            '默认下载第几集（多分集视频）。单条可用 only: ["BV1xxx:2"] 单独指定 —— ' +
            '不写就是第 1 集（P1 不加文件名后缀，P2 及以上会带 [P2]）。',
        },
        limit: { type: 'number', description: '本次最多处理 N 条（小批量试跑用）' },
        partConcurrency: { type: 'number', description: '单条视频上传时的分片并发数（默认 8）' },
        diskMinGb: { type: 'number', description: '磁盘剩余低于该值则拒绝启动（默认取插件配置）' },
        dryRun: { type: 'boolean', description: '只演练：筛选+磁盘检查+打印清单，不下载不上传' },
        biliCookie: COMMON_PROPS.biliCookie,
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          jobId: { type: 'string' },
          pid: { type: 'number' },
          mid: { type: 'string' },
          workDir: { type: 'string' },
          logFile: { type: 'string' },
          command: { type: 'string' },
          draft: { type: 'boolean' },
          pending: { type: 'number' },
          diskFreeGb: { type: 'number' },
          targets: { type: 'array', items: { type: 'object', additionalProperties: true } },
          note: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok', 'mid'],
      },
      render(_a, v) {
        if (!v.ok) return block(`启动失败：${v.error}`);
        if (v.draft) {
          const lines = [`演练（未下载）：将处理 ${v.pending} 条，磁盘剩余 ${v.diskFreeGb} GB`, ''];
          for (const it of v.targets || []) {
            lines.push(`  ${it.bvid}  ${it.width}x${it.height}  ${String(it.title).slice(0, 44)}`);
          }
          lines.push('', '确认无误后把 dryRun 设为 false 再次调用即可真正开始。');
          return block(lines.join('\n'));
        }
        return block(
          [
            '流水线已后台启动',
            `  jobId      ${v.jobId}`,
            `  PID        ${v.pid}`,
            `  工作目录   ${v.workDir}`,
            `  日志       ${v.logFile}`,
            `  待处理     ${v.pending} 条`,
            '',
            `命令：${v.command}`,
            `用 bili_quark_status（mid=${v.mid}）查进度。`,
          ].join('\n'),
        );
      },
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效：需要数字 UID 或 space.bilibili.com 主页链接');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const dryRun = args?.dryRun === true;

      cleanupJobs();
      const existing = jobs.get(mid);
      if (existing && isAlive(existing.pid)) {
        return {
          ok: false,
          mid,
          workDir: work,
          jobId: existing.id,
          pid: existing.pid,
          error: `该 UP 主已有作业在运行（jobId=${existing.id}, PID=${existing.pid}）。先用 bili_quark_status 查看。`,
        };
      }

      const saved = readJson(join(work, 'config.json'));
      if (!dryRun && !args?.quarkDir && !(saved && saved.quark_dir_fid)) {
        return {
          ok: false,
          mid,
          workDir: work,
          error: '还没设定夸克目标目录。请先用 bili_quark_resolve 解析目标目录，或给 quarkDir 传 fid/路径。',
        };
      }

      const dirArg = splitDirArg(args?.quarkDir);
      // 后端 run 只接受 fid；路径要先用 resolve-dir 换成 fid（顺带落盘 config.json）
      const resolved = await resolveDirToFid(cfg, dir, work, mid, dirArg, args);
      if (!resolved.ok) {
        return { ok: false, mid, workDir: work, error: resolved.error };
      }
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'run',
        ...backendBase(cfg, mid, work),
        '--concurrency',
        String(args?.concurrency ?? 5),
        args?.deleteLocal === false ? '--keep-local' : '--delete-local',
        args?.includeHorizontal ? '--include-horizontal' : '--vertical-only',
        '--disk-min-gb',
        String(args?.diskMinGb ?? cfg.diskMinGb),
      ];
      if (resolved.fid) cliArgs.push('--quark-dir', resolved.fid);
      if (args?.quarkDirName) cliArgs.push('--quark-dir-name', String(args.quarkDirName));
      // 显式勾选的条目要能被处理，所以有 only 时清空后端的默认排除名单
      // （否则某些条目勾了也不会跑）；没有 only（跑整个待处理队列）时保留默认排除。
      const onlyList = (args?.only || []).map(String).filter(Boolean);
      if (args?.exclude?.length) {
        for (const bv of args.exclude) cliArgs.push('--exclude', String(bv));
      } else if (onlyList.length) {
        cliArgs.push('--exclude', '');
      }
      for (const bv of onlyList) cliArgs.push('--only', String(bv));
      if (args?.retryFailed) cliArgs.push('--retry-failed');
      // 补传远端缺失：state 说传过也要再传一次
      if (args?.force) cliArgs.push('--force');
      // 分辨率上限与默认分集
      if (args?.maxHeight) cliArgs.push('--max-height', String(args.maxHeight));
      if (args?.page) cliArgs.push('--page', String(args.page));
      if (args?.limit) cliArgs.push('--limit', String(args.limit));
      if (args?.partConcurrency) cliArgs.push('--part-concurrency', String(args.partConcurrency));
      if (dryRun) cliArgs.push('--dry-run');

      // ---- 演练：同步拿清单，不产生大流量
      if (dryRun) {
        const r = await runPythonJson(cfg, dir, cliArgs, {
          work,
          timeoutMs: 180000,
          env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
        });
        if (!r.ok) {
          return { ok: false, mid, workDir: work, error: `${r.error}\n${r.log || ''}`.trim() };
        }
        const items = pickItems(r.value);
        return {
          ok: true,
          draft: true,
          mid,
          workDir: work,
          pending: r.value.todo ?? r.value.pending ?? items.length,
          diskFreeGb: r.value.disk_free_gb ?? -1,
          targets: items,
        };
      }

      // ---- 真跑：detached 后台作业
      const logFile = join(work, 'run.log');
      const progressFile = join(work, 'progress.json');
      const jobId = `bq-${mid}-${Date.now().toString(36)}`;
      const py = await resolvePython(cfg);

      rmQuiet(progressFile);
      writeJsonAtomic(progressFile, {
        state: 'starting',
        mid,
        total: 0,
        done: 0,
        ok: 0,
        fail: 0,
        current: [],
        started: new Date().toISOString(),
        updated: new Date().toISOString(),
        pid: 0,
        last_error: null,
      });

      const fd = openLog(logFile);
      if (fd === null) {
        return { ok: false, mid, workDir: work, error: `打不开日志文件：${logFile}` };
      }
      const credEnv = cookieEnv(cfg, args?.biliCookie, args?.quarkCookie);
      let child;
      try {
        child = spawn(
          py.cmd,
          [...py.pre, ...cliArgs],
          {
            cwd: dir,
            detached: true,
            windowsHide: true,
            stdio: ['ignore', fd, fd],
            env: workerEnv({
              backendDir: dir,
              ffmpegPath: resolveUserPath(cfg.ffmpegPath || ''),
              ffprobePath: resolveUserPath(cfg.ffprobePath || ''),
              extra: credEnv,
            }),
          },
        );
      } catch (e) {
        closeSync(fd);
        return { ok: false, mid, workDir: work, error: `启动子进程失败：${e.message}` };
      }
      closeSync(fd);

      const started = {
        id: jobId,
        pid: child.pid,
        mid,
        work,
        logFile,
        command: previewArgs(py.cmd, [...py.pre, ...cliArgs]),
        startedAt: new Date().toISOString(),
      };
      let earlyError = null;
      let exited = false;
      child.on('error', (e) => {
        earlyError = `${e.code || 'ERR'}: ${e.message}`;
      });
      child.on('exit', (code, signal) => {
        exited = true;
        started.exit = { code, signal };
      });
      child.unref();

      // 确认不是「一启动就挂」
      await new Promise((r) => setTimeout(r, cfg.startGraceMs));
      if (earlyError || (exited && started.exit && started.exit.code !== 0)) {
        const tail = tailFile(logFile, 2500);
        return {
          ok: false,
          mid,
          workDir: work,
          logFile,
          command: started.command,
          error:
            `子进程启动后立即退出（${earlyError || `exit ${started.exit?.code}`}）。\n` +
            `日志尾部：\n${tail || '(空)'}`,
        };
      }

      jobs.set(mid, started);

      try {
        writeJsonAtomic(join(work, 'session.json'), {
          jobId,
          pid: child.pid,
          mid,
          logFile,
          progressFile,
          args: cliArgs,
          startedAt: started.startedAt,
        });
      } catch {
        /* 非致命 */
      }

      return {
        ok: true,
        draft: false,
        jobId,
        pid: child.pid,
        mid,
        workDir: work,
        logFile,
        command: started.command,
        note: '后台运行中。用 bili_quark_status 查进度，全部完成后用 bili_quark_verify 做远端核对。',
      };
    },
    presentCall: (a) => ({
      card: 'terminal',
      title: `启动 B站→夸克 流水线 ${a?.mid ?? ''}`.trim(),
      description: a?.dryRun ? '演练（不下载）' : `并发 ${a?.concurrency ?? 5}`,
      kind: 'execute',
    }),
  });

  // ============================================================ status
  define({
    name: 'bili_quark_status',
    description:
      '查看流水线进度：已完成/成功/失败、正在处理哪条到哪一步、后台进程是否还活着，并给出日志尾部。' +
      '也可用 mode=quark-info 查看夸克账号容量与根目录文件夹。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        jobId: { type: 'string', description: 'bili_quark_run 返回的 jobId（可选，用于确认是同一个作业）' },
        tailLines: { type: 'number', description: '日志尾部行数（默认 40，最多 200）' },
        quarkCookie: COMMON_PROPS.quarkCookie,
        mode: {
          type: 'string',
          enum: ['progress', 'quark-info'],
          description: 'progress（默认）看流水线进度；quark-info 看夸克账号与根目录',
        },
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          state: { type: 'string' },
          alive: { type: 'boolean' },
          pid: { type: 'number' },
          total: { type: 'number' },
          done: { type: 'number' },
          okCount: { type: 'number' },
          failCount: { type: 'number' },
          percent: { type: 'number' },
          current: { type: 'array', items: { type: 'object', additionalProperties: true } },
          updated: { type: 'string' },
          logTail: { type: 'string' },
          workDir: { type: 'string' },
          account: { type: 'object', additionalProperties: true },
          rootDirs: { type: 'array', items: { type: 'object', additionalProperties: true } },
          error: { type: 'string' },
        },
        required: ['ok', 'mid'],
      },
      render(_a, v) {
        if (!v.ok) return block(`查询失败：${v.error}`);
        if (v.mode === 'quark-info') {
          const lines = [];
          if (v.account) {
            lines.push(
              `夸克账号 ${v.account.nickname ?? '?'}  容量 ${v.account.total_capacity_gb ?? '?'} GB / 已用 ${v.account.use_capacity_gb ?? '?'} GB`,
            );
          }
          lines.push('根目录文件夹：');
          for (const d of v.rootDirs || []) lines.push(`  ${d.name}   fid=${d.fid}`);
          return block(lines.join('\n'));
        }
        const lines = [
          `进度：${v.done}/${v.total}（${v.percent}%）  成功 ${v.okCount}  失败 ${v.failCount}`,
          `状态：${v.state}${v.alive ? `（进程 ${v.pid} 存活）` : '（进程已结束）'}`,
          v.updated ? `更新：${v.updated}` : '',
        ];
        if (v.current?.length) {
          lines.push('', '正在处理：');
          for (const c of v.current) {
            lines.push(
              `  ${c.bvid} ${c.stage || ''} ${c.percent != null ? `${c.percent}%` : ''} ${String(c.title || '').slice(0, 40)}`,
            );
          }
        }
        if (v.error) lines.push('', `注意：${v.error}`);
        if (v.logTail) lines.push('', '日志尾部：', v.logTail);
        return block(lines.filter(Boolean).join('\n'));
      },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效');
      const work = workDirFor(cfg, mid);

      if (args?.mode === 'quark-info') {
        const dir = assertBackend(cfg);
        // 账号与根目录其实是 resolve-dir 的输出（status 不返回这些字段），
        // 用 fid=0（根目录）查一次即可，不需要 mid。
        // 这是只读查询：用一个临时工作目录，免得在用户目录里留下文件。
        const scratch = mkdtempSync(join(tmpdir(), 'bq-info-'));
        try {
          const r = await runPythonJson(
            cfg,
            dir,
            ['-m', 'bili_quark.cli', 'resolve-dir', '--workdir', scratch, '--fid', '0'],
            { work: scratch, timeoutMs: 120000, env: cookieEnv(cfg, null, args?.quarkCookie) },
          );
          if (!r.ok) {
            return { ok: false, mid, workDir: work, error: `${r.error}\n${r.log || ''}`.trim() };
          }
          return {
            ok: true,
            mid,
            mode: 'quark-info',
            workDir: work,
            account: accountShape(r.value.account),
            rootDirs: r.value.root_dirs || [],
          };
        } finally {
          try {
            rmSync(scratch, { recursive: true, force: true });
          } catch {
            /* 忽略 */
          }
        }
      }

      const progress = readJson(join(work, 'progress.json'));
      const session = readJson(join(work, 'session.json'));
      const job = jobs.get(mid);
      const pid = progress?.pid || session?.pid || job?.pid || 0;
      const alive = isAlive(pid);

      if (!progress) {
        return {
          ok: true,
          mid,
          state: 'idle',
          alive: false,
          pid,
          total: 0,
          done: 0,
          okCount: 0,
          failCount: 0,
          percent: 0,
          current: [],
          updated: '',
          workDir: work,
          logTail: tailText(cfg, work, args?.tailLines),
          error: job || session
            ? `有作业记录但进度文件缺失（jobId=${job?.id || session?.jobId}），可能启动后立即退出了。`
            : '该 UP 主还没有运行记录（先跑 bili_quark_fetch 与 bili_quark_run）。',
        };
      }

      const total = progress.total ?? 0;
      const done = progress.done ?? 0;
      let state = progress.state || 'running';
      if (state === 'running' && !alive) state = 'stalled';
      if (state === 'starting' && !alive) state = 'failed_start';

      return {
        ok: true,
        mid,
        state,
        alive,
        pid,
        total,
        done,
        okCount: progress.ok ?? 0,
        failCount: progress.fail ?? 0,
        percent: total ? Math.round((done / total) * 100) : 0,
        current: progress.current || [],
        updated: progress.updated || '',
        workDir: work,
        logTail: tailText(cfg, work, args?.tailLines),
        error: progress.last_error || undefined,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: a?.mode === 'quark-info' ? '夸克账号与目录' : `流水线进度 ${a?.mid ?? ''}`.trim(),
      kind: 'other',
      rawInput: a,
    }),
  });

  // ============================================================ verify
  define({
    name: 'bili_quark_verify',
    description:
      '以夸克远端实际文件列表为准做最终核对：逐条比对文件名 + 精确字节数，报告目标条数、已确认条数、' +
      '缺失、字节数不一致、远端总量(GB)、有无多余文件、本地有无残留。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        includeHorizontal: { type: 'boolean', description: '核对范围需与 run 一致（默认 false）' },
        exclude: { type: 'array', items: { type: 'string' }, description: '与 run 一致的排除名单' },
        quarkDir: {
          type: 'string',
          description: '夸克目标目录：fid 或路径（如 /B站上传/某某）。留空则用该 UP 主已保存的目录。',
        },
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          target: { type: 'number' },
          confirmed: { type: 'number' },
          missing: { type: 'number' },
          sizeMismatch: { type: 'number' },
          remoteTotalGb: { type: 'number' },
          extraFiles: { type: 'array', items: { type: 'object', additionalProperties: true } },
          localResidue: { type: 'array', items: { type: 'object', additionalProperties: true } },
          missingItems: { type: 'array', items: { type: 'string' } },
          mismatchItems: { type: 'array', items: { type: 'object', additionalProperties: true } },
          workDir: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok', 'mid'],
      },
      render(_a, v) {
        if (!v.ok) return block(`核对失败：${v.error}`);
        const lines = [
          '最终核对（以夸克远端实际列表为准）',
          `  目标条数      ${v.target}`,
          `  远端已确认    ${v.confirmed}`,
          `  远端缺失      ${v.missing}`,
          `  字节数不一致  ${v.sizeMismatch}`,
          `  远端合计      ${v.remoteTotalGb} GB`,
          `  远端多余文件  ${(v.extraFiles || []).length}`,
          `  本地残留      ${(v.localResidue || []).length}`,
        ];
        for (const n of v.missingItems || []) lines.push(`    缺失: ${n}`);
        for (const m of v.mismatchItems || []) {
          lines.push(`    大小不符: ${m.bvid} 本地=${m.expected} 远端=${m.remote}`);
        }
        for (const f of v.extraFiles || []) lines.push(`    多余: ${f.name}`);
        for (const f of v.localResidue || []) lines.push(`    残留: ${f.name}`);
        const clean = v.missing === 0 && v.sizeMismatch === 0;
        lines.push('', clean ? '✓ 全部核对一致' : '✗ 存在不一致，见上');
        return block(lines.join('\n'));
      },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'verify',
        ...backendBase(cfg, mid, work),
        args?.includeHorizontal ? '--include-horizontal' : '--vertical-only',
      ];
      for (const bv of args?.exclude || []) cliArgs.push('--exclude', String(bv));

      // verify 需要目录 fid：显式给了就先解析（顺带写进 config.json），
      // 没给则交给后端读 <workdir>/config.json（由 bili_quark_resolve 写入）。
      const dirArg = splitDirArg(args?.quarkDir);
      if (dirArg.path || dirArg.fid) {
        const resolved = await resolveDirToFid(cfg, dir, work, mid, dirArg, args);
        if (!resolved.ok) return { ok: false, mid, workDir: work, error: resolved.error };
        if (resolved.fid) cliArgs.push('--quark-dir', resolved.fid);
      } else {
        const saved = readJson(join(work, 'config.json'));
        if (!(saved && saved.quark_dir_fid)) {
          return {
            ok: false,
            mid,
            workDir: work,
            error:
              '还没为该 UP 主保存夸克目标目录。先跑一次 bili_quark_resolve（给 path 或 fid），' +
              '或给本次调用直接传 quarkDir。',
          };
        }
      }

      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
      });
      if (!r.ok) {
        return { ok: false, mid, workDir: work, error: `${r.error}\n${r.log || ''}`.trim() };
      }
      const p = r.value;
      return {
        ok: true,
        mid,
        target: p.target ?? 0,
        confirmed: p.confirmed ?? 0,
        missing: p.missing ?? 0,
        sizeMismatch: p.size_mismatch ?? 0,
        remoteTotalGb: Number(((p.remote_total_bytes ?? 0) / 2 ** 30).toFixed(2)),
        extraFiles: p.extra_files || [],
        localResidue: p.local_residue || [],
        missingItems: p.missing_items || [],
        mismatchItems: p.mismatch_items || [],
        workDir: work,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: `最终核对 ${a?.mid ?? ''}`.trim(),
      kind: 'other',
      rawInput: a,
    }),
  });

  // ============================================================ check（B站源 vs 网盘）
  define({
    name: 'bili_quark_check',
    description:
      '拿 B 站源流的真实字节数（只发 Range 请求，不下载内容），逐个核对夸克网盘里的文件大小是否一致。' +
      '和 bili_quark_verify 的区别：verify 只比「远端 vs 本机 state 记录」，本机没记录就无从判断；' +
      '这个直接问 B 站 CDN，所以换了机器、state 丢了、文件是别人传的，照样能判断网盘里那份是不是这个视频、这个分辨率。' +
      '合并成 mp4 比两路流之和多约 0.04% 的封装开销，默认给 1% 容差（tolerance 可调）。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        quarkDir: {
          type: 'string',
          description: '夸克目标目录：fid 或路径。不传则用该 UP 主已保存的目录。',
        },
        only: {
          type: 'array',
          items: { type: 'string' },
          description: '只核对这些 BV 号（支持 BV号:分集序号，如 BV1xxx:2）',
        },
        page: { type: 'number', description: '默认核对第几集（多分集视频）' },
        maxHeight: {
          type: 'number',
          description: '按这个分辨率上限算 B 站侧的期望大小（默认最高画质）',
        },
        tolerance: { type: 'number', description: '允许的相对误差，默认 0.01（即 1%）' },
        includeHorizontal: { type: 'boolean', description: '包含横屏（默认 false，只要竖屏）' },
        exclude: { type: 'array', items: { type: 'string' }, description: '按 BV 号排除的视频列表' },
        biliCookie: COMMON_PROPS.biliCookie,
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          checked: { type: 'number' },
          okCount: { type: 'number' },
          sizeMismatch: { type: 'number' },
          missing: { type: 'number' },
          failed: { type: 'number' },
          tolerancePercent: { type: 'number' },
          items: { type: 'array', items: { type: 'object', additionalProperties: true } },
          workDir: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok'],
      },
      render(_a, v) {
        if (!v.ok) return block(`核对失败：${v.error}`);
        const lines = [
          `与 B 站源核对：共 ${v.checked} 条（容差 ${v.tolerancePercent}%${v.maxHeight ? `，限 ${v.maxHeight}p` : ''}）`,
          `  一致     ${v.okCount}`,
          `  大小不符 ${v.sizeMismatch}`,
          `  网盘缺失 ${v.missing}`,
          `  取流失败 ${v.failed}`,
        ];
        const bad = (v.items || []).filter((x) => x && x.ok === false).slice(0, 12);
        if (bad.length) {
          lines.push('');
          for (const x of bad) {
            if (x.status === 'missing') lines.push(`  ${x.bvid}  网盘里没有`);
            else if (x.status === 'error') lines.push(`  ${x.bvid}  取流失败：${x.error}`);
            else {
              lines.push(
                `  ${x.bvid}  网盘 ${(x.remote_bytes / 2 ** 20).toFixed(1)}MB / ` +
                  `B站 ${(x.expect_bytes / 2 ** 20).toFixed(1)}MB（差 ${x.diff_percent}%）`,
              );
            }
          }
        }
        return block(lines.join('\n'));
      },
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'check-src',
        ...backendBase(cfg, mid, work),
        args?.includeHorizontal ? '--include-horizontal' : '--vertical-only',
      ];
      if (args?.tolerance != null) cliArgs.push('--tolerance', String(args.tolerance));
      if (args?.maxHeight) cliArgs.push('--max-height', String(args.maxHeight));
      if (args?.page) cliArgs.push('--page', String(args.page));
      for (const bv of args?.only || []) cliArgs.push('--only', String(bv));
      for (const bv of args?.exclude || []) cliArgs.push('--exclude', String(bv));

      const dirArg = splitDirArg(args?.quarkDir);
      if (dirArg.path || dirArg.fid) {
        const resolved = await resolveDirToFid(cfg, dir, work, mid, dirArg, args);
        if (!resolved.ok) return { ok: false, mid, workDir: work, error: resolved.error };
        if (resolved.fid) cliArgs.push('--quark-dir', resolved.fid);
      } else {
        const saved = readJson(join(work, 'config.json'));
        if (!(saved && saved.quark_dir_fid)) {
          return {
            ok: false,
            mid,
            workDir: work,
            error:
              '还没为该 UP 主保存夸克目标目录。先跑一次 bili_quark_resolve（给 path 或 fid），' +
              '或给本次调用直接传 quarkDir。',
          };
        }
      }

      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
      });
      if (!r.ok) {
        return { ok: false, mid, workDir: work, error: `${r.error}\n${r.log || ''}`.trim() };
      }
      const p = r.value;
      return {
        ok: true,
        mid,
        checked: p.checked ?? 0,
        okCount: p.ok ?? 0,
        sizeMismatch: p.size_mismatch ?? 0,
        missing: p.missing ?? 0,
        failed: p.error ?? 0,
        tolerancePercent: p.tolerance_percent ?? 1,
        maxHeight: p.max_height ?? null,
        items: p.items || [],
        workDir: work,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: `与 B 站源核对 ${a?.mid ?? ''}`.trim(),
      kind: 'other',
      rawInput: a,
    }),
  });

  // ============================================================ auth（凭据有效性）
  define({
    name: 'bili_quark_auth',
    description:
      '检查 B 站 / 夸克凭据是否还有效，并给出能拿到的过期时间：B 站用 nav 接口看登录状态' +
      '（还能拿到 cookie 里的 bili_ticket_expires 真实过期时间）；夸克用 member 接口看能不能调通，' +
      '顺带回容量与已用空间。夸克 cookie 本身不带过期时间，只能给「保存时间」。',
    parameters: schema(
      {
        biliCookie: COMMON_PROPS.biliCookie,
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      [],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          bili: { type: 'object', additionalProperties: true },
          quark: { type: 'object', additionalProperties: true },
          checkedAt: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok'],
      },
      render(_a, v) {
        if (!v.ok) return block(`检查失败：${v.error}`);
        const b = v.bili || {};
        const q = v.quark || {};
        const lines = [`凭据检查（${v.checkedAt || ''}）`];
        lines.push(
          b.ok
            ? `  B 站   有效   账号 ${b.uname || '?'}（mid ${b.mid || '?'}）` +
                (b.expires_at ? `，有效至 ${b.expires_at}` : '')
            : `  B 站   失效   ${b.error || ''}`,
        );
        lines.push(
          q.ok
            ? `  夸克   有效   ${q.member_type || ''} 容量 ${q.capacity_gb || 0}GB / 已用 ${q.used_gb || 0}GB`
            : `  夸克   失效   ${q.error || ''}`,
        );
        return block(lines.join('\n'));
      },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const dir = assertBackend(cfg);
      const work = resolvePaths(cfg).workRoot || dir;
      const cliArgs = ['-m', 'bili_quark.cli', 'check-cred'];
      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        timeoutMs: 120000,
        env: cookieEnv(cfg, args?.biliCookie, args?.quarkCookie),
      });
      if (!r.ok) return { ok: false, error: `${r.error}\n${r.log || ''}`.trim() };
      const p = r.value;
      return {
        ok: true,
        bili: p.bili || {},
        quark: p.quark || {},
        checkedAt: p.checked_at || '',
      };
    },
    presentCall: () => ({ card: 'generic', title: '凭据有效性检查', kind: 'other' }),
  });

  // ============================================================ resolve dir
  define({
    name: 'bili_quark_resolve',
    description:
      '解析夸克网盘目标目录：可用 fid，或用路径（如 /B站竖屏-某某，多级用 / 分隔）。路径不存在时按需创建，' +
      '并保存为该 UP 主的默认目标目录。同时返回账号信息与根目录文件夹供确认。',
    parameters: schema(
      {
        mid: COMMON_PROPS.mid,
        path: { type: 'string', description: '夸克目录路径，如 /B站竖屏-小圆脸' },
        fid: { type: 'string', description: '直接给目录 fid（32 位十六进制）' },
        quarkDirName: { type: 'string', description: '目录显示名（可选，仅记录用）' },
        create: { type: 'boolean', description: '路径不存在时创建（默认 true）' },
        quarkCookie: COMMON_PROPS.quarkCookie,
      },
      ['mid'],
    ),
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mid: { type: 'string' },
          fid: { type: 'string' },
          name: { type: 'string' },
          path: { type: 'string' },
          created: { type: 'array', items: { type: 'string' } },
          account: { type: 'object', additionalProperties: true },
          rootDirs: { type: 'array', items: { type: 'object', additionalProperties: true } },
          saved: { type: 'boolean' },
          saveWarning: { type: 'string' },
          workDir: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['ok', 'mid'],
      },
      render(_a, v) {
        if (!v.ok) return block(`解析失败：${v.error}`);
        const lines = [`目标目录已就绪：${v.path || v.name}`, `  fid   ${v.fid}`];
        if (v.created?.length) lines.push(`  新建  ${v.created.join(' / ')}`);
        if (v.saved) lines.push('  已保存为该 UP 主的默认目录（后续 run / verify 可省略目录参数）');
        else if (v.saveWarning) lines.push(`  ⚠ ${v.saveWarning}`);
        if (v.account) {
          lines.push(
            `  账号  ${v.account.nickname ?? '?'}（容量 ${v.account.total_capacity_gb ?? '?'} GB / 已用 ${v.account.use_capacity_gb ?? '?'} GB）`,
          );
        }
        if (v.rootDirs?.length) {
          lines.push('', '根目录文件夹（也可改用 fid 指定）：');
          for (const d of v.rootDirs) lines.push(`  ${d.name}   fid=${d.fid}`);
        }
        return block(lines.join('\n'));
      },
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const mid = parseMid(args?.mid);
      if (!mid) throw new Error('mid 无效');
      if (!args?.path && !args?.fid) throw new Error('需要给 path 或 fid');
      const dir = assertBackend(cfg);
      const work = workDirFor(cfg, mid);
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'resolve-dir',
        ...backendBase(cfg, mid, work, false),
      ];
      if (args?.fid) cliArgs.push('--fid', String(args.fid));
      if (args?.path) cliArgs.push('--path', String(args.path));
      if (args?.create === false) cliArgs.push('--no-create');

      const r = await runPythonJson(cfg, dir, cliArgs, {
        work,
        env: cookieEnv(cfg, null, args?.quarkCookie),
      });
      if (!r.ok) {
        return { ok: false, mid, workDir: work, error: `${r.error}\n${r.log || ''}`.trim() };
      }
      const p = r.value;
      const fid = p.fid || args?.fid || '';
      // 后端 resolve-dir 只解析、不落盘；写 <workdir>/config.json 的是 set-dir。
      // 这里补上，兑现「保存为该 UP 主默认目标目录」的承诺，后续 run/verify 就能省略目录参数。
      let saved = false;
      if (fid) {
        // 优先存完整路径（面板的输入框下次就能直接预填可用路径）
        const fullPath = p.path || args?.path || '';
        saved = await saveDir(
          cfg,
          dir,
          work,
          mid,
          fid,
          args?.quarkDirName || fullPath || p.name || '',
          args?.quarkCookie,
        );
      }
      return {
        ok: true,
        mid,
        fid,
        name: p.name || '',
        path: p.path || args?.path || '',
        created: p.created || [],
        account: accountShape(p.account),
        rootDirs: p.root_dirs || [],
        saved,
        saveWarning: saved
          ? ''
          : '已解析出 fid，但写入 config.json 失败；后续 run/verify 需要显式传目录参数。',
        workDir: work,
      };
    },
    presentCall: (a) => ({
      card: 'generic',
      title: `解析夸克目录 ${a?.path || a?.fid || ''}`.trim(),
      kind: 'other',
      rawInput: a,
    }),
  });

  // ---------------------------------------------------------------- 局部函数

  function tailText(c, work, tailLines) {
    const n = Math.min(Math.max(Number(tailLines) || 40, 1), 200);
    const raw = tailFile(join(work, 'run.log'), 60000);
    if (!raw) return '';
    return clampTail(raw.split(/\r?\n/).slice(-n).join('\n'), c.maxLogTail);
  }

  // 面板的数据接口：客户端轮询它拿进度（见 api.js）。
  // webServer 不存在时（例如非 Web 前端）这里安静地不注册，插件其余功能照常。
  try {
    registerPanelRoutes(ctx, {
      cfg,
      resolvePaths,
      isAlive,
      killTree,
      tools: toolDefs,
    });
  } catch (e) {
    ctx.logger?.warn?.('bili-quark: 面板接口注册失败：%s', e && e.message);
  }

  /**
   * 把「fid 或路径」统一解析成可用 fid。
   * 后端 run 只接受 fid，所以路径要先走 resolve-dir，并顺手写进该 UP 主的 config.json，
   * 之后同一 UP 主再跑就可以省略 quarkDir。
   */
  async function resolveDirToFid(c, dir, work, mid, dirArg, args) {
    const cookie = args?.quarkCookie;
    if (dirArg.path) {
      const cliArgs = [
        '-m',
        'bili_quark.cli',
        'resolve-dir',
        ...backendBase(c, mid, work, false),
        '--path',
        dirArg.path,
      ];
      const r = await runPythonJson(c, dir, cliArgs, {
        work,
        timeoutMs: 180000,
        env: cookieEnv(c, null, cookie),
      });
      if (!r.ok) {
        return { ok: false, error: `解析夸克目录失败：${r.error}\n${r.log || ''}`.trim() };
      }
      const fid = r.value.fid;
      if (!fid) return { ok: false, error: `解析夸克目录未返回 fid（路径 ${dirArg.path}）` };
      await saveDir(c, dir, work, mid, fid, args?.quarkDirName || r.value.name || dirArg.path, cookie);
      return { ok: true, fid };
    }
    if (dirArg.fid) {
      // 显式给了 fid：记下来，便于下次省略
      await saveDir(c, dir, work, mid, dirArg.fid, args?.quarkDirName || '', cookie);
      return { ok: true, fid: dirArg.fid };
    }
    return { ok: true, fid: null };
  }

  /** 把目标目录写进 <work>/config.json，使后续 run 可以省略目录参数。 */
  async function saveDir(c, dir, work, mid, fid, name, quarkCookie) {
    const cliArgs = [
      '-m',
      'bili_quark.cli',
      'set-dir',
      ...backendBase(c, mid, work, false),
      '--fid',
      String(fid),
      '--name',
      String(name || ''),
    ];
    const res = await runPythonJson(c, dir, cliArgs, {
      work,
      timeoutMs: 60000,
      env: cookieEnv(c, null, quarkCookie),
    }).catch((e) => ({ ok: false, error: `抛出 ${e && e.message}` }));
    // 写 config 失败不该让「解析目录」这个动作整体失败，但必须留下痕迹
    if (!res || !res.ok) {
      try {
        writeFileSync(
          join(work, '_savedir_error.log'),
          `${new Date().toISOString()} 保存目标目录失败：${(res && res.error) || '未知'}\n` +
            `命令：${(res && res.command) || cliArgs.join(' ')}\n` +
            `日志：${((res && res.log) || '').slice(0, 1500)}\n`,
          'utf8',
        );
      } catch {
        /* 忽略 */
      }
    } else {
      rmQuiet(join(work, '_savedir_error.log'));
    }
    return Boolean(res && res.ok);
  }
}

/**
 * Config 必须是 standard-schema 对象（cordis 调 `Config['~standard'].validate`）。
 * 用 DSH 自带的 schemastery（见 platform.js，跨平台定位）；拿不到时退化为等价的自实现。
 */

/** schemastery 拿不到时的等价最小实现（足以让 cordis 校验/补默认值）。 */
function fallbackSchema(desc) {
  const applyDefaults = (v) => {
    const out = { ...v };
    for (const [k, d] of Object.entries(desc)) {
      if (out[k] === undefined || out[k] === null) out[k] = d.default;
    }
    return out;
  };
  return {
    '~standard': {
      version: 1,
      vendor: 'bili-quark-pipeline',
      validate(value) {
        const v = value && typeof value === 'object' ? value : {};
        for (const [k, d] of Object.entries(desc)) {
          const x = v[k];
          if (x === undefined || x === null) continue;
          if (d.type === 'string' && typeof x !== 'string') {
            return { issues: [{ message: `${k} 需要字符串`, path: [k] }] };
          }
          if (d.type === 'number' && typeof x !== 'number') {
            return { issues: [{ message: `${k} 需要数字`, path: [k] }] };
          }
        }
        return { value: applyDefaults(v) };
      },
    },
  };
}

function buildConfigSchema(z) {
  const spec = {
    scriptDir: {
      type: 'string',
      default: CONFIG_DEFAULTS.scriptDir,
      description: 'Python 后端目录（含 bili_quark 包与 cookie 文件）。每台机器按自己的位置填。',
    },
    workRoot: {
      type: 'string',
      default: CONFIG_DEFAULTS.workRoot,
      description: '工作根目录：每个 UP 主在其下按 UID 分子目录存放 state.jsonl / downloads / _tmp。留空则取 scriptDir 同级的 _bili_quark_work。',
    },
    pythonPath: {
      type: 'string',
      default: '',
      description: 'Python 可执行文件路径。留空则依次尝试 python3 / python（Windows 还会试 py -3）。',
    },
    ffmpegPath: {
      type: 'string',
      default: '',
      description: 'ffmpeg 可执行文件路径。留空则由后端自动查找（PATH 或常见安装位置）。',
    },
    ffprobePath: {
      type: 'string',
      default: '',
      description: 'ffprobe 可执行文件路径。留空则由后端自动查找。',
    },
    diskMinGb: {
      type: 'number',
      default: CONFIG_DEFAULTS.diskMinGb,
      description: '磁盘剩余低于该值(GB)时拒绝启动流水线。',
    },
    startGraceMs: {
      type: 'number',
      default: CONFIG_DEFAULTS.startGraceMs,
      description: '启动后台作业后等待多少毫秒，以确认子进程没有立即崩溃。',
    },
    backendTimeoutMs: {
      type: 'number',
      default: CONFIG_DEFAULTS.backendTimeoutMs,
      description: '同步类后端调用（fetch / verify / resolve-dir）的超时毫秒数。',
    },
    maxLogTail: {
      type: 'number',
      default: CONFIG_DEFAULTS.maxLogTail,
      description: '返回给模型的日志尾部最大字符数。',
    },
  };
  if (!z) return fallbackSchema(spec);
  return z.object({
    scriptDir: z.string().default(spec.scriptDir.default).description(spec.scriptDir.description),
    workRoot: z.string().default(spec.workRoot.default).description(spec.workRoot.description),
    pythonPath: z.string().default('').description(spec.pythonPath.description),
    ffmpegPath: z.string().default('').description(spec.ffmpegPath.description),
    ffprobePath: z.string().default('').description(spec.ffprobePath.description),
    diskMinGb: z.number().default(spec.diskMinGb.default).description(spec.diskMinGb.description),
    startGraceMs: z.number().default(spec.startGraceMs.default).description(spec.startGraceMs.description),
    backendTimeoutMs: z.number().default(spec.backendTimeoutMs.default).description(spec.backendTimeoutMs.description),
    maxLogTail: z.number().default(spec.maxLogTail.default).description(spec.maxLogTail.description),
  });
}

const ConfigSchema = buildConfigSchema(loadSchemastery());

export { ConfigSchema as Config };
export const inject = ['tools'];

