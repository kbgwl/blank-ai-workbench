/**
 * 附件的前端部分：选之前先拦一次，选之后立刻上传。
 *
 * 上传和发送分两步是刻意的。用户在写一句话的十几秒里，文件已经在传了；等按下回车
 * 时只剩下一个 id 要带过去。反过来（发送时才传）会让"大图 + 慢网"变成一段
 * 按了回车却什么都不发生的空白时间，用户以为没发出去，于是再按一次。
 *
 * 校验规则与常量在 image-policy.ts，那里不依赖网络，才能被单测直接跑。
 */

import { API_V1, errorMessage, jsonInit, request } from "@/lib/http";
import type { AttachmentRef } from "../tasks/api";
import { attachmentKind } from "./image-policy";

export {
  ACCEPT_ATTR,
  ACCEPTED_IMAGE_TYPES,
  MAX_ATTACHMENTS_PER_TURN,
  MAX_FILE_BYTES,
  MAX_IMAGES_PER_TURN,
  MAX_IMAGE_BYTES,
  MAX_TOTAL_IMAGE_BYTES,
  PDF_EXTENSIONS,
  TEXT_EXTENSIONS,
  attachmentKind,
  attachmentRejection,
  filesFrom,
  imageFilesFrom,
  imageRejection,
  totalImageBytes,
  type AttachmentKind,
  type DraftAttachment,
  type DraftImage,
} from "./image-policy";

/**
 * 图片降采样的长边上限。
 *
 * 手机截图动辄 3000px、4~8MB，而模型看一张 2048px 的图和看原图的结论没有区别——
 * 多出来的像素只会变成 base64、变成请求体、变成界面里解码后的内存。降到这里之后
 * 一张截图通常只有几百 KB，上传和预览都跟着快一个数量级。
 */
const MAX_EDGE = 2048;

/** 重编码让体积明显变小才替换，否则保留原文件：PNG 截图转 WebP 不一定更小。 */
const SHRINK_RATIO = 0.85;

function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("读不到这个文件"));
    reader.readAsDataURL(file);
  });
}

function withExtension(name: string, ext: string): string {
  const dot = name.lastIndexOf(".");
  const base = dot < 0 ? name : name.slice(0, dot);
  return `${base}${ext}`;
}

/**
 * 上传前把图片压到合理尺寸。
 *
 * 任何一步失败都原样返回原文件——压缩是优化，不是正确性的前提；为了省几百 KB
 * 让用户传不上图，是本末倒置。GIF 直接跳过：canvas 画一遍就成静态图了，动图没了。
 */
export async function prepareImage(file: File): Promise<File> {
  if (attachmentKind(file) !== "image" || file.type === "image/gif") return file;
  if (typeof document === "undefined" || typeof createImageBitmap !== "function") return file;
  try {
    // from-image：手机照片的 EXIF 方向必须在这里就用上，否则压完是躺倒的。
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const longest = Math.max(bitmap.width, bitmap.height);
    const scale = longest > MAX_EDGE ? MAX_EDGE / longest : 1;
    if (scale === 1 && file.size <= SHRINK_RATIO * (1024 * 1024)) {
      bitmap.close?.();
      return file;
    }
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) {
      bitmap.close?.();
      return file;
    }
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob((result) => resolve(result), "image/webp", 0.9);
    });
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], withExtension(file.name, ".webp"), {
      type: "image/webp",
      lastModified: file.lastModified,
    });
  } catch {
    return file;
  }
}

/** 上传一个附件，返回后端记下的引用。失败时抛出的 message 可以直接展示。 */
export async function uploadAttachment(projectId: string, file: File): Promise<AttachmentRef> {
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

/** 旧名字。 */
export const uploadImage = uploadAttachment;

/** 已上传附件的读取地址，用于消息回放时渲染缩略图或下载。 */
export function uploadUrl(projectId: string, uploadId: string): string {
  return `${API_V1}/projects/${projectId}/uploads/${uploadId}`;
}

/** 本地预览地址。用完要 release，否则一张图会一直占着内存。 */
export function previewUrl(file: Blob): string {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return "";
  return URL.createObjectURL(file);
}

export function releasePreview(url: string): void {
  if (!url) return;
  if (typeof URL === "undefined" || typeof URL.revokeObjectURL !== "function") return;
  URL.revokeObjectURL(url);
}
