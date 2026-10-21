/**
 * 图片附件的前端部分：选之前先拦一次，选之后立刻上传。
 *
 * 上传和发送分两步是刻意的。用户在写一句话的十几秒里，图已经在传了；等按下回车
 * 时只剩下一个 id 要带过去。反过来（发送时才传）会让"大图 + 慢网"变成一段
 * 按了回车却什么都不发生的空白时间，用户以为没发出去，于是再按一次。
 *
 * 校验规则与常量在 image-policy.ts，那里不依赖网络，才能被单测直接跑。
 */

import { API_V1, errorMessage, jsonInit, request } from "@/lib/http";
import type { AttachmentRef } from "../tasks/api";

export {
  ACCEPT_ATTR,
  ACCEPTED_IMAGE_TYPES,
  MAX_IMAGES_PER_TURN,
  MAX_IMAGE_BYTES,
  imageFilesFrom,
  imageRejection,
  type DraftImage,
} from "./image-policy";

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("读不到这张图片"));
    reader.readAsDataURL(file);
  });
}

/** 上传一张图，返回后端记下的引用。失败时抛出的 message 可以直接展示。 */
export async function uploadImage(projectId: string, file: File): Promise<AttachmentRef> {
  const data = await readAsDataUrl(file);
  try {
    return await request<AttachmentRef>(`${API_V1}/projects/${projectId}/uploads`, {
      method: "POST",
      ...jsonInit,
      body: JSON.stringify({ name: file.name, mime: file.type, data }),
    });
  } catch (error) {
    throw new Error(errorMessage(error));
  }
}

/** 已上传图片的读取地址，用于消息回放时渲染缩略图。 */
export function uploadUrl(projectId: string, uploadId: string): string {
  return `${API_V1}/projects/${projectId}/uploads/${uploadId}`;
}

/** 本地预览地址。用完要 release，否则一张图会一直占着内存。 */
export function previewUrl(file: File): string {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return "";
  return URL.createObjectURL(file);
}

export function releasePreview(url: string): void {
  if (!url) return;
  if (typeof URL === "undefined" || typeof URL.revokeObjectURL !== "function") return;
  URL.revokeObjectURL(url);
}
