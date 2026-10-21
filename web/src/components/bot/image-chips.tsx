"use client";

import type { DraftImage } from "./uploads";

/** 草稿区的图片条。上传中、失败、成功三种状态都在这一个卡片上表达。 */
export function DraftImageChips({
  images,
  onRemove,
  disabled,
}: {
  images: DraftImage[];
  onRemove: (key: string) => void;
  disabled?: boolean;
}) {
  if (images.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 px-3 pt-3">
      {images.map((image) => (
        <div
          key={image.key}
          className="relative size-16 overflow-hidden rounded-lg border border-line bg-surface-2"
          title={`${image.name}${image.error ? ` · ${image.error}` : ""}`}
        >
          {image.preview ? (
            // 静态导出（桌面壳从 app:// 加载 out/）用不了 next/image 的优化器，
            // 这里就是一张本地 blob 缩略图，走原生 img 才是对的。
            // eslint-disable-next-line @next/next/no-img-element
            <img src={image.preview} alt={image.name} className="size-full object-cover" />
          ) : (
            <span className="grid size-full place-items-center text-[10px] text-faint">图片</span>
          )}

          {image.uploading && (
            <span className="absolute inset-0 grid place-items-center bg-bg/70 font-mono text-[10px] text-muted">
              上传中
            </span>
          )}

          {image.error && (
            <span className="absolute inset-0 grid place-items-center bg-danger/10 p-1 text-center text-[9px] leading-tight text-danger">
              {image.error}
            </span>
          )}

          {/* 失败的图要能一键去掉：留在草稿里会一直挡住发送（发送前会检查上传状态）。 */}
          {!disabled && (
            <button
              type="button"
              onClick={() => onRemove(image.key)}
              aria-label={`移除 ${image.name}`}
              className="absolute top-0.5 right-0.5 grid size-4 place-items-center rounded-full bg-text/70 text-[10px] leading-none text-bg transition-opacity hover:bg-text"
            >
              ×
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
