/** 无依赖语法高亮器。支持 JS/TS/Python/HTML/CSS/Bash/JSON/Diff。
 *
 * 策略：每个 token 独立渲染成一个 span，通过 CSS class 控制颜色。
 * 不做 AST 解析，只做逐行正则匹配——零外部依赖，体积为零。
 *
 * 设计边界：
 * - 不支持嵌套（引号里套注释不追求精准）
 * - 不支持多行长字符串（Python """..."""）
 * - 行号不追求 100% 准确地对齐所有边缘情况 */

import React from "react";

type TokenKind = "keyword" | "string" | "comment" | "number" | "operator" | "plain";

interface Token {
  kind: TokenKind;
  text: string;
}

/* ---------- 每个语言的 Token 规则 ---------- */
interface Rule {
  kind: TokenKind;
  pattern: RegExp;
}

const JS_RULES: Rule[] = [
  { kind: "comment", pattern: /\/\/.*$/ },
  { kind: "comment", pattern: /\/\*[\s\S]*?\*\// }, // 多行（简单正则，不支持嵌套）
  { kind: "string", pattern: /`(?:[^`\\]|\\.)*`/ },
  { kind: "string", pattern: /"(?:[^"\\]|\\.)*"/ },
  { kind: "string", pattern: /'(?:[^'\\]|\\.)*'/ },
  { kind: "keyword", pattern: /\b(await|break|case|catch|class|const|continue|debugger|default|delete|do|else|export|extends|finally|for|function|if|import|in|instanceof|let|new|of|return|super|switch|this|throw|try|typeof|var|void|while|with|yield|async|from|as|static|enum|implements|interface|package|private|protected|public|null|undefined|true|false)\b/ },
  { kind: "number", pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/ },
];

const PY_RULES: Rule[] = [
  { kind: "comment", pattern: /#.*$/ },
  { kind: "string", pattern: /f?"""[\s\S]*?"""/ },
  { kind: "string", pattern: /f?"(?:[^"\\]|\\.)*"/ },
  { kind: "string", pattern: /f?'(?:[^'\\]|\\.)*'/ },
  { kind: "keyword", pattern: /\b(False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|self|cls|print)\b/ },
  { kind: "number", pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/ },
]

const HTML_RULES: Rule[] = [
  { kind: "comment", pattern: /<!--[\s\S]*?-->/ },
  { kind: "string", pattern: /"(?:[^"\\]|\\.)*"/ },
  { kind: "string", pattern: /'(?:[^'\\]|\\.)*'/ },
  { kind: "keyword", pattern: /<\/?[a-zA-Z][a-zA-Z0-9-]*/ }, // 标签名
  { kind: "keyword", pattern: /\b[A-Z][a-zA-Z]+(?:-[a-zA-Z]+)*\b(?=\s*=)/ }, // Vue/Svelte 属性名
];

const CSS_RULES: Rule[] = [
  { kind: "comment", pattern: /\/\*[\s\S]*?\*\//  },
  { kind: "string", pattern: /"(?:[^"\\]|\\.)*"/ },
  { kind: "string", pattern: /'(?:[^'\\]|\\.)*'/ },
  { kind: "keyword", pattern: /@\w+/ },
  { kind: "number", pattern: /\b\d+(\.\d+)?(px|em|rem|%|vh|vw|s|ms|deg|ch)?\b/ },
];

const BASH_RULES: Rule[] = [
  { kind: "comment", pattern: /#.*$/ },
  { kind: "string", pattern: /"(?:[^"\\]|\\.)*"/ },
  { kind: "string", pattern: /'(?:[^'\\]|\\.)*'/ },
  { kind: "string", pattern: /\$(?:[{(][^)}]+[)}]|\w+)/ }, // shell 变量
  { kind: "keyword", pattern: /\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|exit|export|source|alias|unset|set|local|readonly|declare)\b/ },
];

const JSON_RULES: Rule[] = [
  { kind: "string", pattern: /"(?:[^"\\]|\\.)*"/ },
  { kind: "number", pattern: /\b-?\d+(\.\d+)?([eE][+-]?\d+)?\b/ },
  { kind: "keyword", pattern: /\b(true|false|null)\b/ },
];

const DIFF_RULES: Rule[] = [
  { kind: "keyword", pattern: /^@@[\s\S]*?@@/ },
  { kind: "keyword", pattern: /^\+.*$/ },
  { kind: "comment", pattern: /^-.*$/ },
];

/* ---------- Parser ---------- */

function parseLine(text: string, rules: Rule[]): Token[] {
  const sorted = [...rules].sort((a, b) => {
    // comments first
    if (a.kind === "comment" && b.kind !== "comment") return -1;
    if (b.kind === "comment" && a.kind !== "comment") return 1;
    return 0});
  const tokens: Token[] = [];
  let pos = 0;
  while (pos < text.length) {
    let matched = false;
    for (const rule of sorted) {
      rule.pattern.lastIndex = pos;
      const m = rule.pattern.exec(text);
      if (m && m.index === pos) {
        if (m[0].length > 0) {
          tokens.push({ kind: rule.kind, text: m[0] });
        }
        pos += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      tokens.push({ kind: "plain", text: text[pos] });
      pos += 1;
    }
  }
  return tokens;
}

/* ---------- 语言选择 ---------- */

function rulesFor(lang: string): Rule[] {
  const cleaned = lang.trim().toLowerCase();
  if (cleaned === "js" || cleaned === "javascript" || cleaned === "jsx") return JS_RULES;
  if (cleaned === "ts" || cleaned === "typescript" || cleaned === "tsx") return JS_RULES;
  if (cleaned === "py" || cleaned === "python") return PY_RULES;
  if (cleaned === "html" || cleaned === "xml") return HTML_RULES;
  if (cleaned === "css" || cleaned === "scss") return CSS_RULES;
  if (cleaned === "bash" || cleaned === "sh" || cleaned === "shell") return BASH_RULES;
  if (cleaned === "json") return JSON_RULES;
  if (cleaned === "diff" || cleaned === "patch") return DIFF_RULES;
  return JS_RULES;
}

export function tokenize(text: string, lang: string): Token[][] {
  const rules = rulesFor(lang);
  return text.split("\n").map((line) => parseLine(line, rules));
}

/* ---------- 渲染 ---------- */

const TOKEN_CLASSES: Record<TokenKind, string> = {
  keyword: "text-[#cf222e] dark:text-[#ff7b72] font-semibold",
  string: "text-[#0a3069] dark:text-[#a5d6ff]",
  comment: "text-[#6e7781] dark:text-[#8b949e] italic",
  number: "text-[#0550ae] dark:text-[#79c0ff]",
  plain: "text-text",
  operator: "text-text",
};

export function HighlightedCode({ text, lang }: { text: string; lang: string }) {
  const lines = tokenize(text, lang);
  const lineCount = lines.length;
  const maxDigits = String(lineCount).length;

  return (
    <pre className="overflow-auto bg-surface px-3 py-2.5 text-[12px] leading-relaxed font-mono">
      <code className="table min-w-full">
        {lines.map((tokens, idx) => (
          <span key={idx} className="table-row group hover:bg-surface-2 transition-colors">
            <span
              className="table-cell pr-3 select-none text-right text-faint"
              style={{ minWidth: `${maxDigits + 2}ch` }}
            >
              {idx + 1}
            </span>
            <span className="table-cell pr-2 whitespace-pre">
              {tokens.length > 0 ? tokens.map((tok, ti) => (
                <span key={ti} className={TOKEN_CLASSES[tok.kind] || ""}>
                  {tok.text}
                </span>
              )) : "\u00A0"}
            </span>
          </span>
        ))}
      </code>
    </pre>
  );
}