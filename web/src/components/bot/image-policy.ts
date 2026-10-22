/**
 * 附件的"能不能收"规则。
 *
 * 单独一个文件、不依赖任何网络或 React 模块：这些判断是纯逻辑，应该能被单测
 * 直接跑（node --test 解析不了 `@/` 别名，一旦混进 http 客户端这条链就断了）。
 * 规则必须与后端 services/uploads.py 的 IMAGE_EXT_BY_MIME / TEXT_MIME_BY_EXT /
 * MAX_UPLOAD_BYTES 一致，前端先拦一次只为"立刻给反馈"，最终判据仍在后端。
 */

export const ACCEPTED_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".gif"];

/** 文本类按扩展名认：浏览器给 .md 的 type 常常是空串，给 .py 的又五花八门。 */
export const TEXT_EXTENSIONS = [
  ".md",
  ".markdown",
  ".txt",
  ".log",
  ".csv",
  ".tsv",
  ".json",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".cfg",
  ".py",
  ".js",
  ".mjs",
  ".cjs",
  ".ts",
  ".tsx",
  ".jsx",
  ".sql",
  ".html",
  ".css",
  ".sh",
  ".xml",
];

export const PDF_EXTENSIONS = [".pdf"];

export const ACCEPT_ATTR = [
  ...ACCEPTED_IMAGE_TYPES,
  ...IMAGE_EXTENSIONS,
  ...TEXT_EXTENSIONS,
  ...PDF_EXTENSIONS,
].join(",");

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

/** 单份附件上限。文本按理说不会有这么大，但上限是"防呆"，不是"预期"。 */
export const MAX_FILE_BYTES = 8 * 1024 * 1024;

/** 一轮最多几个附件。与后端 TaskCreate.attachments 的 max_length 一致。 */
export const MAX_IMAGES_PER_TURN = 8;

export const MAX_ATTACHMENTS_PER_TURN = 8;

/**
 * 一轮图片的总字节上限。后端超过就不往后读了——模型侧本来就有总字节限制，
 * 与其让它 400，不如少给几张；但**要在这里先拦住**，让用户知道为什么，
 * 而不是发出去之后发现模型只看到一半。
 */
export const MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024;

export type AttachmentKind = "image" | "text" | "pdf";

/** 草稿里的一个附件。上传前后都在这一个形状上表达。 */
export type DraftAttachment = {
  /** 本地唯一键。不能用 id：上传完成前还没有 id。 */
  key: string;
  id: string;
  name: string;
  mime: string;
  size: number;
  /** 图片的本地缩略图；非图片为空串。 */
  preview: string;
  uploading: boolean;
  error: string;
  kind: AttachmentKind;
};

/** 旧名字，保留给已有调用点。 */
export type DraftImage = DraftAttachment;

function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

/** 这个文件归到哪一类。归不了返回 null（= 不支持）。 */
export function attachmentKind(file: File): AttachmentKind | null {
  const type = (file.type || "").toLowerCase();
  const ext = extOf(file.name || "");
  if (ACCEPTED_IMAGE_TYPES.includes(type) || IMAGE_EXTENSIONS.includes(ext)) return "image";
  if (type === "application/pdf" || PDF_EXTENSIONS.includes(ext)) return "pdf";
  if (TEXT_EXTENSIONS.includes(ext) || type.startsWith("text/")) return "text";
  return null;
}

/** 选之前就能判断的错，直接返回给界面，不进上传流程。 */
export function attachmentRejection(file: File): string | null {
  const kind = attachmentKind(file);
  if (kind === null) {
    return `${file.name || "这个文件"} 不支持，只收图片、文本类文件与 PDF`;
  }
  if (file.size > MAX_FILE_BYTES) {
    return `${file.name || "附件"} 超过 ${MAX_FILE_BYTES / (1024 * 1024)}MB，请先压缩或裁剪`;
  }
  return null;
}

/** 图片专用的老入口：非图片一律按"不支持"拒掉。 */
export function imageRejection(file: File): string | null {
  if (attachmentKind(file) !== "image") {
    return `只支持 PNG / JPEG / WebP / GIF（这个是 ${file.type || "未知格式"}）`;
  }
  return attachmentRejection(file);
}

/** 把 FileList / 数组统一成数组。粘贴、拖拽、选择框三处的入参都不一样。 */
export function filesFrom(list: FileList | File[] | null | undefined): File[] {
  if (!list) return [];
  return Array.from(list as ArrayLike<File>);
}

/** 只挑出图片的老入口。 */
export function imageFilesFrom(list: FileList | File[] | null | undefined): File[] {
  return filesFrom(list).filter((file) => attachmentKind(file) === "image");
}

/** 一轮里的图片总量。用来在发送前拦住"合计超过 20MB"。 */
export function totalImageBytes(items: Array<{ kind: AttachmentKind; size: number }>): number {
  return items.reduce((sum, item) => (item.kind === "image" ? sum + item.size : sum), 0);
}
