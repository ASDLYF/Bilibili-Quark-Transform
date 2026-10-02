/**
 * 流水线面板（浏览器侧）。
 *
 * 用纯 JavaScript 注册，无构建步骤。数据来自 Host 侧的只读接口
 * `/bili-quark/api/*`（见 api.js），组件每 2 秒轮询一次拿实时进度。
 *
 * 布局约定（照官方插件写法）：
 *   - `sidebar.panellist` 只提供左侧图标与标题，id 必须等于 `main` 的 key
 *   - `main`（keyed）渲染整页；宿主通过相同的 key 把两者配成一对
 */
window.__ModuleLoader__.load({
  id: 'bili-quark-pipeline',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    const API = '/bili-quark/api';
    const POLL_MS = 2000;

    const T = {
      panel: 'Bilibili Quark Transform',
      title: 'Bilibili Quark Transform',
      intro: 'B 站视频批量下载（可选分辨率 / 分集）并上传到夸克网盘',
      loading: '正在读取…',
      idle: '空闲',
      running: '运行中',
      stalled: '已中断',
      failedStart: '启动失败',
      finished: '已完成',
      unknown: '未知',
      noSessions: '还没有运行记录。先在对话里让 Agent 抓取某个 UP 主的投稿，这里就会出现进度。',
      refresh: '刷新',
      verify: '远端核对',
      dryRun: '演练',
      start: '开始',
      stop: '停止',
      stopping: '正在停止…',
      confirmStop: '再点一次确认停止',
      colDone: '已完成',
      colPending: '待处理',
      colFail: '失败',
      colLocal: '本地残留',
      dir: '目标目录',
      dirLabel: '夸克目标目录',
      dirSave: '保存目录',
      dirVerify: '核对',
      addUp: '添加 UP 主',
      addCancel: '收起',
      addTitle: '添加 UP 主（只抓取投稿清单，不下载视频；目标目录在下方「夸克目标目录」里设置）',
      addMid: 'B 站 UID',
      addMidHint: 'B 站 UID，例如 3546918307236545',
      addConfirm: '添加',
      // 视频列表
      listTitle: '视频列表',
      fPending: '待处理',
      fAll: '全部',
      fUploaded: '已上传',
      selectPage: '全选本页',
      selectPending: '全选待处理',
      clearSel: '清空',
      runSelected: '开始处理选中',
      runAll: '开始全部待处理',
      noVideos: '该 UP 主还没有投稿清单，先用上方「添加 UP 主」抓取一次。',
      noMatch: '当前筛选下没有视频。',
      loadingVideos: '正在读取视频清单…',
      uploadedTag: '已上传',
      remotePresentTag: '远端已有',
      remoteMissingTag: '远端缺失',
      fMissing: '远端缺失',
      // 视频选择弹窗 + 分集 / 分辨率
      pickOpen: '选择视频…',
      pickTitle: '选择要处理的视频',
      pickClose: '关闭',
      pickDone: '完成',
      pickedNone: '还没勾选任何视频',
      pickHint: '勾选要处理的条目；多分集视频可以在下拉里挑具体哪一集（P1 不加后缀，P2 起文件名带 [P2]）。',
      partLabel: '分集',
      qualityLabel: '分辨率',
      qualityMax: '最高画质',
      qualityHint: '降画质下载会在文件名后加 [1080p] 之类的标记，最高画质保持原名，不会和已上传的重名。',
      // 合集选择浮层（添加 UP 主时，若该 UP 主有合集就先选一个）
      seasonTitle: '该 UP 主有合集，先选一个要抓的范围',
      seasonHint: '选中的合集只会抓该合集内的视频；选「全部投稿」则和以前一样抓整个空间。抓完可在「夸克目标目录」里设目录。',
      seasonAll: '全部投稿（不按合集）',
      seasonTotal: '条',
      seasonCancel: '取消',
      seasonLoading: '正在读取该 UP 主的合集列表…',
      seasonSwitch: '换合集',
      refetchList: '重新抓取清单',
      refetchTitle: '重新抓取当前 UP 主的清单（会重新探测每条的分辨率）。给该 UP 充过电之后点一次，之前被剔除的充电视频就会被捡回来。',
      checkSrc: '与B站核对',
      credCheck: '检测有效性',
      credChecking: '正在检测…',
      remoteNoCheck: '还没核对过夸克远端。点一次「核对」后，夸克上缺的条目会自动回到「待处理」。',
      remoteStale: '目标目录换过了，上一份远端快照已作废 —— 请重新点「核对」。',
      verifyBusy: '正在核对夸克远端目录（完整分页，可能要几十秒）…',
      horizontalTag: '横屏',
      verticalTag: '竖屏',
      prev: '上一页',
      next: '下一页',
      // 凭据
      credTitle: '登录凭据',
      credHint: '整行粘贴浏览器里复制的 Cookie。只写入本机后端目录；界面不回显，也不会经由网络返回。',
      credBili: 'B 站 Cookie（需含 SESSDATA）',
      credQuark: '夸克 Cookie（需含 __puus）',
      credSave: '保存凭据',
      credSaved: '已保存',
      credMissing: '未设置',
      credEnv: '来自环境变量',
      credSavedOk: '已保存，之后的下载/上传会用新凭据',
      credWhere: '写入位置',
      videos: '投稿',
      local: '本地残留',
      runningItem: '正在处理',
      stage: '阶段',
      log: '日志尾部',
      stageDownload: '下载',
      stageUpload: '上传',
      stageVerify: '校验',
      stageDelete: '删除',
      stageMerge: '合并',
      worker: '并发',
    };

    const STAGE = {
      download: T.stageDownload,
      merge: T.stageMerge,
      upload: T.stageUpload,
      verify: T.stageVerify,
      delete: T.stageDelete,
    };

    function fmtBytes(n) {
      const v = Number(n) || 0;
      if (v < 1024) return v + ' B';
      if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
      if (v < 1024 * 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB';
      return (v / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }

    function statusText(s) {
      if (s === 'running') return T.running;
      if (s === 'stalled') return T.stalled;
      if (s === 'failed_start') return T.failedStart;
      if (s === 'finished') return T.finished;
      if (s === 'starting') return T.loading;
      if (s === 'idle') return T.idle;
      return s || T.unknown;
    }

    function statusColor(s) {
      if (s === 'running' || s === 'starting') return 'var(--dsw-alias-state-success-primary)';
      if (s === 'stalled' || s === 'failed_start') return 'var(--dsw-alias-state-error-primary)';
      if (s === 'finished') return 'var(--dsw-alias-brand-primary)';
      return 'var(--dsw-alias-state-idle-primary)';
    }

    // ---------------------------------------------------------------- 样式

    // ---------------------------------------------------------------- 样式表
    //
    // 按钮必须走 CSS 类，不能只用内联 style：
    //  1) 内联样式拿不到 :hover / :active；
    //  2) 主按钮的文字色以前硬编码 #fff —— 而 --dsw-alias-brand-primary 在深色主题下
    //     是近白(#fafafa)，于是白底白字、按钮看起来是一片纯白。官方 Button 用
    //     --dsw-alias-label-primary-foreground（深色下为黑）做自动反色，这里照做。
    const PANEL_STYLE_ID = 'bili-quark-pipeline/panel.css';
    const PANEL_CSS = `
.bqbtn{display:inline-flex;align-items:center;justify-content:center;gap:4px;
  padding:5px 12px;border-radius:6px;border:1px solid var(--dsw-alias-border-l2);
  background:transparent;color:var(--dsw-alias-label-primary);
  cursor:pointer;font-size:12px;line-height:18px;font-family:inherit;
  white-space:nowrap;transition:background .12s ease,border-color .12s ease,color .12s ease}
.bqbtn:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover)}
.bqbtn:active:not(:disabled){background:var(--dsw-alias-interactive-bg-active)}
.bqbtn:disabled{cursor:not-allowed;opacity:.4}
.bqbtnPrimary{border-color:transparent;background:var(--dsw-alias-button-primary-fill);
  color:var(--dsw-alias-label-primary-foreground)}
.bqbtnPrimary:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover)}
.bqbtnDanger{border-color:var(--dsw-alias-state-error-primary);
  background:transparent;color:var(--dsw-alias-state-error-primary)}
.bqbtnDanger:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover-danger)}
.bqcard{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;
  background:var(--dsw-alias-bg-layer-1);padding:14px 16px;margin-bottom:14px}
.bqcheck{width:15px;height:15px;flex:none;cursor:pointer;accent-color:var(--dsw-alias-brand-primary)}
.bqlist{display:flex;flex-direction:column;gap:8px;margin-top:10px}
.bqitem{display:flex;gap:12px;align-items:flex-start;padding:8px;
  border:1px solid var(--dsw-alias-border-l1);border-radius:8px;
  background:var(--dsw-alias-bg-layer-2)}
.bqitem:hover{background:var(--dsw-alias-interactive-bg-hover)}
.bqcover{width:120px;height:68px;flex:none;object-fit:cover;border-radius:6px;
  background:var(--dsw-alias-bg-layer-1);display:block}
.bqtitle{font-size:13px;line-height:19px;color:var(--dsw-alias-label-primary);
  overflow:hidden;text-overflow:ellipsis;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical}
.bqmeta{font-size:11px;color:var(--dsw-alias-label-secondary);margin-top:4px;
  display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.bqchip{display:inline-block;padding:0 6px;border-radius:999px;font-size:10px;
  border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary)}
.bqchipH{border-color:var(--dsw-alias-state-warn-primary);color:var(--dsw-alias-state-warn-primary)}
.bqchipOk{border-color:var(--dsw-alias-state-success-primary);color:var(--dsw-alias-state-success-primary)}
.bqpager{display:flex;align-items:center;gap:10px;margin-top:12px;flex-wrap:wrap}
`;

    /** 注入面板样式；在 apply 里用 ctx.effect 注册，插件卸载时一并移除。 */
    function ensurePanelStyle() {
      if (typeof document === 'undefined') return () => {};
      const existing = document.querySelector(`style[data-plugin-css="${PANEL_STYLE_ID}"]`);
      if (existing) return () => {};
      const tag = document.createElement('style');
      tag.dataset.plugin = 'bili-quark-pipeline';
      tag.dataset.pluginCss = PANEL_STYLE_ID;
      tag.textContent = PANEL_CSS;
      document.head.appendChild(tag);
      return () => {
        try {
          tag.remove();
        } catch {
          /* 忽略 */
        }
      };
    }

    const S = {
      page: {
        padding: '24px clamp(20px, 4vw, 40px) 40px',
        // 关键：main 的容器是 overflow:hidden 的 flex 列。不给 width:100% 会收缩到内容宽，
        // 那样 margin:0 auto 就失效、整页贴左。
        width: '100%',
        maxWidth: '980px',
        margin: '0 auto',
        color: 'var(--dsw-alias-label-primary)',
        background: 'var(--dsw-alias-bg-base)',
        fontSize: '13px',
        lineHeight: 1.6,
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        boxSizing: 'border-box',
      },
      head: { display: 'flex', alignItems: 'baseline', gap: '10px', marginBottom: '4px' },
      title: { fontSize: '20px', fontWeight: 500, margin: 0, lineHeight: '28px' },
      intro: { color: 'var(--dsw-alias-label-secondary)', fontSize: '12px' },
      toolbar: { display: 'flex', gap: '8px', alignItems: 'center', margin: '14px 0', flexWrap: 'wrap' },
      // 外观全部交给 CSS 类（.bqbtn 等），这里只保留 className —— 不再写死颜色
      btn: { className: 'bqbtn' },
      btnPrimary: { className: 'bqbtn bqbtnPrimary' },
      btnDanger: { className: 'bqbtn bqbtnDanger' },
      card: {
        border: '1px solid var(--dsw-alias-border-l1)',
        borderRadius: '10px',
        background: 'var(--dsw-alias-bg-layer-1)',
        padding: '14px 16px',
        marginBottom: '14px',
      },
      muted: { color: 'var(--dsw-alias-label-secondary)', fontSize: '12px' },
      // 视频选择浮层：固定在视口中央，内容自己滚（点「选择视频…」才出现）
      modalCard: {
        position: 'fixed',
        left: '50%',
        top: '50%',
        transform: 'translate(-50%, -50%)',
        width: 'min(940px, 94vw)',
        maxHeight: '86vh',
        overflow: 'auto',
        zIndex: 9999,
        boxShadow: '0 18px 64px rgba(0, 0, 0, .45)',
      },
      input: {
        flex: '1 1 320px',
        minWidth: '220px',
        padding: '6px 10px',
        borderRadius: '6px',
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-layer-2)',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: '12px',
        fontFamily: 'inherit',
      },
      btnGroup: { display: 'flex', gap: '8px', alignItems: 'center', flex: '0 0 auto' },
      msg: { color: 'var(--dsw-alias-label-secondary)', fontSize: '12px', flexBasis: '100%', marginTop: '2px' },
      grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: '10px', marginTop: '12px' },
      statLabel: { color: 'var(--dsw-alias-label-secondary)', fontSize: '11px' },
      statValue: { fontSize: '16px', fontWeight: 600, marginTop: '2px' },
      bar: { height: '6px', borderRadius: '3px', background: 'var(--dsw-alias-bg-layer-2)', overflow: 'hidden', marginTop: '10px' },
      barFill: { height: '100%', background: 'var(--dsw-alias-brand-primary)', transition: 'width .4s ease' },
      tag: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        padding: '1px 8px',
        borderRadius: '999px',
        border: '1px solid var(--dsw-alias-border-l1)',
        fontSize: '11px',
      },
      dot: { width: '7px', height: '7px', borderRadius: '50%', flex: '0 0 auto' },
      row: { display: 'flex', gap: '10px', alignItems: 'center', padding: '6px 0', borderTop: '1px solid var(--dsw-alias-border-l1)' },
      bvid: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '12px', color: 'var(--dsw-alias-label-secondary)' },
      pre: {
        margin: '8px 0 0',
        padding: '10px 12px',
        borderRadius: '8px',
        background: 'var(--dsw-alias-bg-layer-2)',
        color: 'var(--dsw-alias-label-secondary)',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: '11px',
        lineHeight: 1.5,
        maxHeight: '220px',
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      },
      err: { color: 'var(--dsw-alias-state-error-primary)', marginTop: '8px', fontSize: '12px' },
    };

    function button(label, onClick, extra) {
      // S.btn 现在只带 className，外观由 CSS 类负责；extra 可继续追加内联覆盖（如 opacity）
      const merged = Object.assign({}, S.btn, extra || {});
      // extra 给了 disabled 就真正禁用（.bqbtn:disabled 会禁指针并降透明度）
      const props = Object.assign({ type: 'button', onClick, disabled: Boolean(merged.disabled) }, merged);
      return h('button', props, label);
    }

    // ---------------------------------------------------------------- 图标

    function PanelIcon(props) {
      const size = (props && props.size) || 18;
      const active = Boolean(props && props.active);
      return h(
        'svg',
        {
          viewBox: '0 0 24 24',
          width: size,
          height: size,
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.7,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': true,
          style: { opacity: active ? 1 : 0.85, display: 'block' },
        },
        h('rect', { x: 4, y: 2.5, width: 7.5, height: 13, rx: 2 }),
        h('circle', { cx: 7.75, cy: 12.6, r: 1, fill: 'currentColor', stroke: 'none' }),
        h('path', { d: 'M15.5 4.5v6.5m0 0l-2.4-2.4m2.4 2.4l2.4-2.4' }),
        h('path', { d: 'M13.6 20.5a4 4 0 0 1 .7-7.9 5.2 5.2 0 0 1 9.7 2.2 3.4 3.4 0 0 1-.6 5.7z' }),
      );
    }

    // ---------------------------------------------------------------- 数据

    function usePipelineData() {
      const [data, setData] = React.useState(null);
      const [error, setError] = React.useState('');
      const [busy, setBusy] = React.useState(false);

      const load = React.useCallback(async () => {
        try {
          const res = await fetch(API + '/data', { headers: { accept: 'application/json' } });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const json = await res.json();
          if (json && json.ok) {
            setData(json);
            setError('');
          } else {
            setError((json && json.error) || '读取失败');
          }
        } catch (e) {
          setError((e && e.message) || String(e));
        }
      }, []);

      React.useEffect(() => {
        let alive = true;
        load();
        const timer = setInterval(() => {
          if (alive) load();
        }, POLL_MS);
        return () => {
          alive = false;
          clearInterval(timer);
        };
      }, [load]);

      const act = React.useCallback(
        async (action, extra) => {
          setBusy(true);
          try {
            const res = await fetch(API + '/action', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify(Object.assign({ action }, extra || {})),
            });
            const json = await res.json();
            await load();
            return json;
          } catch (e) {
            setError((e && e.message) || String(e));
            return { ok: false, error: (e && e.message) || String(e) };
          } finally {
            setBusy(false);
          }
        },
        [load],
      );

      return { data, error, busy, act, reload: load };
    }

    /** 凭据状态：只判断"有没有 / 是否来自环境变量"，cookie 内容不经网络。 */
    function useCredentials() {
      const [cred, setCred] = React.useState(null);
      const [credErr, setCredErr] = React.useState('');

      const loadCred = React.useCallback(async () => {
        try {
          const res = await fetch(API + '/credentials', { headers: { accept: 'application/json' } });
          const json = await res.json();
          if (json && json.ok) {
            setCred(json);
            setCredErr('');
          } else {
            setCredErr((json && json.error) || '读取凭据状态失败');
          }
        } catch (e) {
          setCredErr((e && e.message) || String(e));
        }
      }, []);

      React.useEffect(() => {
        loadCred();
      }, [loadCred]);

      return { cred, credErr, loadCred };
    }

    // ---------------------------------------------------------------- 本地持久化

    /**
     * 勾选状态按 UP 主存进 localStorage：关掉弹窗、刷新页面都还在，
     * 点「选择视频…」能直接看到上次选了什么。
     */
    function loadSel(mid) {
      const m = new Map();
      if (!mid) return m;
      try {
        const raw = window.localStorage.getItem('bq:sel:' + mid);
        if (raw) {
          for (const [k, v] of Object.entries(JSON.parse(raw) || {})) m.set(k, v || null);
        }
      } catch {
        /* 读不到就当没选 */
      }
      return m;
    }

    function saveSel(mid, map) {
      if (!mid) return;
      try {
        const obj = {};
        for (const [k, v] of map.entries()) obj[k] = v || null;
        window.localStorage.setItem('bq:sel:' + mid, JSON.stringify(obj));
      } catch {
        /* 存不进就算了 */
      }
    }

    // ---------------------------------------------------------------- 页面

    function PanelPage() {
      const { data, error, busy, act, reload } = usePipelineData();
      const { cred, loadCred } = useCredentials();
      const [showCred, setShowCred] = React.useState(false);
      const [biliCookie, setBiliCookie] = React.useState('');
      const [quarkCookie, setQuarkCookie] = React.useState('');
      const [credMsg, setCredMsg] = React.useState('');
      const [confirmStop, setConfirmStop] = React.useState('');
      const [selMid, setSelMid] = React.useState('');
      // 目录输入框：默认显示该 UP 主已保存的目录路径，可改后保存
      const [selDir, setSelDir] = React.useState('');
      const [dirMsg, setDirMsg] = React.useState('');
      // 新增 UP 主表单：只填 B 站 UID，目录统一在下方「夸克目标目录」卡片里设置
      const [showAdd, setShowAdd] = React.useState(false);
      const [newMid, setNewMid] = React.useState('');
      const [addMsg, setAddMsg] = React.useState('');
      // 视频列表：分页 / 勾选 / 过滤
      const [vids, setVids] = React.useState(null);
      const [vidsErr, setVidsErr] = React.useState('');
      const [vidsLoading, setVidsLoading] = React.useState(false);
      const [page, setPage] = React.useState(1);
      // 勾选：Map<bvid, page|null>。page 是用户在下拉里挑的分集（null = 默认第 1 集）
      const [sel, setSel] = React.useState(() => new Map());
      const [filter, setFilter] = React.useState('pending'); // pending | missing | all | uploaded
      const [verifyMsg, setVerifyMsg] = React.useState('');
      const [showPicker, setShowPicker] = React.useState(false); // 视频选择弹窗
      const [quality, setQuality] = React.useState(() => {
        try {
          return Number(window.localStorage.getItem('bq:quality')) || 0;
        } catch {
          return 0;
        }
      }); // 0 = 最高画质
      const [checkMsg, setCheckMsg] = React.useState('');   // 「与B站核对」结果
      const [repairTargets, setRepairTargets] = React.useState([]); // checkSrc 返回的大小不符条目（bvid 列表）
      const [repairBusy, setRepairBusy] = React.useState(false);
      const [seasonPick, setSeasonPick] = React.useState(null); // {mid, seasons} 选合集浮层
      const [seasonBusy, setSeasonBusy] = React.useState(false);
      const [credInfo, setCredInfo] = React.useState(null); // 凭据检测结果
      const [credBusy, setCredBusy] = React.useState(false);

      const sessions = (data && data.sessions) || [];
      const active = sessions.find((s) => s.mid === selMid) || sessions.find((s) => s.alive) || sessions[0] || null;

      React.useEffect(() => {
        if (!selMid && active) setSelMid(active.mid);
      }, [selMid, active]);

      // 切换 UP 主时，把输入框重置成它的当前目录
      React.useEffect(() => {
        if (active) {
          const raw = String(active.dirPath || active.dirName || '').trim();
          // 旧配置里可能只存了目录名（没有前导 /），补上让它是可直接用的路径
          setSelDir(raw && !raw.startsWith('/') && !raw.includes('\\') ? '/' + raw : raw);
        }
        setDirMsg('');
      }, [active && active.mid]);

      const stop = async (mid) => {
        if (confirmStop !== mid) {
          setConfirmStop(mid);
          setTimeout(() => setConfirmStop(''), 4000);
          return;
        }
        setConfirmStop('');
        await act('stop', { mid });
      };

      const saveDir = async () => {
        if (!active) return;
        const v = selDir.trim();
        if (!v) {
          setDirMsg('请填写夸克目录路径，例如 /B站上传/B站竖屏-小圆脸');
          return;
        }
        setDirMsg('正在解析并保存…');
        const r = await act('saveDir', { mid: active.mid, path: v });
        if (r && r.ok) {
          setDirMsg('已保存到该 UP 主的工作目录');
        } else {
          setDirMsg('失败：' + String((r && r.error) || '未知错误').slice(0, 200));
        }
        await reload();
      };

      /** 读取某 UP 主的合集并弹选择浮层；没有合集就直接抓全部投稿。 */
      const askSeasonsThenFetch = async (m) => {
        // 先问后端要合集列表：该 UP 主有合集就先让用户选范围，再决定抓哪一份清单
        setSeasonBusy(true);
        setAddMsg(T.seasonLoading);
        let seasons = [];
        try {
          const sr = await act('seasons', { mid: m });
          if (sr && sr.ok) seasons = (sr.data && sr.data.seasons) || [];
          else if (sr && sr.error) setAddMsg('读取合集失败（将直接抓全部投稿）：' + String(sr.error).slice(0, 160));
        } catch (e) {
          setAddMsg('读取合集失败（将直接抓全部投稿）：' + String((e && e.message) || e).slice(0, 160));
        }
        setSeasonBusy(false);
        if (seasons.length) {
          setSeasonPick({ mid: m, seasons });
          return;
        }
        await runAddUp(m, 0);
      };

      // 只做「抓取投稿清单」；目标目录交给下方目录卡片（saveDir），避免两处入口
      const addUp = async () => {
        const m = newMid.trim();
        if (!/^\d{4,20}$/.test(m.replace(/^.*space\.bilibili\.com\//, '').replace(/[/?#].*$/, ''))) {
          setAddMsg('请填写 UP 主的数字 UID，例如 3546918307236545');
          return;
        }
        await askSeasonsThenFetch(m);
      };

      /** 真正发起抓取；seasonId 为 0/空 表示抓全部投稿。 */
      const runAddUp = async (m, seasonId, force) => {
        setSeasonPick(null);
        setSeasonBusy(true);
        setAddMsg(seasonId
          ? '正在抓取该合集（不下载视频，约需 1～3 分钟，请等这一步返回）…'
          : (force
              ? '正在重新抓取投稿清单，会重新探测每条分辨率（不下载视频，请等这一步返回）…'
              : '正在抓取投稿清单（不下载视频；投稿多的 UP 会自动分批，请等这一步返回）…'));
        let r = null;
        try {
          r = await act('addUp', { mid: m, seasonId: seasonId || undefined, force: force || undefined });
        } finally {
          setSeasonBusy(false);
        }
        if (r && r.ok) {
          const added = String((r.data && r.data.mid) || m);
          setSelMid(added);
          setNewMid('');
          // 不自动收起：这条两步引导要留在屏幕上才看得见
          const locked = Number((r.data && r.data.locked) || 0);
          const pending = Number((r.data && r.data.pending) || 0);
          setAddMsg(
            '已添加 UP 主 ' + added + '。' +
            (locked ? '已自动剔除 ' + locked + ' 条下载不了的（充电专属等），未计入清单。' : '') +
            (pending
              ? '这个 UP 主投稿很多，还有 ' + pending +
                ' 条没探测完 —— 再点一次「重新抓取清单」会接着抓，已经抓好的不会重跑。'
              : '下一步：在下方「夸克目标目录」里填目录路径，再点「保存目录」。'),
          );
        } else {
          setAddMsg('失败：' + String((r && r.error) || '未知错误').slice(0, 300));
        }
        await reload();
        // 抓取结束（无论成败）都重新拉一次清单，避免列表停在"还没有投稿清单"的旧状态
        await loadVideos();
      };

      /** 重新抓取当前 UP 主的清单（force，忽略"清单已存在就不重抓"）。
       *  用途：给该 UP 充过电之后，把之前被剔除的充电视频捡回来。 */
      const refetchList = async () => {
        const m = (active && active.mid) || selMid;
        if (!m) return;
        await runAddUp(m, 0, true);
      };

      const children = [];

      /** 保存凭据：只把用户新填的值发出去（空输入框=不修改）。 */
      const saveCred = async () => {
        if (!biliCookie.trim() && !quarkCookie.trim()) {
          setCredMsg('两个输入框都是空的，没有需要保存的内容');
          return;
        }
        setCredMsg('正在保存…');
        const payload = {};
        if (biliCookie.trim()) payload.biliCookie = biliCookie;
        if (quarkCookie.trim()) payload.quarkCookie = quarkCookie;
        const r = await act('saveCred', payload);
        if (r && r.ok) {
          setBiliCookie('');
          setQuarkCookie('');
          setCredMsg(T.credSavedOk);
          await loadCred();
        } else {
          setCredMsg('保存失败：' + String((r && r.error) || '未知错误').slice(0, 200));
        }
      };

      // 视频清单体积大，只在切换 UP 主 / 手动刷新 / 处理完成后拉取，不跟着 2 秒轮询走
      const activeMid = active ? active.mid : '';
      const loadVideos = React.useCallback(async () => {
        if (!activeMid) {
          setVids(null);
          return;
        }
        setVidsLoading(true);
        try {
          const res = await fetch(API + '/videos?mid=' + encodeURIComponent(activeMid), {
            headers: { accept: 'application/json' },
          });
          const json = await res.json();
          if (json && json.ok) {
            setVids(json);
            setVidsErr('');
          } else {
            setVids(null);
            setVidsErr((json && json.error) || '读取视频清单失败');
          }
        } catch (e) {
          setVids(null);
          setVidsErr((e && e.message) || String(e));
        } finally {
          setVidsLoading(false);
        }
      }, [activeMid]);

      React.useEffect(() => {
        loadVideos();
      }, [loadVideos]);

      // 切换 UP 主时列表状态归位，并把该 UP 主上次的勾选恢复出来
      React.useEffect(() => {
        setPage(1);
        setSel(loadSel(activeMid));
      }, [activeMid]);

      // 勾选变化就写回本地（按 UP 主分开存）
      React.useEffect(() => {
        saveSel(activeMid, sel);
      }, [sel, activeMid]);

      // 分辨率选择也记住
      React.useEffect(() => {
        try {
          window.localStorage.setItem('bq:quality', String(quality || 0));
        } catch {
          /* 忽略 */
        }
      }, [quality]);

      // ---- 过滤 / 分页 / 勾选（派生值，不用 hook，避免改变 hook 数量）
      const allVids = (vids && vids.items) || [];
      /*
       * 「待处理」以夸克远端为准：有核对快照时，远端没有的就算待处理
       * （哪怕 state.jsonl 里记着「已上传」——那正是被删/传丢、需要补传的）；
       * 该条没进过快照（例如清单刚更新、多出来的新投稿）时退回按 state 判断。
       */
      const needsWork = (v) => (v.remoteChecked ? !v.remotePresent : !v.uploaded);
      const remoteMissingN = allVids.filter((v) => v.remoteChecked && !v.remotePresent).length;
      const pendingN = allVids.filter(needsWork).length;
      const shown = allVids.filter((v) =>
        filter === 'all'
          ? true
          : filter === 'uploaded'
            ? v.uploaded
            : filter === 'missing'
              ? v.remoteChecked && !v.remotePresent
              : needsWork(v),
      );
      const PER_PAGE = 12;
      const pageCount = Math.max(1, Math.ceil(shown.length / PER_PAGE));
      const pg = Math.min(Math.max(page, 1), pageCount);
      const pageItems = shown.slice((pg - 1) * PER_PAGE, pg * PER_PAGE);
      // 勾选里远端已经有几条（会被重跑，提示一下免得以为白干）
      const selOnRemote = allVids.filter((v) => sel.has(v.bvid) && v.remoteChecked && v.remotePresent).length;

      const toggleOne = (bvid, page) => {
        setSel((prev) => {
          const nx = new Map(prev);
          if (nx.has(bvid)) nx.delete(bvid);
          else nx.set(bvid, page || prev.get(bvid) || null);
          return nx;
        });
      };
      /** 下拉里挑分集 = 明确要这一集，顺手勾上 */
      const setItemPage = (bvid, page) => {
        setSel((prev) => {
          const nx = new Map(prev);
          nx.set(bvid, page > 1 ? page : null);
          return nx;
        });
      };
      const selectPage = () => {
        setSel((prev) => {
          const nx = new Map(prev);
          pageItems.forEach((v) => {
            if (!nx.has(v.bvid)) nx.set(v.bvid, null);
          });
          return nx;
        });
      };
      const selectPending = () => {
        // 与「待处理」筛选同一口径：夸克上缺的都选上（含远端缺失但本机以为传过的）
        const nx = new Map();
        allVids.filter(needsWork).forEach((v) => nx.set(v.bvid, null));
        setSel(nx);
      };
      const selectMissing = () => {
        const nx = new Map(sel);
        allVids.filter((v) => v.remoteChecked && !v.remotePresent).forEach((v) => {
          if (!nx.has(v.bvid)) nx.set(v.bvid, null);
        });
        setSel(nx);
      };

      /** 核对夸克远端目录：verify 会落盘远端快照，面板据此重算「待处理」。 */
      const doVerify = async () => {
        if (!active) return;
        setVerifyMsg(T.verifyBusy);
        const r = await act('verify', { mid: active.mid });
        const d = (r && r.data) || {};
        if (r && r.ok) {
          setVerifyMsg(
            `核对完成：清单 ${d.target ?? 0} 条，远端已有 ${d.confirmed ?? 0} 条，` +
              `缺 ${d.missing ?? 0} 条` +
              (d.sizeMismatch ? `，字节数不符 ${d.sizeMismatch} 条` : '') +
              (d.extraFiles && d.extraFiles.length ? `，远端多余文件 ${d.extraFiles.length} 个` : '') +
              '。列表已按远端刷新。',
          );
        } else {
          setVerifyMsg('核对失败：' + String((r && r.error) || '未知错误').slice(0, 300));
        }
        await loadVideos();
      };

      /** 与 B 站源核对：拿 CDN 的真实字节数跟网盘里的文件比（不下载内容） */
      const doCheckSrc = async () => {
        if (!active) return;
        setCheckMsg('正在向 B 站取流大小并核对网盘文件…（每条两个 Range 请求，稍等）');
        const r = await act('checkSrc', { mid: active.mid, maxHeight: quality || undefined });
        const d = (r && r.data) || {};
        if (r && r.ok) {
          const bad = (d.items || []).filter((x) => x && x.ok === false);
          // 只把「大小不符」的条目作为修复目标（missing/error 不走修复流程）
          const sizeBad = bad.filter((x) => x.status === 'size');
          setRepairTargets(sizeBad.map((x) => x.bvid));
          setCheckMsg(
            `与B站核对完成：共 ${d.checked ?? 0} 条，一致 ${d.okCount ?? 0}，` +
              `大小不符 ${d.sizeMismatch ?? 0}，网盘缺失 ${d.missing ?? 0}，取流失败 ${d.failed ?? 0}` +
              (d.tolerancePercent != null ? `（容差 ${d.tolerancePercent}%）` : '') +
              (bad.length
                ? '；异常：' +
                  bad
                    .slice(0, 3)
                    .map((x) =>
                      x.status === 'missing'
                        ? `${x.bvid} 缺`
                        : x.status === 'error'
                          ? `${x.bvid} 取流失败`
                          : `${x.bvid} 差 ${x.diff_percent}%`,
                    )
                    .join('、') +
                  (bad.length > 3 ? ` 等 ${bad.length} 条` : '')
                : '。'),
          );
        } else {
          setRepairTargets([]);
          setCheckMsg('核对失败：' + String((r && r.error) || '未知错误').slice(0, 300));
        }
      };

      /** 修复大小不符条目：删除夸克端文件 → 重置 state → 自动重传 */
      const doRepair = async () => {
        if (!active || !repairTargets.length) return;
        setRepairBusy(true);
        setCheckMsg(`正在修复 ${repairTargets.length} 条大小不符的视频…（删除远端文件 + 重置状态）`);
        const r = await act('repair', { mid: active.mid, only: repairTargets });
        if (r && r.ok) {
          const d = r.data || {};
          setCheckMsg(
            `修复完成：删除 ${d.deleted ?? 0} 个远端文件，重置 ${(d.resetBvids || []).length} 条状态。正在启动重传…`,
          );
          // 自动触发重传
          const runR = await act('run', {
            mid: active.mid,
            only: d.resetBvids || repairTargets,
            force: true,
            maxHeight: quality || undefined,
          });
          setCheckMsg(
            runR && runR.ok
              ? `已启动重传 ${(d.resetBvids || repairTargets).length} 条（force 模式）`
              : '重传启动失败：' + String((runR && runR.error) || '未知错误').slice(0, 200),
          );
          setRepairTargets([]);
          await reload();
          await loadVideos();
        } else {
          setCheckMsg('修复失败：' + String((r && r.error) || '未知错误').slice(0, 300));
        }
        setRepairBusy(false);
      };

      /** 凭据有效性检测（B 站 nav + 夸克 member，顺带回过期时间） */
      const doCheckCred = async () => {
        setCredBusy(true);
        setCredMsg(T.credChecking);
        const r = await act('checkCred', {});
        setCredBusy(false);
        if (r && r.ok) {
          setCredInfo(r.data || {});
          setCredMsg('');
        } else {
          setCredMsg('检测失败：' + String((r && r.error) || '未知错误').slice(0, 200));
        }
        await loadCred();
      };

      /** 处理勾选的视频：横竖屏一视同仁，宿主会按选择清空默认排除名单。 */
      const runSelected = async () => {
        const entries = [...sel.entries()];
        if (!entries.length) {
          setDirMsg('还没有勾选任何视频');
          return;
        }
        // 带分集的条目写成 `BV1xxx:2`，宿主与后端都认这个写法
        const only = entries.map(([bv, pg]) => (pg && pg > 1 ? `${bv}:${pg}` : bv));
        const hasHorizontal = allVids.some((v) => sel.has(v.bvid) && v.vertical === false);
        setDirMsg(`已提交 ${only.length} 条，正在启动…`);
        const r = await act('run', {
          mid: activeMid,
          only,
          includeHorizontal: hasHorizontal,
          maxHeight: quality || undefined,
        });
        setDirMsg(
          r && r.ok
            ? `已启动，处理 ${only.length} 条（分辨率 ${quality ? quality + 'p' : '最高画质'}）`
            : '启动失败：' + String((r && r.error) || '未知错误').slice(0, 200),
        );
        await reload();
        await loadVideos();
      };


      children.push(
        h(
          'div',
          { key: 'head', style: S.head },
          h('h2', { style: S.title }, T.title),
          h('span', { style: S.intro }, T.intro),
        ),
      );

      children.push(
        h(
          'div',
          { key: 'toolbar', style: S.toolbar },
          button(T.refresh, () => {
            reload();
            loadVideos();
          }),
          button(showAdd ? T.addCancel : T.addUp, () => {
            setShowAdd(!showAdd);
            setAddMsg('');
          }),
          // 视频清单收进弹窗：主面板只留一个入口 + 已选计数
          active
            ? button(`${T.pickOpen}${sel.size ? `（已选 ${sel.size}）` : ''}`, () => setShowPicker(true))
            : null,
          // 当前清单来自哪个合集（抓合集时才有；抓全部投稿时不显示）
          active && vids && vids.seasonName
            ? h('span', { style: Object.assign({}, S.muted, { alignSelf: 'center' }) },
                `清单来源：${vids.seasonName}（${vids.total} ${T.seasonTotal}）`)
            : null,
          // 换合集：直接回弹合集选择浮层（复用已选 UP 主的 mid，不必重新填 UID）
          active && vids && vids.seasonName
            ? button(T.seasonSwitch, () => askSeasonsThenFetch((active && active.mid) || selMid))
            : null,
          // 重新抓取清单：给该 UP 充过电之后，用它把之前被剔除的充电视频捡回来
          active
            ? button(T.refetchList, refetchList,
                Object.assign({ title: T.refetchTitle }, seasonBusy ? { opacity: 0.6 } : null))
            : null,
          // 处理勾选的视频（横竖屏一视同仁）；没勾选时禁用
          active
            ? button(
                `${T.runSelected}${sel.size ? `(${sel.size})` : ''}`,
                runSelected,
                Object.assign({}, S.btnPrimary, sel.size ? null : { opacity: 0.5, cursor: 'not-allowed' }),
              )
            : null,
          // 下载分辨率（对本次勾选/整队列生效；最高画质保持老文件名）
          active
            ? h(
                'label',
                { style: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px' } },
                h('span', { style: S.muted }, T.qualityLabel),
                h(
                  'select',
                  {
                    className: 'bqbtn',
                    value: String(quality || 0),
                    title: T.qualityHint,
                    onChange: (e) => setQuality(Number(e.target.value) || 0),
                  },
                  [
                    { v: 0, t: T.qualityMax },
                    { v: 2160, t: '2160p' },
                    { v: 1440, t: '1440p' },
                    { v: 1080, t: '1080p' },
                    { v: 720, t: '720p' },
                    { v: 480, t: '480p' },
                    { v: 360, t: '360p' },
                  ].map((o) => h('option', { key: o.v, value: String(o.v) }, o.t)),
                ),
              )
            : null,
          active
            ? button(T.dryRun, () => act('runDry', { mid: active.mid, maxHeight: quality || undefined }), busy ? { opacity: 0.6 } : null)
            : null,
          active
            ? button(T.verify, doVerify, busy ? { opacity: 0.6 } : null)
            : null,
          // 与 B 站源头核对大小（拿 CDN 真实字节数，不下载）
          active
            ? button(T.checkSrc, doCheckSrc, busy || repairBusy ? { opacity: 0.6 } : null)
            : null,
          // 修复大小不符条目（删除远端 + 重置 state + 重传）
          active && repairTargets.length
            ? button(
                repairBusy ? '修复中…' : `修复 ${repairTargets.length} 条`,
                doRepair,
                busy || repairBusy ? { opacity: 0.6 } : S.btnDanger,
              )
            : null,
          // 处理整个待处理队列（不勾选时的批量方式）
          active && !active.alive && active.state !== 'running'
            ? button(T.runAll, () => act('run', { mid: active.mid, maxHeight: quality || undefined }), busy ? { opacity: 0.6 } : null)
            : null,
          active && active.alive
            ? button(confirmStop === active.mid ? T.confirmStop : T.stop, () => stop(active.mid), S.btnDanger)
            : null,
          h('span', { key: 'sp', style: { flex: '1 1 auto' } }),
          data && data.workRoot ? h('span', { style: S.muted }, data.workRoot) : null,
        ),
      );

      if (error) children.push(h('div', { key: 'err', style: S.err }, '接口错误：' + error));
      if (verifyMsg) children.push(h('div', { key: 'vmsg', style: S.muted }, verifyMsg));
      if (checkMsg) children.push(h('div', { key: 'cmsg', style: S.muted }, checkMsg));
      // 清单读不到时，主面板也要看得见（列表现在收在浮层里）
      if (vidsErr) children.push(h('div', { key: 'verr', style: S.muted }, vidsErr));

      // ---- 登录凭据（B 站 / 夸克 cookie）。值不回显，只显示"是否已设置"与时间。
      const credState = (side) => {
        const c = cred ? cred[side] : null;
        if (!c) return T.credMissing;
        if (!c.present && !c.envSet) return T.credMissing;
        if (c.envSet && !c.present) return T.credEnv;
        return c.hasMarker ? `${T.credSaved}（${c.bytes || 0} 字节）` : '已保存但格式可疑（缺少必需字段）';
      };
      /** 「保存于 / 有效至」这类时间信息；两项都为空就不占地方。 */
      const credTime = (side) => {
        const c = (cred && cred[side]) || {};
        const bits = [];
        if (c.savedAt) bits.push(`保存于 ${c.savedAt}`);
        if (c.expiresAt) bits.push(`有效至 ${c.expiresAt}`);
        else if (side === 'quark' && (c.present || c.envSet)) bits.push('夸克 cookie 不带过期时间');
        if (c.source === 'env') bits.push('来源：环境变量');
        return bits.join('　');
      };
      /** 上一次「检测有效性」的结果摘要。 */
      const credLive = (side) => {
        const d = credInfo && credInfo[side];
        if (!d) return '';
        if (!d.ok) return `检测：失效（${String(d.error || '').slice(0, 60)}）`;
        if (side === 'bili') {
          return `检测：有效　账号 ${d.uname || '?'}${d.expires_at ? `　有效至 ${d.expires_at}` : ''}`;
        }
        return `检测：有效　${d.member_type || ''} 容量 ${d.capacity_gb || 0}GB / 已用 ${d.used_gb || 0}GB`;
      };
      children.push(
        h(
          'div',
          { key: 'cred', className: 'bqcard' },
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' } },
            h('div', { style: S.statLabel }, T.credTitle),
            h('span', { style: { flex: '1 1 auto' } }),
            h('span', { style: S.muted },
              `B 站：${credState('bili')}　夸克：${credState('quark')}`),
            button(credBusy ? T.credChecking : T.credCheck, doCheckCred, credBusy ? { opacity: 0.6 } : null),
            button(showCred ? T.addCancel : '填写', () => {
              setShowCred(!showCred);
              setCredMsg('');
            }),
          ),
          credTime('bili') || credTime('quark')
            ? h(
                'div',
                { style: Object.assign({}, S.muted, { marginTop: '6px' }) },
                `B 站：${credTime('bili') || '—'}`,
                h('br'),
                `夸克：${credTime('quark') || '—'}`,
              )
            : null,
          credLive('bili') || credLive('quark')
            ? h(
                'div',
                { style: Object.assign({}, S.muted, { marginTop: '6px' }) },
                credLive('bili') ? h('div', null, `B 站　${credLive('bili')}`) : null,
                credLive('quark') ? h('div', null, `夸克　${credLive('quark')}`) : null,
              )
            : null,
          showCred
            ? h(
                'div',
                { style: { marginTop: '10px' } },
                h('div', { style: S.muted }, T.credHint),
                h(
                  'div',
                  { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '8px' } },
                  h('input', {
                    type: 'password',
                    value: biliCookie,
                    placeholder: T.credBili,
                    autoComplete: 'off',
                    onChange: (e) => setBiliCookie(e.target.value),
                    style: S.input,
                    'aria-label': T.credBili,
                  }),
                ),
                h(
                  'div',
                  { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '8px' } },
                  h('input', {
                    type: 'password',
                    value: quarkCookie,
                    placeholder: T.credQuark,
                    autoComplete: 'off',
                    onChange: (e) => setQuarkCookie(e.target.value),
                    onKeyDown: (e) => {
                      if (e.key === 'Enter') saveCred();
                    },
                    style: S.input,
                    'aria-label': T.credQuark,
                  }),
                ),
                h(
                  'div',
                  { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '10px' } },
                  button(T.credSave, saveCred, Object.assign({}, S.btnPrimary, busy ? { opacity: 0.6 } : {})),
                  cred && cred.scriptDir
                    ? h('span', { style: S.muted }, `${T.credWhere}：${cred.scriptDir}`)
                    : null,
                ),
                credMsg
                  ? h('div', { style: Object.assign({}, S.msg, /失败|空/.test(credMsg) ? { color: 'var(--dsw-alias-state-error-primary)' } : null) }, credMsg)
                  : null,
              )
            : null,
        ),
      );

      // 新增 UP 主表单（只有 UID 一项：目录在下方目录卡片里设置）
      children.push(
        h(
          'div',
          { key: 'addform', style: Object.assign({}, S.card, showAdd ? null : { display: 'none' }) },
          h('div', { style: S.statLabel }, T.addTitle),
          h(
            'div',
            { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '8px' } },
            h('input', {
              type: 'text',
              value: newMid,
              placeholder: T.addMidHint,
              onChange: (e) => setNewMid(e.target.value),
              onKeyDown: (e) => {
                if (e.key === 'Enter') addUp();
              },
              style: Object.assign({}, S.input, { flex: '0 1 200px' }),
              'aria-label': T.addMid,
            }),
            h(
              'div',
              { style: S.btnGroup },
              button(T.addConfirm, addUp, Object.assign({}, S.btnPrimary, busy ? { opacity: 0.6 } : {})),
            ),
          ),
          addMsg
            ? h('div', { style: Object.assign({}, S.msg, /失败|请填写/.test(addMsg) ? { color: 'var(--dsw-alias-state-error-primary)' } : null) }, addMsg)
            : null,
        ),
      );

      if (!sessions.length) {
        children.push(
          h(
            'div',
            { key: 'empty', style: S.card },
            h('div', { style: S.muted }, T.noSessions),
          ),
        );
        return h('div', { style: S.page }, children);
      }

      if (sessions.length > 1) {
        children.push(
          h(
            'div',
            { key: 'tabs', style: S.toolbar },
            sessions.map((s) =>
              h(
                'button',
                {
                  key: s.mid,
                  type: 'button',
                  className: 'bqbtn',
                  onClick: () => setSelMid(s.mid),
                  style: s.mid === active.mid ? { borderColor: 'var(--dsw-alias-brand-primary)' } : null,
                },
                (s.dirName || s.mid) + (s.alive ? ' ●' : ''),
              ),
            ),
          ),
        );
      }

      if (active) {
        // ---- 目标目录输入框：可直接改并保存
        children.push(
          h(
            'div',
            { key: 'dirform', style: S.card },
            h('div', { style: S.statLabel }, T.dirLabel),
            h(
              'div',
              { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '8px' } },
              h('input', {
                type: 'text',
                value: selDir,
                placeholder: '/B站上传/B站竖屏-某某',
                onChange: (e) => setSelDir(e.target.value),
                onKeyDown: (e) => {
                  if (e.key === 'Enter') saveDir();
                },
                style: S.input,
                'aria-label': T.dirLabel,
              }),
              h(
                'div',
                { style: S.btnGroup },
                button(T.dirSave, saveDir, busy ? { opacity: 0.6 } : null),
                button(T.dirVerify, async () => {
                  const v = selDir.trim();
                  if (v) {
                    setDirMsg('正在解析…');
                    await act('saveDir', { mid: active.mid, path: v });
                  }
                  setDirMsg('');
                  const r = await act('verify', { mid: active.mid });
                  setDirMsg(r && r.ok
                    ? `核对完成：${r.data.confirmed}/${r.data.target} 一致，远端 ${r.data.remoteTotalGb} GB`
                    : '核对失败：' + String((r && r.error) || ''));
                }, busy ? { opacity: 0.6 } : null),
              ),
            ),
            dirMsg
              ? h('div', { style: Object.assign({}, S.msg, /失败|请填写/.test(dirMsg) ? { color: 'var(--dsw-alias-state-error-primary)' } : null) }, dirMsg)
              : null,
            h('div', { style: Object.assign({}, S.muted, { marginTop: '6px' }) },
              active.dirName ? `当前：${active.dirName}` : '（该 UP 主还没设目标目录）'),
          ),
        );

        // ---- 视频选择浮层：点「选择视频…」才渲染，主面板不再长期占屏
        const chips = (list) =>
          list.map((f) =>
            h('button', {
              key: f.k,
              type: 'button',
              className: 'bqbtn',
              style: filter === f.k ? { borderColor: 'var(--dsw-alias-brand-primary)' } : null,
              onClick: () => {
                setFilter(f.k);
                setPage(1);
              },
            }, f.label),
          );

        // ---- 合集选择浮层：添加 UP 主时该 UP 主有合集才出现
        if (seasonPick) {
          const seasonList = seasonPick.seasons || [];
          const pickRow = (label, onClick, key) =>
            h('button', {
              key,
              type: 'button',
              className: 'bqbtn',
              style: { textAlign: 'left', justifyContent: 'flex-start', width: '100%' },
              onClick,
            }, label);
          children.push(
            h(
              'div',
              { key: 'seasonpick', className: 'bqcard', style: S.modalCard },
              h('div', { style: S.statLabel },
                `${T.seasonTitle}（UID ${seasonPick.mid}）`),
              h('div', { style: Object.assign({}, S.muted, { marginTop: '6px' }) }, T.seasonHint),
              h(
                'div',
                { style: { marginTop: '10px', display: 'flex', flexDirection: 'column', gap: '6px' } },
                pickRow(T.seasonAll, () => runAddUp(seasonPick.mid, 0), 'season-all'),
                ...seasonList.map((s) =>
                  pickRow(
                    `${s.name || ('合集 ' + s.seasonId)}（${s.total} ${T.seasonTotal}）`,
                    () => runAddUp(seasonPick.mid, s.seasonId),
                    'season-' + s.seasonId,
                  )),
              ),
              h(
                'div',
                { className: 'bqpager' },
                button(T.seasonCancel, () => { setSeasonPick(null); setAddMsg(''); },
                  seasonBusy ? { opacity: 0.5 } : null),
              ),
            ),
          );
        }

        if (showPicker) {
        if (vidsLoading && !vids) {
          children.push(h('div', { key: 'vids-loading', style: S.card }, h('div', { style: S.muted }, T.loadingVideos)));
        } else if (vidsErr) {
          children.push(
            h('div', { key: 'vids-err', style: S.card },
              h('div', { style: S.statLabel }, T.listTitle),
              h('div', { style: S.err }, vidsErr)),
          );
        } else {
          children.push(
            h(
              'div',
              { key: 'vidlist', className: 'bqcard', style: S.modalCard },
              h(
                'div',
                { style: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' } },
                h('div', { style: S.statLabel },
                  `${T.pickTitle}：${allVids.length} 条（已上传 ${(vids && vids.uploadedCount) || 0}` +
                  (vids && vids.remoteCheckedAt
                    ? `｜远端缺失 ${remoteMissingN}｜待处理 ${pendingN}）`
                    : '）')),
                h('span', { style: { flex: '1 1 auto' } }),
                ...chips([
                  { k: 'pending', label: `${T.fPending} ${pendingN}` },
                  { k: 'missing', label: `${T.fMissing} ${remoteMissingN}` },
                  { k: 'all', label: T.fAll },
                  { k: 'uploaded', label: T.fUploaded },
                ]),
                button(T.pickDone, () => setShowPicker(false), S.btnPrimary),
              ),
              h('div', { style: Object.assign({}, S.muted, { marginTop: '6px' }) }, T.pickHint),
              vids && vids.remoteStale
                ? h('div', { style: Object.assign({}, S.muted, { marginTop: '8px' }) }, T.remoteStale)
                : vids && !vids.remoteCheckedAt && allVids.length
                  ? h('div', { style: Object.assign({}, S.muted, { marginTop: '8px' }) }, T.remoteNoCheck)
                  : vids && vids.remoteCheckedAt
                    ? h('div', { style: Object.assign({}, S.muted, { marginTop: '8px' }) },
                        `远端核对于 ${vids.remoteCheckedAt}（远端 ${vids.remoteFiles} 个文件）。` +
                        '「待处理」以夸克实际文件为准：远端没有的都算待处理，勾选后点「开始处理选中」即可补传。')
                    : null,
              h(
                'div',
                { className: 'bqpager' },
                button(T.selectPage, selectPage),
                button(T.selectPending, selectPending),
                button(`${T.fMissing}全选`, selectMissing, remoteMissingN ? null : { opacity: 0.5 }),
                button(T.clearSel, () => setSel(new Map()), sel.size ? null : { opacity: 0.5 }),
                h('span', { style: S.muted },
                  `已选 ${sel.size} 条` +
                  (selOnRemote ? `（其中 ${selOnRemote} 条远端已有，会重跑）` : '')),
              ),
              !shown.length
                ? h('div', { style: Object.assign({}, S.muted, { marginTop: '10px' }) }, T.noMatch)
                : h(
                    'div',
                    { className: 'bqlist' },
                    pageItems.map((v) =>
                      h(
                        'div',
                        { key: v.bvid, className: 'bqitem' },
                        h('input', {
                          type: 'checkbox',
                          className: 'bqcheck',
                          checked: sel.has(v.bvid),
                          onChange: () => toggleOne(v.bvid, v.statePage),
                          'aria-label': `选择 ${v.title}`,
                        }),
                        v.pic
                          ? h('img', {
                              className: 'bqcover',
                              src: v.pic + '@320w_200h_1c.webp',
                              alt: '',
                              loading: 'lazy',
                              // B 站图有防盗链，必须去掉 Referer，否则一片灰
                              referrerPolicy: 'no-referrer',
                              onError: (e) => {
                                e.target.style.visibility = 'hidden';
                              },
                            })
                          : null,
                        h(
                          'div',
                          { style: { minWidth: 0, flex: '1 1 auto' } },
                          h('div', { className: 'bqtitle' }, v.title || v.bvid),
                          h(
                            'div',
                            { className: 'bqmeta' },
                            h('span', null, v.created ? new Date(v.created * 1000).toLocaleDateString('zh-CN') : ''),
                            h('span', null, v.duration
                              ? `${Math.floor(v.duration / 60)}m ${String(v.duration % 60).padStart(2, '0')}s`
                              : ''),
                            h('span', null, v.width && v.height ? `${v.width}x${v.height}` : ''),
                            h('span', { className: 'bqchip' }, v.vertical ? T.verticalTag : T.horizontalTag),
                            v.uploaded ? h('span', { className: 'bqchip bqchipOk' }, T.uploadedTag) : null,
                            // 远端核对结果（有快照时才显示）：绿色=夸克上确实有，橙色=夸克上缺
                            v.remoteChecked
                              ? h(
                                  'span',
                                  { className: v.remotePresent ? 'bqchip bqchipOk' : 'bqchip bqchipH' },
                                  v.remotePresent ? T.remotePresentTag : T.remoteMissingTag,
                                )
                              : null,
                            h('span', { className: 'bqchip' }, v.bvid),
                          ),
                          // 多分集视频：下拉挑具体哪一集（挑了就视为要下这一集）
                          v.pageCount > 1
                            ? h(
                                'div',
                                { style: { display: 'flex', alignItems: 'center', gap: '6px', marginTop: '4px', flexWrap: 'wrap' } },
                                h('span', { style: S.muted }, `${T.partLabel}（共 ${v.pageCount} 集）`),
                                h(
                                  'select',
                                  {
                                    className: 'bqbtn',
                                    value: String(sel.get(v.bvid) || v.statePage || 1),
                                    onChange: (e) => setItemPage(v.bvid, Number(e.target.value) || 1),
                                  },
                                  v.pages.map((p) =>
                                    h(
                                      'option',
                                      { key: p.cid, value: String(p.page) },
                                      `P${p.page}${p.part ? ' ' + p.part.slice(0, 28) : ''}`,
                                    ),
                                  ),
                                ),
                              )
                            : null,
                        ),
                      ),
                    ),
                  ),
              h(
                'div',
                { className: 'bqpager' },
                button('← ' + T.prev, () => setPage(Math.max(1, pg - 1)), pg <= 1 ? { disabled: true } : null),
                h('span', { style: S.muted }, `第 ${pg} / ${pageCount} 页`),
                button(T.next + ' →', () => setPage(Math.min(pageCount, pg + 1)), pg >= pageCount ? { disabled: true } : null),
              ),
            ),
          );
        }
        } // end if (showPicker)

        children.push(
          h(
            'div',
            { key: 'card', style: S.card },
            h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' } },
              h(
                'span',
                { style: S.tag },
                h('span', { style: Object.assign({}, S.dot, { background: statusColor(active.state) }) }),
                statusText(active.state),
              ),
              active.alive ? h('span', { style: S.muted }, `P/${active.pid}`) : null,
              active.dirName ? h('span', { style: S.muted }, `${T.dir}：${active.dirName}`) : null,
              active.jobId ? h('span', { style: S.muted }, active.jobId) : null,
            ),
            h(
              'div',
              { style: S.grid },
              h('div', null, h('div', { style: S.statLabel }, T.colDone), h('div', { style: S.statValue }, `${active.done}/${active.total}`)),
              h('div', null, h('div', { style: S.statLabel }, T.colFail), h('div', { style: S.statValue }, String(active.fail))),
              h('div', null, h('div', { style: S.statLabel }, T.videos), h('div', { style: S.statValue }, `${active.videoVertical}/${active.videoTotal}`)),
              h('div', null, h('div', { style: S.statLabel }, T.colLocal), h('div', { style: S.statValue }, fmtBytes(active.localBytes))),
            ),
            h('div', { style: S.bar }, h('div', { style: Object.assign({}, S.barFill, { width: active.percent + '%' }) })),
            h('div', { style: Object.assign({}, S.muted, { marginTop: '6px' }) },
              `${active.percent}%` +
              (active.current && active.current.length ? `　在途 ${active.current.length} 条` : '') +
              (active.updated ? `　更新于 ${active.updated}` : '')),
            active.lastError ? h('div', { style: S.err }, active.lastError) : null,
          ),
        );

        if (active.current && active.current.length) {
          children.push(
            h(
              'div',
              { key: 'cur', style: S.card },
              h('div', { style: S.statLabel }, `${T.runningItem}（${active.current.length}）`),
              active.current.map((c, i) => {
                const pct = Math.max(0, Math.min(100, Number(c.percent) || 0));
                const totalB = Number(c.total_bytes) || 0;
                const doneB = Number(c.done_bytes) || 0;
                const speed = Number(c.speed) || 0;
                const bits = [];
                if (totalB > 0) bits.push(`${fmtBytes(doneB)} / ${fmtBytes(totalB)}`);
                else if (doneB > 0) bits.push(fmtBytes(doneB));
                if (speed > 0) bits.push(`${fmtBytes(speed)}/s`);
                const overall = Number(c.overall);
                if (Number.isFinite(overall)) bits.push(`本条整体 ${overall}%`);
                return h(
                  'div',
                  {
                    key: (c.bvid || '') + i,
                    style: { padding: '8px 0', borderTop: '1px solid var(--dsw-alias-border-l1)' },
                  },
                  h(
                    'div',
                    { style: { display: 'flex', gap: '10px', alignItems: 'baseline' } },
                    h('span', { style: S.bvid }, c.bvid || ''),
                    h(
                      'span',
                      { style: { flex: '1 1 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                      c.title || '',
                    ),
                    h('span', { style: S.muted }, (STAGE[c.stage] || c.stage || '') + (c.detail ? `·${c.detail}` : '')),
                    h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, pct.toFixed(0) + '%'),
                  ),
                  // 当前这条的下载/上传进度条
                  h('div', { style: S.bar }, h('div', { style: Object.assign({}, S.barFill, { width: pct + '%' }) })),
                  bits.length ? h('div', { style: Object.assign({}, S.muted, { marginTop: '4px' }) }, bits.join('　')) : null,
                );
              }),
            ),
          );
        }

        if (data && data.logTail) {
          children.push(
            h(
              'div',
              { key: 'log', style: S.card },
              h('div', { style: S.statLabel }, T.log),
              h('pre', { style: S.pre }, data.logTail),
            ),
          );
        }
      }

      return h('div', { style: S.page }, children);
    }

    // ---------------------------------------------------------------- 注册

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        const PANEL_ID = 'bili-quark-pipeline';
        const NS = 'bili-quark-pipeline';

        // 字典走宿主 locale 服务；取不到时就退回上面内置的中文 T，保证面板仍可用
        let t = null;
        try {
          ctx.effect(
            () =>
              ctx.locale.register(NS, {
                zh: {
                  panel: T.panel,
                  title: T.title,
                  intro: T.intro,
                  refresh: T.refresh,
                  verify: T.verify,
                  dryRun: T.dryRun,
                  start: T.start,
                  stop: T.stop,
                  stopConfirm: T.confirmStop,
                },
                en: {
                  panel: 'Bilibili Quark Transform',
                  title: 'Bilibili Quark Transform',
                  intro: 'Batch-download Bilibili videos (resolution / part selectable) and upload them to Quark Drive',
                  refresh: 'Refresh',
                  verify: 'Verify remote',
                  dryRun: 'Dry run',
                  start: 'Start',
                  stop: 'Stop',
                  stopConfirm: 'Click again to stop',
                },
              }),
            'bili-quark: dictionaries',
          );
          if (typeof ctx.locale.bind === 'function') t = ctx.locale.bind(NS);
        } catch {
          t = null;
        }
        const tr = (key, fallback) => {
          if (!t) return fallback;
          try {
            const v = t(key);
            return v && v !== key ? v : fallback;
          } catch {
            return fallback;
          }
        };

        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            {
              name: 'sidebar.panellist',
              id: PANEL_ID,
              order: 60,
              // thunk：切换语言时无需重新注册
              label: () => tr('panel', T.panel),
              locale: NS,
            },
            PanelIcon,
          ),
        );

        // 面板样式（含按钮的 hover/active 与官方语义色令牌），随插件卸载移除
        ctx.effect(ensurePanelStyle, 'bili-quark: panel css');

        ctx.slots.inject('main', () =>
          ctx.slots.register(
            {
              name: 'main',
              key: PANEL_ID,
              locale: NS,
            },
            PanelPage,
          ),
        );
      },
    };
  },
});
