import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_IMAGE_BYTES,
  imageFilesFrom,
  imageRejection,
} from "./image-policy.ts";

/** 只需要 type 与 size，不必真的造一个 File。 */
function fakeFile(type: string, size = 1024, name = "a.png"): File {
  return { type, size, name } as File;
}

test("只收模型看得懂的四种位图，其余在选择时就说清楚", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp", "image/gif"]) {
    assert.equal(imageRejection(fakeFile(type)), null, `${type} 应该被接受`);
  }
  // svg 是文本、pdf 不是图：放进去只会在调模型时变成 400，代价比现在说一句高得多。
  assert.match(imageRejection(fakeFile("image/svg+xml")) ?? "", /只支持/);
  assert.match(imageRejection(fakeFile("application/pdf")) ?? "", /PDF|只支持|pdf/i);
  assert.match(imageRejection(fakeFile("")) ?? "", /未知格式/);
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
