/**
 * 图片的"能不能收"规则。
 *
 * 单独一个文件、不依赖任何网络或 React 模块：这些判断是纯逻辑，应该能被单测
 * 直接跑（node --test 解析不了 `@/` 别名，一旦混进 http 客户端这条链就断了）。
 * 规则必须与后端 services/uploads.py 的 ALLOWED_MIME / MAX_UPLOAD_BYTES 一致，
 * 前端先拦一次只为"立刻给反馈"，最终判据仍在后端。
 */

export const ACCEPTED_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

export const ACCEPT_ATTR = ACCEPTED_IMAGE_TYPES.join(",");

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

/** 一轮最多几张。与后端 TaskCreate.attachments 的 max_length 一致。 */
export const MAX_IMAGES_PER_TURN = 8;

/** 草稿里的一张图。上传前后都在这一个形状上表达。 */
export type DraftImage = {
  /** 本地唯一键。不能用 id：上传完成前还没有 id。 */
  key: string;
  id: string;
  name: string;
  mime: string;
  size: number;
  preview: string;
  uploading: boolean;
  error: string;
};

/** 选之前就能判断的错，直接返回给界面，不进上传流程。 */
export function imageRejection(file: File): string | null {
  if (!ACCEPTED_IMAGE_TYPES.includes(file.type)) {
    return `只支持 PNG / JPEG / WebP / GIF（这个是 ${file.type || "未知格式"}）`;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return `图片超过 ${MAX_IMAGE_BYTES / (1024 * 1024)}MB，请先压缩或裁剪`;
  }
  return null;
}

/** 从拖拽/粘贴事件里挑出图片文件。非图片的一律忽略而不是报错。 */
export function imageFilesFrom(list: FileList | File[] | null | undefined): File[] {
  if (!list) return [];
  return Array.from(list).filter((file) => file.type.startsWith("image/"));
}
