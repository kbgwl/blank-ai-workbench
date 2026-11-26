/** 文件预览的类型判断与格式化。纯函数，`node --test` 能直接跑。 */

export type PreviewKind = "html" | "markdown" | "json" | "csv" | "text";

function extension(path: string): string {
  const name = String(path ?? "").split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : "";
}

export function previewKind(path: string): PreviewKind {
  const ext = extension(path);
  if (ext === "html" || ext === "htm") return "html";
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "json") return "json";
  if (ext === "csv" || ext === "tsv") return "csv";
  // docx/xlsx/pptx 由后端抽成 markdown 文本（见 services/office_preview.py）；
  // 抽出内容时才走到这里，抽不出来仍是 binary 分支的提示。
  if (ext === "docx" || ext === "xlsx" || ext === "pptx") return "markdown";
  return "text";
}

export function delimiterFor(path: string): "," | "\t" {
  return extension(path) === "tsv" ? "\t" : ",";
}

export type JsonFormat =
  | { ok: true; text: string }
  | { ok: false; error: string };

/** 合法 JSON 才格式化；解析失败时保留原文，由调用方显示错误。 */
export function formatJson(text: string): JsonFormat {
  try {
    return { ok: true, text: JSON.stringify(JSON.parse(text), null, 2) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "JSON 解析失败" };
  }
}

export type DelimitedTable = {
  rows: string[][];
  truncated: boolean;
};

/**
 * 解析 CSV/TSV。
 *
 * 只做预览需要的部分：引号字段、双引号转义、CRLF、行内逗号。行列都有上限——
 * 一个 200 万行的导出 CSV 塞进 DOM 会把页面卡死，预览只承诺"看清结构"。
 */
export function parseDelimited(
  text: string,
  delimiter: "," | "\t",
  maxRows = 200,
  maxCols = 50,
): DelimitedTable {
  const source = String(text ?? "");
  if (!source.trim()) return { rows: [], truncated: false };

  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let truncated = false;

  const pushField = (): void => {
    if (row.length < maxCols) row.push(field);
    else truncated = true;
    field = "";
  };
  const pushRow = (): void => {
    pushField();
    if (rows.length < maxRows) rows.push(row);
    else truncated = true;
    row = [];
  };

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"' && field === "") {
      quoted = true;
    } else if (char === delimiter) {
      pushField();
    } else if (char === "\n") {
      pushRow();
    } else if (char === "\r") {
      // CRLF 的 \r 丢掉；单独的 \r 也当换行处理。
      if (source[index + 1] !== "\n") pushRow();
    } else {
      field += char;
    }
  }
  if (field !== "" || row.length > 0) pushRow();

  // 末尾换行会多推一个空行：CSV 的末尾空行不是一条数据。
  const cleaned = rows.filter((item, index) => !(index === rows.length - 1 && item.length === 1 && item[0] === ""));
  return { rows: cleaned, truncated };
}
