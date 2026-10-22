import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_FILE_BYTES,
  MAX_IMAGE_BYTES,
  MAX_TOTAL_IMAGE_BYTES,
  attachmentKind,
  attachmentRejection,
  filesFrom,
  imageFilesFrom,
  imageRejection,
  totalImageBytes,
} from "./image-policy.ts";

/** 只需要 type 与 size，不必真的造一个 File。 */
function fakeFile(type: string, size = 1024, name = "a.png"): File {
  return { type, size, name } as File;
}

test("只收模型看得懂的四种位图，其余在选择时就说清楚", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp", "image/gif"]) {
    assert.equal(imageRejection(fakeFile(type)), null, `${type} 应该被接受`);
  }
  // svg 是文本、不是位图：放进去只会在调模型时变成 400，代价比现在说一句高得多。
  assert.match(imageRejection(fakeFile("image/svg+xml", 1024, "a.svg")) ?? "", /只支持/);
  assert.match(imageRejection(fakeFile("application/zip", 1024, "a.zip")) ?? "", /只支持/);
  // 没有扩展名也没有 type：归不了类，要拒。
  assert.match(imageRejection(fakeFile("", 1024, "noext")) ?? "", /未知格式/);
});

test("大小上限与后端一致，超了给出可操作的建议而不是只报错", () => {
  assert.equal(imageRejection(fakeFile("image/png", MAX_IMAGE_BYTES)), null);
  const over = imageRejection(fakeFile("image/png", MAX_IMAGE_BYTES + 1)) ?? "";
  assert.match(over, /8MB/);
  assert.match(over, /压缩|裁剪/);
});

test("拖拽/粘贴里混着非图片文件时只挑图片，不因为混入而整批拒掉", () => {
  const files = [
    fakeFile("image/png", 10, "a.png"),
    fakeFile("application/pdf", 10, "b.pdf"),
    fakeFile("image/jpeg", 10, "c.jpg"),
  ];
  assert.deepEqual(
    imageFilesFrom(files).map((file) => file.name),
    ["a.png", "c.jpg"],
  );
  // 拖进来一段文字或从别处拖东西时 files 可能为空，不能因此抛错。
  assert.deepEqual(imageFilesFrom(null), []);
  assert.deepEqual(imageFilesFrom([]), []);
});

test("文档类附件按扩展名认：浏览器的 type 常常是空串", () => {
  // .md 的 type 在 Windows 上是空串，在 mac 上是 text/markdown——不许靠 type。
  assert.equal(attachmentKind(fakeFile("", 10, "需求.md")), "text");
  assert.equal(attachmentKind(fakeFile("text/markdown", 10, "需求.md")), "text");
  assert.equal(attachmentKind(fakeFile("", 10, "spec.pdf")), "pdf");
  assert.equal(attachmentKind(fakeFile("application/pdf", 10, "spec.pdf")), "pdf");
  assert.equal(attachmentKind(fakeFile("", 10, "app.tsx")), "text");
  assert.equal(attachmentKind(fakeFile("", 10, "shot.PNG")), "image");
  assert.equal(attachmentKind(fakeFile("", 10, "a.zip")), null);
  assert.equal(attachmentKind(fakeFile("", 10, "noext")), null);

  assert.equal(attachmentRejection(fakeFile("", 10, "需求.md")), null);
  assert.match(attachmentRejection(fakeFile("", 10, "a.zip")) ?? "", /不支持/);
  // 文档同样有 8MB 上限，且错误里要带文件名——一次选十个文件时得知道是哪个。
  assert.match(attachmentRejection(fakeFile("", MAX_FILE_BYTES + 1, "大文件.csv")) ?? "", /大文件\.csv/);
});

test("filesFrom 统一三种入参形状，粘贴/拖拽/选择框都走它", () => {
  const list = [fakeFile("image/png", 10, "a.png"), fakeFile("", 20, "b.md")];
  assert.deepEqual(
    filesFrom(list).map((file) => file.name),
    ["a.png", "b.md"],
  );
  assert.deepEqual(filesFrom(null), []);
});

test("图片总量按压完之后的字节算，超过 20MB 要能提前发现", () => {
  const items = [
    { kind: "image" as const, size: 8 * 1024 * 1024 },
    { kind: "text" as const, size: 8 * 1024 * 1024 },
    { kind: "image" as const, size: 8 * 1024 * 1024 },
  ];
  // 文本不算进图片总量：它走的是提示词，不占图像的字节预算。
  assert.equal(totalImageBytes(items), 16 * 1024 * 1024);
  assert.ok(totalImageBytes(items) < MAX_TOTAL_IMAGE_BYTES);

  const tooMany = [...items, { kind: "image" as const, size: 6 * 1024 * 1024 }];
  assert.ok(totalImageBytes(tooMany) > MAX_TOTAL_IMAGE_BYTES);
});
