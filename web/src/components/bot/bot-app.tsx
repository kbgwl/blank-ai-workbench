"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  archiveThreads,
  deleteThread,
  updateThread,
  answerTask,
  decideApproval,
  errorCode,
  errorMessage,
  fetchActiveTasks,
  fetchEvidence,
  fetchScenarios,
  fetchTask,
  fetchThreadGoal,
  forkThread,
  steerTask,
  type AnswerValues,
  type ChangeItem,
  type Evidence,
  type PendingApproval,
  type PendingQuestion,
  type Project,
  type Provider,
  type ScenarioInfo,
  type Task,
  type Thread,
  type ThreadGoal,
} from "../tasks/api";
import { sendBackChoices } from "../tasks/approval-copy";
import { decisionInput, type ApprovalDecision } from "../tasks/approval-payload";
import { fetchAgentSettings } from "../settings/api";
import { fetchSkillLibrary, type SkillLibraryItem } from "../skills/api";
import { suggestSkill } from "@/lib/skill-suggest";
import { useTaskEvents } from "../tasks/use-task-events";
import { NEW_CHAT_EVENT } from "@/lib/commands";
import { canPickDirectory, pickDirectory } from "@/lib/desktop";
import { readJson, readString, removeString, writeJson, writeString } from "@/lib/safe-storage";
import { activateWorkspace, bindWorkspace, revealWorkspacePath, updateProject } from "../projects/api";
import { DirectoryPicker } from "../projects/directory-picker";
import { loadBoot, loadConversation, loadConversations, sendMessage, stopTask, type BotTurn, type Conversation } from "./bot-api";
import { summarizeBatchResult } from "./batch-archive";
import { BackgroundList } from "./background-list";
import { backgroundRowsFor, type BackgroundRow } from "./background";
import { Composer } from "./composer";
import { FirstScreen } from "./first-screen";
import { GoalStatusBar } from "./goal-status-bar";
import type { GoalStatusBarData } from "./goal-status";
import { defaultModelLabel, usableProviderId } from "./provider-choice";
import { configuredProviders } from "../configured-providers";
import { TaskPanel } from "./panel";
import { Sidebar } from "./sidebar";
import { backgroundTaskTarget, visibleBackgroundTasks } from "./background";
import { TurnView } from "./turn";
import { PendingPanel } from "./pending-panel";
import { buildSteps, isTurnRunning, planOf, quoteDraft, touchedFiles, turnReply } from "./turns";
import {
  MAX_ATTACHMENTS_PER_TURN,
  MAX_TOTAL_IMAGE_BYTES,
  attachmentKind,
  attachmentRejection,
  prepareImage,
  previewUrl,
  releasePreview,
  uploadAttachment,
  type DraftAttachment,
} from "./uploads";

const PROVIDER_KEY = "blank.bot.provider";
const SCENARIO_KEY = "blank.bot.scenario";
const MEMORY_KEY = "blank.bot.memory";
const PERMISSION_KEY = "blank.bot.permission";
/** 侧栏收窄与结果面板开合是界面偏好，刷新/跨页都该保持，不用每次重新摆一遍。 */
const SIDEBAR_COLLAPSED_KEY = "blank.bot.sidebar-collapsed";
const PANEL_OPEN_KEY = "blank.bot.panel-open";

/** 最近用过的工作区按项目记在本地：跨会话保留，但绝不进库（它只是偏好，不是数据）。 */
const RECENT_WS_PREFIX = "blank.recent-workspaces.";

function readRecentWorkspaces(projectId: string): string[] {
  if (!projectId) return [];
  const parsed = readJson<unknown>(`${RECENT_WS_PREFIX}${projectId}`, []);
  return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
}

function rememberWorkspace(projectId: string, workspaceId: string): string[] {
  const next = [workspaceId, ...readRecentWorkspaces(projectId).filter((id) => id !== workspaceId)].slice(0, 5);
  writeJson(`${RECENT_WS_PREFIX}${projectId}`, next);
  return next;
}

let draftSeq = 0;

/** 建一个草稿条目。本地的缩略图只给图片用，文本与 PDF 显示文件条。 */
function entryFor(file: File, kind: DraftAttachment["kind"], error: string): DraftAttachment {
  draftSeq += 1;
  return {
    key: `att-${Date.now()}-${draftSeq}`,
    id: "",
    name: file.name || (kind === "image" ? "粘贴的图片" : "附件"),
    mime: file.type,
    size: file.size,
    preview: kind === "image" ? previewUrl(file) : "",
    uploading: !error,
    error,
    kind,
  };
}

export function BotApp() {
  const [boot, setBoot] = useState<{ projects: Project[]; providers: Provider[] } | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState("");
  const [providerId, setProviderId] = useState("");
  // 场景与记忆开关：与模型选择同一套持久化套路——localStorage 里存用户的选择，
  // 清单读不到时保持开发场景，行为与没有场景系统时完全一致。
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  // 产品默认是「日常」：新用户第一句话就是通用任务，不必先理解编程场景。
  // 存过的选择仍然优先；切到编程会写进 localStorage。
  const [scenarioKey, setScenarioKey] = useState("daily");
  const [memoryOptIn, setMemoryOptIn] = useState(false);
  // v0.10.1 §4：发送栏的权限档。默认「询问」（= 后端 default 档）。
  // 界面文案是「询问 / 完全访问 / 计划」，内部映射回既有四档枚举。
  const [permissionMode, setPermissionMode] = useState("default");

  const [threads, setThreads] = useState<Thread[] | null>(null);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  /** 已归档列表单独读取失败：活跃列表照常显示，只在侧栏已归档组位置报错。 */
  const [archivedError, setArchivedError] = useState(false);
  const [threadsNonce, setThreadsNonce] = useState(0);
  /** 后台任务：关窗后仍在跑的、停在审批上等人的、或者失败没处理的（v164 §2.4）。 */
  const [activeTasks, setActiveTasks] = useState<Task[]>([]);
  /** 顶部状态：当前会话的活跃目标、服务是否在线。 */
  // 带上 threadId 存：切会话时读回的值还没到，不能把上一条会话的目标显示到这一条。
  const [threadGoal, setThreadGoal] = useState<{ threadId: string; goal: ThreadGoal | null } | null>(null);
  const [serviceOnline, setServiceOnline] = useState(false);
  // 技能库：只在会话页用来做"这一轮像不像某条技能的活"的提示，拿不到就不提示。
  const [skillItems, setSkillItems] = useState<SkillLibraryItem[]>([]);

  const [conv, setConv] = useState<Conversation | null>(null);
  const [convError, setConvError] = useState<string | null>(null);
  const [loadingConv, setLoadingConv] = useState(false);

  /** 当前要盯住的那一轮。它决定 SSE 连谁、右侧面板显示谁。 */
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [liveTask, setLiveTask] = useState<Task | null>(null);

  const [draft, setDraft] = useState("");
  /** 每加一，输入框就把焦点收回并落到末尾。引用一段话、点一条建议之后用。 */
  const [focusToken, setFocusToken] = useState(0);
  /** 后台任务清单是否展开。默认收起：只有一条在跑时它不值得常驻三行。 */
  const [backgroundOpen, setBackgroundOpen] = useState(false);
  /** 草稿里已选/已上传的图片。发送成功后清空。 */
  const [images, setImages] = useState<DraftAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [composerHint, setComposerHint] = useState<string | null>(null);

  // 默认展开会话列表，截图里进去就能看见历史任务；侧栏顶部仍可一键收窄成 52px。
  // 收窄是功能不是默认值——高频的会话不该一进来就把入口藏起来让人找。
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  /** 浏览器环境的目录浏览器；桌面壳走原生目录框，不打开它。 */
  const [directoryPickerOpen, setDirectoryPickerOpen] = useState(false);
  /** 目录浏览器从当前目录开始，而不是每次都退回盘符列表。 */
  const [directoryPickerStart, setDirectoryPickerStart] = useState("");
  /**
   * 这个对话选中的目录。
   *
   * `null` = 新对话还没选；`undefined` = 打开已有对话，跟随项目当前目录。
   * 新对话不继承项目的活动目录——用户明确选过之后，这个对话才算有目录。
   */
  const [conversationWorkspaceId, setConversationWorkspaceId] = useState<
    string | null | undefined
  >(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [threadChanges, setThreadChanges] = useState<ChangeItem[]>([]);
  const threadTurnKey = conv?.turns.map((turn) => turn.id).join("|") ?? "";

  useEffect(() => {
    let cancelled = false;
    const ids = threadTurnKey ? threadTurnKey.split("|") : [];
    if (ids.length === 0) return;
    void Promise.all(
      ids.map(async (taskId) => {
        try {
          const item = await fetchEvidence(taskId);
          return item.changes;
        } catch {
          return [];
        }
      }),
    ).then((groups) => {
      if (cancelled) return;
      const merged = new Map<string, ChangeItem>();
      for (const group of groups) {
        for (const change of group) {
          const existing = merged.get(change.path);
          if (!existing || (!existing.diff && change.diff)) merged.set(change.path, change);
        }
      }
      setThreadChanges([...merged.values()]);
    });
    return () => {
      cancelled = true;
    };
  }, [threadTurnKey]);

  /**
   * 清空草稿里的图并释放预览地址。
   *
   * 不清 blob 的话，每发一次带图的消息都会漏掉一份图片内存——单张看不出来，
   * 聊二十轮之后就是几十兆。这个函数定义在 startNew 之前，因为"新对话"要用它。
   */
  const clearImages = useCallback(() => {
    images.forEach((item) => releasePreview(item.preview));
    setImages([]);
  }, [images]);

  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [showJump, setShowJump] = useState(false);
  /** 每个会话上次读到哪。切走再切回来就回到原处，而不是被拽回最新一句。 */
  const scrollMemory = useRef<Map<string, number>>(new Map());
  const currentThreadId = useRef("");
  /** 正在等某条会话加载完、好把滚动位置放回去。 */
  const pendingScroll = useRef<{ threadId: string; top: number } | null>(null);
  /** 插话正在提交：挡住连按回车造成的重复排队（见 steer）。 */
  const steerBusy = useRef(false);

  /** 当前项目与它绑定的目录。工作区是"在哪个目录里干活"，是项目的一个属性。 */
  const currentProject = boot?.projects.find((project) => project.id === projectId) ?? null;
  // 最近切过的排前面：一个项目往往绑两三个目录，常用的那一个应该在第一行，
  // 而不是每次都在列表里找。原始顺序仍然保留（没有记录时就是它）。
  // 记录变动用一个版本号触发重算，而不是把 localStorage 塞进 state：
  // 在 effect 里 setState 会级联渲染，而这里真正变的只有"排序依据"。
  const [recentVersion, setRecentVersion] = useState(0);
  const workspaces = useMemo(() => {
    const all = currentProject?.workspaces ?? [];
    const recent = readRecentWorkspaces(projectId);
    if (recent.length === 0) return all;
    const rank = (id: string) => {
      const index = recent.indexOf(id);
      return index < 0 ? recent.length : index;
    };
    return [...all].sort((left, right) => rank(left.id) - rank(right.id));
    // recentVersion 只用来在"刚切过"之后重算一次，不参与计算。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.workspaces, projectId, recentVersion]);
  const nativeDirectoryPicker = canPickDirectory();
  // 桌面壳用原生目录框；网页环境走后端目录浏览器。两条路都能选目录。
  const canPickWorkspace = true;
  /** 项目和目录是否已读回来。首屏为 false，此时"没有目录"只是还没读到。 */
  const workspaceReady = boot !== null;
  /** 项目当前在用的目录。它只是选择框的起点，不代表新对话已经选中了目录。 */
  const projectActiveWorkspace =
    workspaces.find((item) => item.is_active) ?? workspaces[0] ?? null;
  const activeWorkspace =
    conversationWorkspaceId === undefined
      ? projectActiveWorkspace
      : conversationWorkspaceId
        ? (workspaces.find((item) => item.id === conversationWorkspaceId) ?? null)
        : null;

  const changeScenario = useCallback(
    (key: string) => {
      setScenarioKey(key);
      // 换到非日常场景时记忆开关一起收起（后端本来就不认非日常的 opt_in，
      // 前端把状态也复位，界面和真实行为保持一致）。
      const scenario = scenarios.find((item) => item.key === key);
      if (!scenario?.memoryAllowed) setMemoryOptIn(false);
      writeString(SCENARIO_KEY, key);
      writeString(MEMORY_KEY, memoryOptIn && scenario?.memoryAllowed ? "on" : "off");
    },
    [scenarios, memoryOptIn],
  );

  // ── 启动：项目与供应商 ────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    loadBoot()
      .then((data) => {
        if (!alive) return;
        setBoot(data);
        setProjectId((current) => current || data.projects[0]?.id || "");
        // 存过的模型选择在这里读：不在 useState 初始化里碰 localStorage，
        // 否则服务端渲染出空值、客户端渲染出选中值，hydration 会报不一致。
        const saved = usableProviderId(
          readString(PROVIDER_KEY),
          configuredProviders(data.providers).map((provider) => provider.id),
        );
        if (saved) {
          setProviderId(saved);
        } else {
          removeString(PROVIDER_KEY);
        }
        const savedScenario = readString(SCENARIO_KEY);
        if (savedScenario) setScenarioKey(savedScenario);
        setMemoryOptIn(readString(MEMORY_KEY) === "on");
        const savedPermission = readString(PERMISSION_KEY);
        if (["default", "accept_edits", "plan", "bypass"].includes(savedPermission)) {
          setPermissionMode(savedPermission);
        }
        // 界面偏好跟着启动数据一起读回：放在这里而不是 useState 初始化，
        // 是为了让服务端首帧与客户端首帧一致，避免水合不一致。
        setSidebarCollapsed(readString(SIDEBAR_COLLAPSED_KEY) === "1");
        setPanelOpen(readString(PANEL_OPEN_KEY) !== "0");
      })
      .catch((error: unknown) => {
        if (alive) setBootError(errorMessage(error));
      });
    // 场景清单单独拉：失败不阻塞首屏，选择器隐藏即可。
    fetchScenarios()
      .then((rows) => {
        if (alive) setScenarios(rows);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  // ── 会话列表 ─────────────────────────────────────────────────────────
  useEffect(() => {
    // 会话列表是全局的，不再依赖 projectId。之前这里有一句 `if (!projectId) return`，
    // 首帧 projectId 还没就绪就跳过，而依赖里又没有它 → 永远不重跑，左栏一直停在骨架。
    let alive = true;
    loadConversations()
      .then(({ threads: rows, archivedError: archivedFailed }) => {
        if (!alive) return;
        setThreads(rows);
        setThreadsError(null);
        setArchivedError(archivedFailed);
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setThreads([]);
        setThreadsError(`会话列表读取失败：${errorMessage(error)}`);
        setArchivedError(false);
      });
    return () => {
      alive = false;
    };
  }, [threadsNonce]);

  // ── 后台任务：关窗之后还在动的那几条 ───────────────────────────────
  //
  // 窗口关掉不代表任务结束，后端 worker 会继续跑；任务也可能卡在审批上等人。
  // 界面每次打开都要重新问一遍"还有哪些没结束"，否则用户重开客户端看到的是
  // 一片空，以为活丢了——而它其实还在后台改文件。
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    const pull = () =>
      fetchActiveTasks(projectId)
        .then((rows) => {
          if (alive) setActiveTasks(rows);
        })
        // 读不到就不显示：这是恢复入口，不是首屏关键路径，不该弹错误打断用户。
        .catch(() => undefined);
    pull();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") pull();
    }, 20000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // threadsNonce / activeTaskId：刚发出去一条、或换了一条时立刻刷新一次，
    // 不等那 20 秒的轮询。
  }, [projectId, threadsNonce, activeTaskId]);

  // ── 顶部状态条：服务是否在线 ─────────────────────────────────────────
  // 这里只当探针用：能读到设置说明后端活着，读不到就在状态条上如实说断线。
  // 20 秒轮询兜住「设置页在另一个窗口」的情况，focus 立刻补一次。
  useEffect(() => {
    let alive = true;
    const pull = () =>
      fetchAgentSettings()
        .then(() => {
          if (alive) setServiceOnline(true);
        })
        .catch(() => {
          if (alive) setServiceOnline(false);
        });
    pull();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") pull();
    }, 20000);
    window.addEventListener("focus", pull);
    return () => {
      alive = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", pull);
    };
  }, []);

  // 技能库只在会话页做提示用：拉不到就静默不提示，不影响任何主流程。
  useEffect(() => {
    let alive = true;
    fetchSkillLibrary()
      .then((library) => {
        if (alive) setSkillItems(library.items);
      })
      .catch(() => {
        // 技能库不可用时本来就不该有提示，无需报错打扰用户。
      });
    return () => {
      alive = false;
    };
  }, []);

  // 这一轮已经用过的技能：模型自己加载过的，不再提示"你可以用"。
  const usedSkillsByTurn = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const turn of conv?.turns ?? []) {
      const names = (turn.events ?? [])
        .filter((event) => event.type === "skill.invoked")
        .map((event) => String(event.payload?.skill ?? ""))
        .filter(Boolean);
      map.set(turn.id, [...new Set(names)]);
    }
    return map;
  }, [conv]);
  const turnSkills = useCallback((id: string) => usedSkillsByTurn.get(id) ?? [], [usedSkillsByTurn]);

  // ── 当前轮的事件流 ────────────────────────────────────────────────────
  const activeTurn = conv?.turns.find((turn) => turn.id === activeTaskId) ?? null;
  const liveStatus = liveTask?.id === activeTaskId ? liveTask.status : (activeTurn?.status ?? "");
  const live = useTaskEvents(activeTaskId, activeTaskId ? liveStatus : null);
  const running = Boolean(activeTaskId) && isTurnRunning(liveStatus, live.events);

  // ── 顶部状态条：当前会话的活跃目标 ────────────────────────────────────
  // 目标由服务端 GoalTracker 维护，多轮补充要求会挂回同一个目标。任务状态一变
  // 就补拉一次，否则「执行中 → 等你批准」这种最需要看见的变化要等 8 秒。
  useEffect(() => {
    const threadId = conv?.thread.id ?? "";
    if (!threadId) return;
    let alive = true;
    const pull = () =>
      fetchThreadGoal(threadId)
        .then((goal) => {
          if (alive) setThreadGoal({ threadId, goal });
        })
        // 老后端没有这个端点时不显示目标，不能把整页打红。
        .catch(() => undefined);
    pull();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") pull();
    }, 8000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [conv?.thread.id, threadsNonce, activeTaskId, liveStatus]);

  // 任务从跑到停：把状态补一次。事件里已经有终态事件，但任务的 status 与失败原因
  // 只有任务详情才有——只靠事件会漏掉"被策略拒绝"这类没有终态事件的情况。
  useEffect(() => {
    if (!activeTaskId || running) return;
    let alive = true;
    fetchTask(activeTaskId)
      .then((task) => {
        if (alive && task) setLiveTask(task);
      })
      .catch(() => undefined);
    // 结束的一轮也把证据取一次：右侧面板的 diff 来自这里，不取的话历史轮次的
    // 文件行只有路径没有 diff，点开是空的——看起来就是"点了没反应"。
    fetchEvidence(activeTaskId)
      .then((next) => {
        if (alive && next) setEvidence(next);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [activeTaskId, running]);

  // 跑着的时候也要定期核对任务状态。
  //
  // 事件流只带时间线，不带任务状态；而"停"的判据以前完全依赖终态事件。一旦
  // 状态在别处被改（用户点了停止、队列把它标成失败、后端重启），就没有人会
  // 补那条事件，界面会永久停在"进行中"：输入被当成插话、全部打回 409。
  // 4 秒一次很便宜——一次任务通常几十秒，多这十几次请求换来的是"永远不会卡住"。
  useEffect(() => {
    if (!activeTaskId || !running) return;
    let alive = true;
    const timer = window.setInterval(() => {
      fetchTask(activeTaskId)
        .then((task) => {
          if (alive && task) setLiveTask(task);
        })
        .catch(() => undefined);
    }, 4000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [activeTaskId, running]);

  // 待审批 / 待回答：只有跑着的时候才有意义，且跟着事件推进刷新。
  useEffect(() => {
    if (!activeTaskId || !running) return;
    let alive = true;
    const timer = setTimeout(() => {
      fetchEvidence(activeTaskId)
        .then((next) => {
          if (alive) setEvidence(next);
        })
        .catch(() => undefined);
    }, 350);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [activeTaskId, running, live.revision]);

  // 等审批期间定期补拉一次证据。
  //
  // 审批卡是"停在某一刻"等来的：它出现的那一刻，未必和上面那次拉取重合。上面的
  // effect 只在 running 变化或事件 revision 变化时跑，而等审批恰恰是"没有新事件"
  // 的状态——于是错过那一次，页面就永远停在「等你批准」却没有任何卡片，用户既
  // 看不到要批什么，也没有按钮。实测重开一条等审批的会话就是这样：正文只剩一行
  // 「在翻代码 44 分」，而卡片从未出现。
  //
  // 2.5 秒一次很便宜（一次 GET），换来的是"卡片一定会出现"，以及过期能被看见。
  useEffect(() => {
    if (!activeTaskId || !running) return;
    if (liveStatus !== "waiting_approval") return;
    let alive = true;
    const pull = () => {
      fetchEvidence(activeTaskId)
        .then((next) => {
          if (alive && next) setEvidence(next);
        })
        .catch(() => undefined);
    };
    pull();
    const timer = window.setInterval(pull, 2500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [activeTaskId, running, liveStatus]);

  // ── 滚动：贴着底部，除非用户自己往上翻 ─────────────────────────────────
  useEffect(() => {
    const node = scroller.current;
    if (!node || !stick.current) return;
    // 等这一帧布局落定再滚：在 effect 里直接读 scrollHeight 拿到的是"变高之前"
    // 的高度，于是打开一条等审批的会话时，最需要看见的批准按钮正好停在视口外。
    const frame = requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
    // 审批卡出现 / 消失也要跟着贴底：它不是新事件，是一条要你动手的东西。
  }, [live.revision, live.streaming, conv?.turns.length, evidence?.pendingApprovals?.length, loadingConv]);

  const onScroll = useCallback(() => {
    const node = scroller.current;
    if (!node) return;
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
    stick.current = atBottom;
    // 只在"贴底 / 离开底部"切换时 setState，避免每一帧滚动都触发重渲染。
    setShowJump((current) => (current === !atBottom ? current : !atBottom));
  }, []);

  // 记住当前打开的会话 id，供 onScroll 记录位置时使用（onScroll 不该依赖 conv）。
  useEffect(() => {
    currentThreadId.current = conv?.thread.id ?? "";
  }, [conv?.thread.id]);

  // 切回一条读过的会话：等内容渲染完，把滚动位置放回离开时的地方。
  // 原来在底部附近就继续贴底，避免"明明看的是最新消息却被丢回中间"。
  useEffect(() => {
    const node = scroller.current;
    const pending = pendingScroll.current;
    if (!node || !pending || conv?.thread.id !== pending.threadId) return;
    pendingScroll.current = null;
    const frame = requestAnimationFrame(() => {
      const max = Math.max(0, node.scrollHeight - node.clientHeight);
      const top = Math.min(Math.max(0, pending.top), max);
      node.scrollTop = top;
      const atBottom = max - top < 80;
      stick.current = atBottom;
      setShowJump(!atBottom && max > 200);
    });
    return () => cancelAnimationFrame(frame);
  }, [conv]);

  const jumpToBottom = useCallback(() => {
    const node = scroller.current;
    if (!node) return;
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
    stick.current = true;
    setShowJump(false);
  }, []);

  // ── 打开一条会话 ──────────────────────────────────────────────────────
  const openThread = useCallback((threadId: string) => {
    // 离开前先把当前位置记在"离开的那条"名下：等新会话内容一渲染，
    // 浏览器会把 scrollTop 夹到新内容的范围内并触发滚动事件，那时再记已经晚了。
    const leaving = currentThreadId.current;
    if (leaving && leaving !== threadId && scroller.current) {
      scrollMemory.current.set(leaving, scroller.current.scrollTop);
    }
    // 读过的会话回到原处；没读过的（或新会话）照旧贴底，先看最新一句。
    const remembered = scrollMemory.current.get(threadId);
    pendingScroll.current = remembered === undefined ? null : { threadId, top: remembered };
    stick.current = remembered === undefined;
    setShowJump(false);
    setConvError(null);
    setLoadingConv(true);
    setEvidence(null);
    setThreadChanges([]);
    setActiveTaskId(null);
    setLiveTask(null);
    // 地址栏跟着走：刷新页面能回到同一条对话，也能把链接直接发给别人。
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("thread", threadId);
      window.history.replaceState(null, "", url);
    } catch {
      // 静态导出下也有可能拿不到完整 URL，忽略即可，不影响打开。
    }
    loadConversation(threadId)
      .then((next) => {
        if (!next) {
          setConvError("后端没有返回这条对话");
          return;
        }
        setConv(next);
        // 已有对话跟随项目当前目录；新对话的“未选择”不会带到这里。
        setConversationWorkspaceId(undefined);
        const last = next.turns[next.turns.length - 1];
        setActiveTaskId(last?.id ?? null);
      })
      .catch((error: unknown) => setConvError(errorMessage(error)))
      .finally(() => setLoadingConv(false));
  }, []);

  // 带 ?thread= 进来时直接打开那条对话（深链 / 刷新后回到原处）。
  useEffect(() => {
    if (!projectId) return;
    let target = "";
    try {
      target = new URLSearchParams(window.location.search).get("thread") ?? "";
    } catch {
      target = "";
    }
    if (!target) return;
    // 推迟一个 tick：openThread 上来就会置 loading 状态，而 effect 里同步 setState
    // 会多触发一轮渲染（这条 lint 规则挡的正是它）。放到异步回调里，
    // 就能和随后的请求结果合并成一次更新。
    const timer = setTimeout(() => openThread(target), 0);
    return () => clearTimeout(timer);
  }, [projectId, openThread]);

  // ── 重开窗口后回到没结束的那一轮 ──────────────────────────────────────
  //
  // 关窗时任务还在跑，重开是一张白纸的话，用户只能从左侧几十条里猜是哪一条。
  // 首屏没有指定会话时，自动打开最该被看到的那条：先等审批的（它正卡着等一个点击），
  // 再在跑的，再暂停的。只做一次——用户随后自己切走，不该被反复拽回来。
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current || conv || activeTasks.length === 0) return;
    let hasDeepLink = false;
    try {
      hasDeepLink = Boolean(new URLSearchParams(window.location.search).get("thread"));
    } catch {
      hasDeepLink = false;
    }
    if (hasDeepLink) return;
    const rank = (status: string) =>
      status === "waiting_approval" ? 0 : status === "running" ? 1 : status === "paused" ? 2 : 3;
    const target = activeTasks
      .filter((task) => task.threadId)
      .sort((left, right) => rank(left.status) - rank(right.status))[0];
    if (!target?.threadId) return;
    resumed.current = true;
    // 和上面的深链恢复同理：openThread 会同步置 loading，放到异步回调里，
    // 避免 effect 里直接触发一次 setState（react-hooks/set-state-in-effect）。
    const timer = setTimeout(() => openThread(target.threadId!), 0);
    return () => clearTimeout(timer);
  }, [activeTasks, conv, openThread]);

  const startNew = useCallback(() => {
    // 和 openThread 一样：先把上一条读到哪记下来，再清空。
    const leaving = currentThreadId.current;
    if (leaving && scroller.current) {
      scrollMemory.current.set(leaving, scroller.current.scrollTop);
    }
    setConv(null);
    setConvError(null);
    setActiveTaskId(null);
    setLiveTask(null);
    setEvidence(null);
    setThreadChanges([]);
    setSendError(null);
    setComposerHint(null);
    // 新对话从“没有目录”开始：项目里绑过什么，不自动算到这个对话头上。
    setConversationWorkspaceId(null);
    // 换会话/换项目时草稿里的图要一起清：它们属于上一个项目，跨项目带过去后端也认不出 id。
    clearImages();
    pendingScroll.current = null;
    stick.current = true;
    // 清掉地址栏里的会话 id：否则刷新一下又跳回刚才那条。
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("thread");
      window.history.replaceState(null, "", url);
    } catch {
      // 拿不到 URL 不影响清空。
    }
  }, [clearImages]);

  // 命令面板里的「新对话」是同路由动作，靠事件回来。
  useEffect(() => {
    const onNew = () => startNew();
    window.addEventListener(NEW_CHAT_EVENT, onNew);
    return () => window.removeEventListener(NEW_CHAT_EVENT, onNew);
  }, [startNew]);

  /** 结果面板开合用一次，同时记住偏好：刷新之后保持上次的样子。 */
  const setPanelOpenSaved = useCallback((next: boolean) => {
    setPanelOpen(next);
    writeString(PANEL_OPEN_KEY, next ? "1" : "0");
  }, []);

  // ── 工作区 ───────────────────────────────────────────────────────────
  /**
   * 只刷新项目列表。
   *
   * 不整个重跑 loadBoot：供应商那条请求没必要重发，而 providerId 是从 localStorage
   * 里读的，重跑一次 boot 会把用户当前选的模型冲回默认值。
   */
  const refreshProjects = useCallback(async () => {
    try {
      const next = await loadBoot();
      setBoot((current) => (current ? { ...current, projects: next.projects } : next));
    } catch (error: unknown) {
      setActionError(`项目列表刷新失败：${errorMessage(error)}`);
    }
  }, []);

  const activateWorkspaceById = useCallback(
    async (workspaceId: string) => {
      if (!projectId) return;
      setActionError(null);
      try {
        await activateWorkspace(projectId, workspaceId);
        // 这个对话从现在起才算有目录；首屏标题与发送闸门都看它。
        setConversationWorkspaceId(workspaceId);
        // 选完目录就把"还没选目录"的提示撤掉，别让红字留在输入框上。
        setSendError(null);
        rememberWorkspace(projectId, workspaceId);
        setRecentVersion((version) => version + 1);
        await refreshProjects();
        // 说清生效时机：正在跑的那一轮已经把根目录读进去了，切换只影响下一次启动。
        setComposerHint("已切换目录，下一轮在这个目录里执行。");
      } catch (error: unknown) {
        setActionError(errorMessage(error));
      }
    },
    [projectId, refreshProjects],
  );

  /**
   * 把目录选择框选中的目录绑定到当前项目并立刻切过去。
   *
   * 桌面壳用系统目录框，浏览器用后端目录浏览器；两条路都只返回已存在的目录，
   * 不再提供手填路径。
   */
  const bindAndActivateWorkspace = useCallback(
    async (path: string) => {
      if (!projectId) return;
      const picked = path.trim();
      if (!picked) return;
      setActionError(null);
      try {
        const bound = await bindWorkspace(projectId, { path: picked });
        // 刚选的目录就是用户想用的那个：绑完立刻切成活动，否则新绑的在界面上是灰的，
        // 用户会以为没成功（这也是旧版最难理解的一处）。
        await activateWorkspace(projectId, bound.id);
        setConversationWorkspaceId(bound.id);
        setSendError(null);
        rememberWorkspace(projectId, bound.id);
        setRecentVersion((version) => version + 1);
        await refreshProjects();
        // 只换目录，不动会话：Codex 也是 `-C` 换工作根，历史会话照旧在。
        // 只报目录名：完整主机路径不进默认界面，截图/投屏时才不会把目录结构带出去。
        setComposerHint(`已切换目录：${bound.label}（下一轮在这里执行，会话不变）`);
      } catch (error: unknown) {
        setActionError(errorMessage(error));
      }
    },
    [projectId, refreshProjects],
  );

  /** 分叉会话：带上上下文另开一条线，不复制任务（后端只记 forked_from）。 */
  const forkThreadById = useCallback(
    async (thread: Thread) => {
      setActionError(null);
      try {
        const created = await forkThread(thread.id);
        setThreadsNonce((value) => value + 1);
        await openThread(created.id);
        setComposerHint("已分叉，接着这条线继续说。");
      } catch (error: unknown) {
        setActionError(errorMessage(error));
      }
    },
    [openThread],
  );

  const pickWorkspace = useCallback(async () => {
    if (!projectId) return;
    setActionError(null);
    if (!nativeDirectoryPicker) {
      // 从当前目录开始：列表接口只给打码路径，这里按需单独取一次真实路径。
      // 新对话还没选目录时，用项目上次的目录当起点；取不到就退回盘符列表。
      const anchor = activeWorkspace ?? projectActiveWorkspace;
      let start = "";
      if (anchor) {
        try {
          start = await revealWorkspacePath(projectId, anchor.id);
        } catch {
          start = "";
        }
      }
      setDirectoryPickerStart(start);
      setDirectoryPickerOpen(true);
      return;
    }
    // 用户在系统对话框里点了"取消"会拿到空串——那是正常操作，不该报错。
    const picked = await pickDirectory();
    if (!picked) return;
    await bindAndActivateWorkspace(picked);
  }, [projectId, nativeDirectoryPicker, activeWorkspace, projectActiveWorkspace, bindAndActivateWorkspace]);

  // ── 图片 ─────────────────────────────────────────────────────────────
  /**
   * 选完就传，不等发送。
   *
   * 这样用户写字的十几秒里图已经在路上了，按回车时只剩一个 id 要带过去。
   * 相反的做法（发送时才传）会让"大图 + 慢网"变成一段按了回车什么都不发生的空白。
   */
  const addImages = useCallback(
    (incoming: File[]) => {
      if (!projectId || incoming.length === 0) return;
      setSendError(null);
      const room = Math.max(0, MAX_ATTACHMENTS_PER_TURN - images.length);
      if (room === 0) {
        setSendError(`一次最多 ${MAX_ATTACHMENTS_PER_TURN} 个附件`);
        return;
      }
      const picked = incoming.slice(0, room);
      if (incoming.length > room) {
        setSendError(`一次最多 ${MAX_ATTACHMENTS_PER_TURN} 个附件，多出来的没有加`);
      }

      void (async () => {
        // 图片先降采样再上传：手机截图 6MB 压到几百 KB，模型看到的信息量不变。
        const prepared = await Promise.all(picked.map((file) => prepareImage(file)));
        // 总量要按"压完之后"的字节算，否则刚压小的图会被按原图大小算进上限。
        let imageBytes = images.reduce(
          (sum, item) => (item.kind === "image" && !item.error ? sum + item.size : sum),
          0,
        );
        const entries: DraftAttachment[] = [];
        for (const file of prepared) {
          const kind = attachmentKind(file);
          const reason = attachmentRejection(file);
          if (reason || !kind) {
            entries.push(entryFor(file, kind ?? "text", reason ?? "不支持的附件"));
            continue;
          }
          if (kind === "image") {
            if (imageBytes + file.size > MAX_TOTAL_IMAGE_BYTES) {
              // 先在这里拦住，而不是发出去之后发现模型只看到一半。
              setSendError(
                `本轮图片合计超过 ${MAX_TOTAL_IMAGE_BYTES / (1024 * 1024)}MB，${file.name || "这张图"}没有加进来`,
              );
              continue;
            }
            imageBytes += file.size;
          }
          entries.push(entryFor(file, kind, ""));
        }

        setImages((current) => [...current, ...entries]);
        entries.forEach((entry, index) => {
          if (entry.error) return;
          uploadAttachment(projectId, prepared[index])
            .then((ref) =>
              setImages((current) =>
                current.map((item) =>
                  item.key === entry.key ? { ...item, id: ref.id, uploading: false } : item,
                ),
              ),
            )
            .catch((error: unknown) =>
              setImages((current) =>
                current.map((item) =>
                  item.key === entry.key
                    ? { ...item, uploading: false, error: errorMessage(error) }
                    : item,
                ),
              ),
            );
        });
      })();
    },
    [images, projectId],
  );

  /** 从草稿里去掉一个。预览地址要一起释放，否则那张图会一直留在内存里。 */
  const removeImage = useCallback(
    (key: string) => {
      const target = images.find((item) => item.key === key);
      if (target) releasePreview(target.preview);
      setImages((current) => current.filter((item) => item.key !== key));
    },
    [images],
  );

  // ── 发送 ─────────────────────────────────────────────────────────────
  const send = useCallback(async () => {
    const text = draft.trim();
    const ready = images.filter((item) => item.id && !item.uploading && !item.error);
    // 只有图没有字是合法的；两样都空就不发——否则会建出一条没有目标的空轮次。
    if ((!text && ready.length === 0) || sending || !projectId) return;
    // 没有目录也能发：这一轮走纯聊天，后端禁用文件/命令工具。
    // 只有选了目录，才把这一轮升级成可操作电脑。
    setSending(true);
    setSendError(null);
    setActionError(null);
    setComposerHint(null);
    try {
      const result = await sendMessage({
        projectId,
        threadId: conv?.thread.id ?? "",
        text,
        providerId,
        scenario: scenarioKey,
        memoryOptIn,
        permissionMode,
        // 这个对话没选目录 = 纯聊天；选了才允许操作电脑。
        chatOnly: !activeWorkspace,
        attachments: ready,
      });
      const created: BotTurn = {
        id: result.task.id,
        title: result.task.title,
        // 只发附件时 bubble 里也得有东西可显示，否则会是一个空白的用户消息。
        goal: text || `发来 ${ready.length} 个附件`,
        status: result.task.status,
        summary: "",
        createdAt: result.task.createdAt,
        attachments: ready,
        events: [],
        eventsLoaded: false,
      };
      setConv((current) =>
        current && current.thread.id === result.thread.id
          ? {
              thread: { ...current.thread, turnCount: current.thread.turnCount + 1 },
              turns: [...current.turns, created],
            }
          : { thread: { ...result.thread, turns: [] }, turns: [created] },
      );
      setActiveTaskId(result.task.id);
      setLiveTask(result.task);
      setEvidence(null);
      setDraft("");
      clearImages();
      stick.current = true;
      // 每次都刷新左侧：不只新会话要出现，老会话也要更新"几轮"和"最近活动时间"，
      // 否则左侧列表停在发消息之前的样子，看起来像没发出去。
      setThreadsNonce((value) => value + 1);
      // 地址栏换到这条会话：发完消息刷新页面应该还在原地，而不是回到空态。
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("thread", result.thread.id);
        window.history.replaceState(null, "", url);
      } catch {
        // 拿不到 URL 就算了，不影响这一轮继续跑。
      }
      if (result.runError) setSendError(`消息已经记下，但启动失败：${result.runError}`);
    } catch (error: unknown) {
      setSendError(errorMessage(error));
    } finally {
      setSending(false);
    }
  }, [clearImages, conv, draft, images, memoryOptIn, permissionMode, projectId, providerId, scenarioKey, sending, activeWorkspace]);

  const stop = useCallback(() => {
    if (!activeTaskId) return;
    setActionBusy(true);
    stopTask(activeTaskId)
      .catch((error: unknown) => setActionError(errorMessage(error)))
      .finally(() => {
        setActionBusy(false);
        // 取消完立刻把状态读回来：界面必须马上回到"可以再发一句"，
        // 而不是等 SSE 推下一条事件（任务卡住时那条可能永远不到）。
        fetchTask(activeTaskId)
          .then((task) => {
            if (task) setLiveTask(task);
          })
          .catch(() => undefined);
      });
  }, [activeTaskId]);

  /** 清单里点一行：有会话就回那条会话，没有就退到任务详情。 */
  const openBackgroundRow = useCallback(
    (row: BackgroundRow) => {
      if (row.target.kind === "thread") {
        openThread(row.target.threadId);
        setBackgroundOpen(false);
        return;
      }
      window.location.href = row.target.href;
    },
    [openThread],
  );

  /**
   * 停一条后台任务。
   *
   * 停完立刻重拉一次清单，不等 20 秒的轮询：点完「停止」还挂在清单上，
   * 用户会以为没停掉，然后再点一次——第二次撞的是 409。
   */
  const stopBackgroundRow = useCallback(
    (row: BackgroundRow) => {
      setActionBusy(true);
      stopTask(row.id)
        .catch((error: unknown) => setActionError(errorMessage(error)))
        .finally(() => {
          setActionBusy(false);
          if (!projectId) return;
          fetchActiveTasks(projectId)
            .then((rows) => setActiveTasks(rows))
            .catch(() => undefined);
        });
    },
    [projectId],
  );

  /**
   * 运行中插话。后端语义是"从下一次模型调用开始生效"，不撤回已经执行的动作——
   * 所以提示里写的是"已排队"，不写成"已改好"，否则用户会以为刚才那步被撤销了。
   */
  const steer = useCallback(() => {
    const text = draft.trim();
    // 连按两次回车会发两次：draft 是在 then 里才清空的，第二次按的时候还是原文，
    // 后端于是收到两条一模一样的插话，对话里就出现两段逐字相同的话。
    // 清空移到请求之前（失败再放回去），并用一个 in-flight 标记挡住重复提交。
    if (!text || !activeTaskId || steerBusy.current) return;
    steerBusy.current = true;
    setSendError(null);
    setComposerHint(null);
    setDraft("");
    steerTask(activeTaskId, text)
      .then((result) => {
        setComposerHint(result.queued ? "已经排上了，从它下一次想想的时候开始生效。" : "后端没有确认排队，这句话可能没生效。");
      })
      .catch((error: unknown) => {
        // 没发出去就把话放回输入框：输入框空了而什么都没发生，用户会以为自己没按。
        setDraft((current) => (current ? current : text));
        if (errorCode(error) !== "task_not_steerable") {
          setSendError(errorMessage(error));
          return;
        }
        // 这一轮其实已经结束了，只是界面状态慢了一拍（或者状态在别处被改过）。
        // 用户按回车是想说下一句，不是往一个停下的轮次里塞话——直接当新的一轮发出去。
        // 只留一句红字把他挡在门外，等于把"状态没同步"变成"这个功能废了"。
        //
        // 提示写在 send() 之后：send 一进去就会清掉上一条 hint，先设等于白设。
        void send().then(() => {
          setComposerHint("上一轮已经结束，这句话作为新的一轮发出。");
        });
      })
      .finally(() => {
        steerBusy.current = false;
      });
  }, [activeTaskId, draft, send]);

  const decide = useCallback(
    (approval: PendingApproval, request: ApprovalDecision) => {
      if (!activeTaskId) return;
      setActionBusy(true);
      setActionError(null);
      // 卡的身份 + 决定载荷合成一次入参。对话页这边没有 Block，所以身份只取
      // PendingApproval 与 Block 共有的那几个字段（见 approval-payload.ts）。
      decideApproval(
        activeTaskId,
        decisionInput({
          ...request,
          target: {
            approvalId: approval.approvalId,
            toolCallId: approval.toolCallId,
            argumentsHash: approval.argumentsHash,
            toolName: approval.toolName,
          },
        }),
      )
        .then(() => {
          setEvidence(null);
          live.reload();
        })
        .catch((error: unknown) => setActionError(errorMessage(error)))
        .finally(() => setActionBusy(false));
    },
    [activeTaskId, live],
  );

  const answer = useCallback(
    (questions: PendingQuestion[], answers: AnswerValues) => {
      if (!activeTaskId) return;
      setActionBusy(true);
      setActionError(null);
      // 只有真的填了答案的问题才提交：把空串一起发回去，等于替用户回答了"空"。
      // 多选题的数组里也可能带空串，同样先滤再判空。
      const payload: AnswerValues = {};
      for (const question of questions) {
        const raw = answers[question.id];
        const value = Array.isArray(raw)
          ? raw.filter((item) => item.trim().length > 0)
          : (raw ?? "").trim();
        if (Array.isArray(value) ? value.length > 0 : value) payload[question.id] = value;
      }
      answerTask(activeTaskId, payload)
        .then(() => {
          setEvidence(null);
          live.reload();
        })
        .catch((error: unknown) => setActionError(errorMessage(error)))
        .finally(() => setActionBusy(false));
    },
    [activeTaskId, live],
  );

  const onDelete = useCallback(
    (thread: Thread) => {
      const ok = window.confirm('删除「' + thread.title + '」？删除后不可恢复！');
      if (!ok) return;
      deleteThread(thread.id)
        .then(() => {
          if (conv?.thread.id === thread.id) startNew();
          setThreadsNonce((value) => value + 1);
        })
        .catch((error: unknown) => setActionError(errorMessage(error)));
    },
    [conv?.thread.id, startNew],
  );

  const archive = useCallback(
    (thread: Thread) => {
      const ok = window.confirm(`归档「${thread.title}」？\n\n它不再接受新的一轮，已有的轮次全部保留。`);
      if (!ok) return;
      updateThread(thread.id, { status: "archived" })
        .then(() => {
          if (conv?.thread.id === thread.id) startNew();
          setThreadsNonce((value) => value + 1);
        })
        .catch((error: unknown) => setActionError(errorMessage(error)));
    },
    [conv?.thread.id, startNew],
  );

  /**
   * 批量归档：Sidebar 已经做过一次整批确认，这里只发一个请求。
   *
   * 失败必须把错误抛回去：Sidebar 靠它决定"留在管理态、保留选择"——
   * 在这里吞掉错误会让一次失败看起来像成功，选择也被清空。
   */
  const openThreadId = conv?.thread.id ?? "";
  const batchArchive = useCallback(
    async (items: Thread[]) => {
      const ids = items.map((thread) => thread.id);
      setActionError(null);
      try {
        const result = await archiveThreads(ids);
        // 当前正打开的这一条也在归档列表里：切到新对话，否则输入框对着一条
        // 已归档会话，下一句一定 409。
        if (openThreadId && ids.includes(openThreadId)) startNew();
        setThreadsNonce((value) => value + 1);
        setSendError(null);
        // startNew 会清提示，所以成功文案放在它之后。
        setComposerHint(summarizeBatchResult(result));
      } catch (error: unknown) {
        setActionError(errorMessage(error));
        throw error;
      }
    },
    [openThreadId, startNew],
  );


  const rename = useCallback(
    (thread: Thread) => {
      const next = window.prompt('新标题', thread.title);
      if (!next || next.trim() === '' || next.trim() === thread.title) return;
      updateThread(thread.id, { title: next.trim() })
        .then(() => setThreadsNonce((value) => value + 1))
        .catch((error: unknown) => setActionError(errorMessage(error)));
    },
    [],
  );

  const unarchive = useCallback(
    (thread: Thread) => {
      updateThread(thread.id, { status: 'active' })
        .then(() => setThreadsNonce((value) => value + 1))
        .catch((error: unknown) => setActionError(errorMessage(error)));
    },
    [],
  );
  const retry = useCallback(() => {
    const last = conv?.turns[conv.turns.length - 1];
    if (!last) return;
    setDraft(last.goal);
    // 重试是"把同一句话再发一次"，不是"我自己改一改再发"。光标落进输入框，
    // 用户能就地改一个字重发，也能直接按回车；两条路都不用再点一下输入框。
    setFocusToken((value) => value + 1);
  }, [conv]);

  // ── 渲染数据 ─────────────────────────────────────────────────────────
  const currentTurnView = useMemo(() => {
    if (!conv) return [];
    return conv.turns.map((turn) => {
      const isLive = turn.id === activeTaskId;
      const events = isLive && live.events.length > 0 ? live.events : turn.events;
      const steps = buildSteps(events);
      const plan = planOf(events);
      const verdict = turnReply(events);
      const status = isLive ? liveStatus || turn.status : turn.status;
      const isRunning = isLive ? running : false;
      const approvals = isLive && running ? (evidence?.pendingApprovals ?? []) : [];
      // 审批全过期了，就不能再写「等你批准」：那个徽标在叫用户点一个必然 409 的按钮。
      // 任务本身还要等巡检收尾（最多一拍），这一段窗口里徽标先说实话。
      const stale = status === "waiting_approval" && approvals.length > 0 && approvals.every((item) => item.expired);
      return {
        id: turn.id,
        goal: turn.goal,
        images: turn.attachments,
        steps,
        plan,
        reply: verdict.text || turn.summary,
        reason: verdict.reason,
        rawReason: verdict.rawReason,
        failureCode: verdict.code,
        status: stale ? "approval_expired" : status,
        running: isRunning,
        // 实时文字只挂在还跑着的那一轮：任务结束后事件里已经有完整回复，
        // 两处同时显示会出现一段重复的话。
        streaming: isLive && isRunning ? live.streaming : "",
        createdAt: turn.createdAt,
        files: touchedFiles(steps),
        stepCount: steps.length,
        // 只在真的跑着的时候挂审批/提问卡。跑完的一轮如果还挂着，用户点"允许这一次"
        // 后端会回"任务不在等审批"（409），而错误显示在对话顶部——人已经滚到底部，
        // 看起来就是"点了没反应"。卡片跟着轮次结束一起消失，才不会有这种死按钮。
        approvals,
        questions: isLive && running ? (evidence?.pendingQuestions ?? []) : [],
      };
    });
  }, [conv, activeTaskId, live.events, live.streaming, liveStatus, running, evidence]);

  const lastTurn = currentTurnView[currentTurnView.length - 1] ?? null;
  const activeEvidence = running ? evidence : null;
  // 普通对话没有产物时不占右侧：只有真的改过文件、跑出测试/验收，
  // 或这条会话有工作区改动，面板才有可看的东西。会话本身仍在时间线里。
  const panelHasContent = Boolean(
    (lastTurn?.files?.length ?? 0) > 0 ||
    (evidence?.changes?.length ?? 0) > 0 ||
    (activeEvidence?.changes?.length ?? 0) > 0 ||
    threadChanges.length > 0 ||
    evidence?.test ||
    activeEvidence?.test ||
    (evidence?.acceptance?.length ?? 0) > 0 ||
    (activeEvidence?.acceptance?.length ?? 0) > 0,
  );
  // 不在当前这条会话里的后台任务。正在看的那条不该在横幅里再报一遍。
  // 没有 threadId 的历史孤儿任务**不能**被过滤掉：它们恰恰是最容易"关窗后找不到"
  // 的那批，横幅是唯一的兜底入口（v164 §2.3）。它们点开走任务详情，不走会话。
  const backgroundTasks = visibleBackgroundTasks(activeTasks, conv?.thread.id);
  // 「后台」这一格说的是还在动的那几条。
  //
  // 接口的 active=true 还会带回 failed / interrupted——它们是"关窗后回来找"的兜底入口，
  // 该出现，但不该被算进"后台 N 个"：实测页面写着「后台 10 个」，其中 9 条已经失败
  // 结束，用户找不到能等的东西，只会以为界面在乱报。数量按"还在动"算，标题也取
  // 还在动的那条；一条都不在动时整格不显示。
  const liveBackgrounds = backgroundTasks.filter((task) =>
    ["queued", "running", "waiting_approval", "paused"].includes(task.status),
  );
  const topBackground = liveBackgrounds[0] ?? backgroundTasks[0] ?? null;
  const backgroundTarget = topBackground ? backgroundTaskTarget(topBackground) : null;
  // 分成两个入口：有会话的回会话（要调 openThread），没会话的直接跳详情页。
  const backgroundThreadId = backgroundTarget?.kind === "thread" ? backgroundTarget.threadId : null;
  const backgroundHref = backgroundTarget?.kind === "detail" ? backgroundTarget.href : undefined;
  // 清单的行：在做什么、跑了多久、能不能停。只有一条时状态条直接跳过去，
  // 两条以上才值得展开——否则"清单"就是重复那一格的复制品。
  const backgroundRows = backgroundRowsFor(activeTasks, conv?.thread.id);

  // 顶部状态条只留「这一轮在做什么」：目标进度、后台任务、服务是否断。
  // 场景 / 模型 / 权限档都在输入框工具条上，各有可点的选择器，
  // 顶部再放一份就是三重重复——用户第一反应是「模式 辅助 是什么东西」。
  const activeGoal = threadGoal && threadGoal.threadId === conv?.thread.id ? threadGoal.goal : null;
  const statusBarData: GoalStatusBarData = {
    goal: activeGoal?.title ?? "",
    goalStatus: activeGoal?.status ?? "",
    // 数的是「别的」会话：当前这条已经在眼前了，再报一次只会让人以为还有一份。
    backgroundCount: liveBackgrounds.length,
    backgroundLabel: liveBackgrounds[0]?.title ?? "",
    backgroundStatus: liveBackgrounds[0]?.status ?? "",
    serviceOnline,
  };

  return (
    <div className="flex h-dvh overflow-hidden bg-transparent">
      <Sidebar
        projects={(boot?.projects ?? []).map((project) => ({ id: project.id, name: project.name }))}
        activeProjectId={projectId}
        onToggleProjectPin={(id, pinned) => {
          // 置顶是幂等的服务端状态，不做乐观更新：列表顺序由后端排，
          // 前端先改会出现"星亮了但位置没动"的错觉。
          void (async () => {
            try {
              await updateProject(id, { pinned });
              await refreshProjects();
            } catch (error: unknown) {
              setActionError(errorMessage(error));
            }
          })();
        }}
        onSelectProject={(id) => {
          // 切项目 = 换上下文（Codex 的 "What should we build in X?"）。
          // 与"换目录"是两件事：换目录只改工作根，会话不动。
          if (id === projectId) return;
          setProjectId(id);
          setThreadsNonce((value) => value + 1);
          startNew();
        }}
        threads={threads}
        activeId={conv?.thread.id ?? null}
        error={threadsError}
        archivedError={archivedError}
        collapsed={sidebarCollapsed}
        onToggle={() => {
          const next = !sidebarCollapsed;
          setSidebarCollapsed(next);
          writeString(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
        }}
        onNew={startNew}
        onSelect={openThread}
        onArchive={archive}
        onBatchArchive={batchArchive}
        onRename={rename}
        onFork={(thread) => void forkThreadById(thread)}
        onUnarchive={unarchive}
        onDelete={onDelete}
      />

      {/* 主内容与任务面板共用一块圆角卡片，和 Qoder 的“灰底 + 白卡”一致。 */}
      <div className="flex min-w-0 flex-1 p-3">
      <div className="glass flex min-w-0 flex-1 overflow-hidden rounded-[var(--radius-lg)]">
      <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line/70 bg-transparent px-5">
          <h1 className="min-w-0 flex-1 truncate text-[14px] font-medium">
            {conv?.thread.title || "新对话"}
          </h1>
          {conv && (
            <span className="shrink-0 font-mono text-[11px] text-faint">{conv.turns.length} 轮</span>
          )}
          {panelHasContent && (
            <button
              type="button"
              onClick={() => setPanelOpenSaved(!panelOpen)}
              title={panelOpen ? "收起结果面板" : "打开结果面板"}
              className={`grid size-7 place-items-center rounded-md transition-colors hover:bg-surface-2 ${
                panelOpen ? "text-text" : "text-faint"
              }`}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
                <rect x="3" y="4" width="18" height="16" rx="2" />
                <path d="M15 4v16" />
              </svg>
            </button>
          )}
        </header>
        {/* 后台任务收进状态条那一格（见 goal-status-bar）：同一件事原来在
            状态条和下面的橙色横幅各说一遍，两条整行告示互相抢注意力。 */}
        <GoalStatusBar
          data={statusBarData}
          backgroundExpandable={backgroundRows.length > 1}
          backgroundOpen={backgroundOpen}
          onBackground={
            backgroundRows.length > 1
              ? () => setBackgroundOpen((value) => !value)
              : backgroundThreadId
                ? () => openThread(backgroundThreadId)
                : undefined
          }
          backgroundHref={backgroundRows.length > 1 ? undefined : backgroundHref}
          backgroundTitle={topBackground?.title}
        />
        {backgroundOpen && backgroundRows.length > 1 && (
          <BackgroundList
            rows={backgroundRows}
            busy={actionBusy}
            onOpen={openBackgroundRow}
            onStop={stopBackgroundRow}
          />
        )}

        <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {bootError && (
              <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[13px] text-danger">
                连不上后端：{bootError}
              </p>
            )}
            {convError && (
              <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[13px] text-danger">
                对话读取失败：{convError}
              </p>
            )}
            {currentTurnView.length === 0 && !loadingConv && (
              <FirstScreen
                workspaceLabel={activeWorkspace?.label ?? ""}
                workspaceReady={workspaceReady}
              />
            )}

            {loadingConv && currentTurnView.length === 0 && (
              <div className="space-y-3 py-10" aria-label="读取对话">
                {[0, 1, 2].map((row) => (
                  <div key={row} className="space-y-2 rounded-2xl border border-line/60 bg-surface-2/50 p-4">
                    <div className="h-3 w-24 animate-pulse rounded-full bg-surface-3" />
                    <div className="h-3 w-full animate-pulse rounded-full bg-surface-3" />
                    <div className="h-3 w-3/4 animate-pulse rounded-full bg-surface-3" />
                  </div>
                ))}
              </div>
            )}

            <div className="space-y-8">
              {currentTurnView.map((turn) => (
                <TurnView
                  key={turn.id}
                  goal={turn.goal}
                  images={turn.images}
                  projectId={projectId}
                  steps={turn.steps}
                  plan={turn.plan}
                  reply={turn.reply}
                  reason={turn.reason}
                  rawReason={turn.rawReason}
                  failureCode={turn.failureCode}
                  status={turn.status}
                  running={turn.running}
                  streaming={turn.streaming}
                  createdAt={turn.createdAt}
                  files={turn.files}
                  stepCount={turn.stepCount}
                  approvals={turn.approvals}
                  questions={turn.questions}
                  actionBusy={actionBusy}
                  onDecide={decide}
                  onAnswer={answer}
                  onRetry={retry}
                  onQuote={(text) => {
                    // 引用要拼在用户已经在打的内容前面，而不是把它顶掉。
                    setDraft((current) => quoteDraft(current, text));
                    setFocusToken((value) => value + 1);
                  }}
                  // 技能提示只给最后一条：回放历史时每条都挂一个"你可以用…"，
                  // 等于把对话变成广告位。
                  skillHint={
                    turn.id === currentTurnView[currentTurnView.length - 1]?.id && !turn.running
                      ? suggestSkill(turn.goal, skillItems, { used: turnSkills(turn.id) })
                      : null
                  }
                  onUseSkill={(display) => {
                    setDraft(`用「${display}」技能的做法处理这件事：`);
                    setFocusToken((value) => value + 1);
                  }}
                />
              ))}
            </div>

            {live.notice && (
              <p className="mt-4 font-mono text-[11px] text-faint">{live.notice}</p>
            )}
          </div>
        </div>

        {showJump && (
          <button
            type="button"
            onClick={jumpToBottom}
            className="glass-soft absolute bottom-36 left-1/2 z-20 -translate-x-1/2 rounded-full px-3 py-1.5 text-[12px] text-muted transition-colors hover:text-text"
          >
            回到底部 ↓
          </button>
        )}

        {/* 待你处理的卡挂在输入框上方，不放在聊天流里：卡片长在正文里时，
            长对话要往上翻才看得见，用户只会看到输入框突然不能用了。 */}
        <PendingPanel
          approvals={running ? (evidence?.pendingApprovals ?? []) : []}
          waiting={liveStatus === "waiting_approval"}
          busy={actionBusy}
          // 能打回到哪几步，跟任务页用同一个函数算：两处的"第 N 步"必须指同一步，
          // 否则用户在对话页退回的可能是另一件事。
          steps={sendBackChoices(liveTask?.id === activeTaskId ? liveTask.planSteps : [])}
          onDecide={decide}
          onRetry={retry}
          onStop={stop}
        />

        <Composer
          projects={boot?.projects ?? []}
          providers={boot?.providers ?? []}
          providersReady={boot !== null}
          projectId={projectId}
          workspaces={workspaces}
          workspaceReady={workspaceReady}
          activeWorkspaceId={activeWorkspace?.id ?? null}
          canPickWorkspace={canPickWorkspace}
          onActivateWorkspace={(id) => void activateWorkspaceById(id)}
          onPickWorkspace={() => void pickWorkspace()}
          providerId={providerId}
          // 顶部状态条撤掉模型胶囊后，默认模型的名字改从这里显示——
          // 不然「默认模型」四个字看不出到底在跑哪个。
          defaultModelLabel={
            // 只认"有 Key"的供应商；用户没显式选时优先认后端标出的默认供应商，
            // 而不是拿列表第一项猜（设置里锁的可能是另一家）。
            defaultModelLabel(boot?.providers ?? [], providerId)
          }
          onProviderChange={(id) => {
            setProviderId(id);
            writeString(PROVIDER_KEY, id);
          }}
          scenarios={scenarios}
          scenarioKey={scenarioKey}
          onScenarioChange={changeScenario}
          memoryOptIn={memoryOptIn}
          onMemoryOptInChange={(value) => {
            setMemoryOptIn(value);
            writeString(MEMORY_KEY, value ? "on" : "off");
          }}
          permissionMode={permissionMode}
          onPermissionChange={(value) => {
            setPermissionMode(value);
            writeString(PERMISSION_KEY, value);
          }}
          draft={draft}
          onDraftChange={setDraft}
          focusToken={focusToken}
          images={images}
          onAddImages={addImages}
          onRemoveImage={removeImage}
          onSend={() => void send()}
          onSteer={steer}
          onStop={stop}
          running={running}
          // 等审批（含已过期）时输入框改说法：running 为真只因为这一轮还没结束，
          // 不代表它在干活。
          waiting={lastTurn?.status === "waiting_approval" || lastTurn?.status === "approval_expired"}
          // 审批/停止/归档的失败也走这里：错误必须出现在用户刚点过的地方。
          // 原来它渲染在对话流顶部，滚到当前这一轮的人根本看不到。
          error={sendError ?? actionError}
          hint={composerHint}
          disabled={(boot !== null && !projectId) || sending}
        />
      </main>

      {panelOpen && lastTurn && panelHasContent && (
        <TaskPanel
          // 每个会话一套面板状态：文件标签/搜索/全屏都跟着会话走，
          // 否则切到另一条会话时会残留上一条的文件标签，点开全是读取失败。
          key={conv?.thread.id ?? ""}
          title={conv?.thread.title ?? ""}
          status={lastTurn.status}
          running={lastTurn.running}
          steps={lastTurn.steps}
          changes={activeEvidence?.changes ?? evidence?.changes ?? []}
          workspaceChanges={threadChanges}
          projectId={projectId}
          usage={activeEvidence?.usage ?? evidence?.usage ?? null}
          acceptance={activeEvidence?.acceptance ?? evidence?.acceptance ?? []}
          test={activeEvidence?.test ?? evidence?.test ?? null}
          onClose={() => setPanelOpenSaved(false)}
        />
      )}
      </div>
      </div>
      {directoryPickerOpen && (
        <DirectoryPicker
          initialPath={directoryPickerStart}
          onClose={() => setDirectoryPickerOpen(false)}
          onPick={(path) => {
            setDirectoryPickerOpen(false);
            void bindAndActivateWorkspace(path);
          }}
        />
      )}
    </div>
  );
}
