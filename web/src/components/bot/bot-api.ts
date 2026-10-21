/**
 * bot 的数据编排：一次对话在界面上是"消息"，在数据层是"一条会话 + 若干轮任务"。
 *
 * 这里不新增任何后端概念。一句话发出去，做的事是：
 * 有会话就在这条会话里开一轮，没有就先开一条会话；然后按任务那套启动。
 * 所以运行、审批、中断、回放全都还是原来那条路径——bot 只是它的另一种呈现。
 */

import {
  archiveThread,
  cancelTask,
  createThread,
  createTurn,
  fetchEvents,
  fetchProjects,
  fetchProviders,
  fetchThread,
  fetchThreads,
  runTask,
  type AgentEvent,
  type Project,
  type Provider,
  type Task,
  type Thread,
  type ThreadDetail,
} from "../tasks/api";
import { threadTitleFrom } from "./turns";

/**
 * 打开一条会话时给最近几轮拉事件。
 *
 * 不给全部轮次拉：一条会话聊十轮之后，进页面要发十几个请求、传几百 KB 的
 * 工具输出，而用户第一眼要看的是最后那几句。更早的轮次仍然显示目标与结论，
 * 只是不展开过程——想看可以点开单独拉。
 */
export const HYDRATE_TURNS = 6;

/** 一轮的事件上限，与后端 `GET /tasks/{id}/events` 的默认值一致。 */
const EVENT_LIMIT = 500;

export type BotTurn = {
  id: string;
  title: string;
  goal: string;
  status: string;
  summary: string;
  createdAt: string;
  events: AgentEvent[];
  /** 事件是否真的取回来了。false 表示这一轮只有目标与结论，过程没加载。 */
  eventsLoaded: boolean;
};

export type Conversation = { thread: ThreadDetail; turns: BotTurn[] };

export type Boot = { projects: Project[]; providers: Provider[] };

export async function loadBoot(): Promise<Boot> {
  const [projects, providers] = await Promise.allSettled([fetchProjects(), fetchProviders()]);
  return {
    projects: projects.status === "fulfilled" ? projects.value : [],
    providers: providers.status === "fulfilled" ? providers.value : [],
  };
}

/** 会话按最近活动排。后端顺序不保证，这里再排一次——列表第一项必须是"刚才那条"。 */
export async function loadConversations(projectId: string): Promise<Thread[]> {
  if (!projectId) return [];
  const rows = await fetchThreads(projectId);
  return [...rows].sort((a, b) => {
    const left = Date.parse(b.updatedAt || b.createdAt);
    const right = Date.parse(a.updatedAt || a.createdAt);
    return (Number.isNaN(left) ? 0 : left) - (Number.isNaN(right) ? 0 : right);
  });
}

async function turnEvents(taskId: string): Promise<{ events: AgentEvent[]; loaded: boolean }> {
  try {
    const events = await fetchEvents(taskId);
    return { events: events.slice(0, EVENT_LIMIT), loaded: true };
  } catch {
    // 取不到过程不影响读会话：目标和结论已经在会话详情里，界面照样能显示。
    // 这里的失败不向上抛，否则一次事件接口抖动会让整条会话打不开。
    return { events: [], loaded: false };
  }
}

export async function loadConversation(threadId: string): Promise<Conversation | null> {
  const detail = await fetchThread(threadId);
  if (!detail) return null;
  const recent = detail.turns.slice(-HYDRATE_TURNS);
  const loaded = await Promise.all(recent.map((turn) => turnEvents(turn.id)));
  const offset = detail.turns.length - recent.length;
  return {
    thread: detail,
    turns: detail.turns.map((turn, index) => {
      const hydrated = index >= offset ? loaded[index - offset] : null;
      return {
        id: turn.id,
        title: turn.title,
        goal: turn.goal,
        status: turn.status,
        summary: turn.resultSummary,
        createdAt: turn.createdAt,
        events: hydrated?.events ?? [],
        eventsLoaded: hydrated?.loaded ?? false,
      };
    }),
  };
}

export type SendResult = { thread: Thread; task: Task; runError: string };

/**
 * 发一句话：必要时先开会话，再开一轮，然后立刻跑。
 *
 * 立刻跑是刻意的：在 bot 里按回车就是"去做"，不是"先建个草稿等我去点运行"。
 * 启动失败不抛给调用方去猜：任务已经建出来了，这一轮是"跑失败"而不是"没发出去"，
 * 所以把原因一起返回，让界面在原地说明白。
 */
export async function sendMessage(input: {
  projectId: string;
  threadId: string;
  text: string;
  providerId?: string;
}): Promise<SendResult> {
  const text = input.text.trim();
  if (!text) throw new Error("消息不能为空");
  const title = threadTitleFrom(text);

  let thread: Thread;
  let fresh = false;
  if (input.threadId) {
    thread = { id: input.threadId, projectId: input.projectId, title: "", status: "active", forkedFrom: "", turnCount: 0, createdAt: "", updatedAt: "" };
  } else {
    thread = await createThread(input.projectId, { title });
    fresh = true;
  }

  let task: Task;
  try {
    task = await createTurn(thread.id, {
      projectId: input.projectId,
      title,
      prompt: text,
      acceptanceCriteria: [],
      providerId: input.providerId,
    });
  } catch (error) {
    // 刚建的空会话要收掉：否则用户点一次发送失败，左侧就多一条点开没内容的会话。
    if (fresh) await archiveThread(thread.id).catch(() => undefined);
    throw error;
  }

  let runError = "";
  try {
    await runTask(task.id);
  } catch (error) {
    runError = error instanceof Error ? error.message : String(error);
  }
  return { thread, task, runError };
}

export async function stopTask(taskId: string): Promise<void> {
  await cancelTask(taskId);
}
