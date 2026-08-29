import fs from "node:fs/promises";
import path from "node:path";

const SITE = "https://help.tdx.com.cn";
const QUANT_ROOT = `${SITE}/quant/`;
const DOC_ROOT = `${SITE}/quant/docs/markdown/`;

const workspace = process.cwd();
const docsDir = path.resolve(workspace, "tdx_quant_docs");
const rawDir = path.join(docsDir, "raw");
const officialDir = path.join(docsDir, "official");
const compactDir = path.join(docsDir, "compact");

const categories = [
  {
    title: "TdxQuant概述",
    dir: "01-tdxquant-overview",
    root: "mindoc-1cfsjkbf8f3is",
    matches: (p) => p === "mindoc-1cfsjkbf8f3is" || p.startsWith("mindoc-1cfsjkbf8f3is/"),
  },
  {
    title: "通用函数",
    dir: "02-general-functions",
    root: "ctx.stock.md",
    matches: (p) => p === "ctx.stock.md" || p.startsWith("ctx.stock.md/"),
  },
  {
    title: "行情类信息",
    dir: "03-market-data",
    root: "mindoc-1ctuhthaq5qmg",
    matches: (p) => p === "mindoc-1ctuhthaq5qmg" || p.startsWith("mindoc-1ctuhthaq5qmg/"),
  },
  {
    title: "财务类数据",
    dir: "04-financial-data",
    root: "TdxQuant.md",
    matches: (p) => p === "TdxQuant.md" || p.startsWith("TdxQuant.md/"),
  },
  {
    title: "分类/板块成份股",
    dir: "05-sector-constituents",
    root: "mindoc-1ctuhttn72svo",
    matches: (p) => p === "mindoc-1ctuhttn72svo" || p.startsWith("mindoc-1ctuhttn72svo/"),
  },
  {
    title: "自选股/自定义板块",
    dir: "06-watchlist-custom-sector",
    root: "mindoc-1h139a4ckchkk",
    matches: (p) => p === "mindoc-1h139a4ckchkk" || p.startsWith("mindoc-1h139a4ckchkk/"),
  },
  {
    title: "ETF/可转债/期货数据",
    dir: "07-etf-bond-futures",
    root: "mindoc-1h13a594nhvb4",
    matches: (p) => p === "mindoc-1h13a594nhvb4" || p.startsWith("mindoc-1h13a594nhvb4/"),
  },
  {
    title: "调用通达信公式",
    dir: "08-tdx-formula",
    root: "mindoc-1h3hrvkp4sc0g",
    matches: (p) => p === "mindoc-1h3hrvkp4sc0g" || p.startsWith("mindoc-1h3hrvkp4sc0g/"),
  },
  {
    title: "交易函数",
    dir: "09-trading-functions",
    root: "mindoc-1h7k4iqb1grk4",
    matches: (p) => p === "mindoc-1h7k4iqb1grk4" || p.startsWith("mindoc-1h7k4iqb1grk4/"),
  },
  {
    title: "常量枚举",
    dir: "10-constants",
    root: "Dict.html",
    matches: (p) => p === "Dict.html",
  },
  {
    title: "回测及模拟交易",
    dir: "11-backtesting-paper-trading",
    root: "mindoc-1h12t4q6fg29o.html",
    matches: (p) => p === "mindoc-1h12t4q6fg29o.html",
  },
  {
    title: "场景化示例",
    dir: "12-scenarios",
    root: "mindoc-1h1525ci3mnkc",
    matches: (p) => p === "mindoc-1h1525ci3mnkc" || p.startsWith("mindoc-1h1525ci3mnkc/"),
  },
  {
    title: "公众号文章例子",
    dir: "13-wechat-examples",
    root: "gzh0122inweixinwenz",
    matches: (p) => p === "gzh0122inweixinwenz" || p.startsWith("gzh0122inweixinwenz/"),
  },
  {
    title: "常见问题",
    dir: "14-faq",
    root: "mindoc-tdxpy.html",
    matches: (p) => p === "mindoc-tdxpy.html",
  },
];

const manualSlugs = new Map([
  ["mindoc-1cfsjkbf8f3is", "tdxquant_intro"],
  ["mindoc-1cfsjkbf8f3is/TdxQuantVersion.html", "version_updates"],
  ["mindoc-1cfsjkbf8f3is/mindoc-1d00970eq1rtc.html", "install_python_dev_env"],
  ["mindoc-1cfsjkbf8f3is/mindoc-1d00kk3jsibbc.html", "install_tdx_terminal"],
  ["mindoc-1cfsjkbf8f3is/mindoc-1cv7o3nje2gu8.html", "quick_start_first_strategy"],
  ["ctx.stock.md", "general_functions"],
  ["mindoc-1ctuhthaq5qmg", "market_data_overview"],
  ["TdxQuant.md", "financial_data_overview"],
  ["mindoc-1ctuhttn72svo", "sector_constituents_overview"],
  ["mindoc-1h139a4ckchkk", "watchlist_custom_sector_overview"],
  ["mindoc-1h13a594nhvb4", "etf_bond_futures_overview"],
  ["mindoc-1h3hrvkp4sc0g", "tdx_formula_overview"],
  ["mindoc-1h7k4iqb1grk4", "trading_functions_overview"],
  ["Dict.html", "constants"],
  ["mindoc-1h12t4q6fg29o.html", "backtesting_paper_trading"],
  ["mindoc-1h1525ci3mnkc", "scenario_overview"],
  ["mindoc-1h1525ci3mnkc/mindoc-1h15262vnafcc.html", "stock_selection_to_custom_sector"],
  ["mindoc-1h1525ci3mnkc/mindoc-1h1526nmnk5n4.html", "realtime_breakout_subscription"],
  ["mindoc-1h1525ci3mnkc/mindoc-1h1ep1rl20jv8.html", "rebalance_signal_fast_trade"],
  ["mindoc-1h1525ci3mnkc/mindoc-1h62qo3mceppc.html", "vbt_backtest_plot"],
  ["gzh0122inweixinwenz", "tq_strategy_intro_examples"],
  ["gzh0122inweixinwenz/gzh20260122wzlz.html", "wechat_20260122_strategy_examples"],
  ["gzh0122inweixinwenz/gzh20260302wzlz.html", "wechat_20260302_formula_python_loop"],
  ["mindoc-tdxpy.html", "python_file_location_faq"],
]);

function assertInside(parent, target) {
  const rel = path.relative(parent, target);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`Refusing to operate outside ${parent}: ${target}`);
  }
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function archiveOriginalMarkdown() {
  await fs.mkdir(rawDir, { recursive: true });
  const entries = await fs.readdir(docsDir, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".md") || ["README.md", "FULL_INDEX.md"].includes(entry.name)) {
      continue;
    }
    const from = path.join(docsDir, entry.name);
    const to = path.join(rawDir, entry.name);
    assertInside(docsDir, from);
    assertInside(rawDir, to);
    if (await exists(to)) {
      throw new Error(`Archive target already exists, refusing to overwrite: ${to}`);
    }
    await fs.rename(from, to);
  }
}

async function fetchText(url, retries = 2) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
      return await response.text();
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)));
      }
    }
  }
  throw new Error(`Failed to fetch ${url}: ${lastError.message}`);
}

function normalizeDocPath(href, fromUrl) {
  let url;
  try {
    url = new URL(href, fromUrl);
  } catch {
    return null;
  }
  url.hash = "";
  if (url.origin !== SITE || !url.pathname.startsWith("/quant/docs/markdown/")) {
    return null;
  }
  return decodeURIComponent(url.pathname.replace("/quant/docs/markdown/", ""));
}

async function crawlOfficialPaths() {
  const seenUrls = new Set();
  const paths = new Set();
  const queue = [QUANT_ROOT];

  while (queue.length > 0 && seenUrls.size < 200) {
    const url = queue.shift();
    if (seenUrls.has(url)) {
      continue;
    }
    seenUrls.add(url);
    const html = await fetchText(url);
    for (const match of html.matchAll(/href=["']([^"']+)["']/g)) {
      const docPath = normalizeDocPath(match[1], url);
      if (!docPath) {
        continue;
      }
      paths.add(docPath);
      const nextUrl = `${DOC_ROOT}${docPath}`;
      if (!seenUrls.has(nextUrl) && !queue.includes(nextUrl)) {
        queue.push(nextUrl);
      }
    }
  }

  return [...paths];
}

function htmlEntityDecode(text) {
  const named = {
    amp: "&",
    lt: "<",
    gt: ">",
    quot: "\"",
    apos: "'",
    nbsp: " ",
    ndash: "-",
    mdash: "-",
    hellip: "...",
    times: "x",
  };
  return text.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (full, entity) => {
    if (entity.startsWith("#x")) {
      return String.fromCodePoint(Number.parseInt(entity.slice(2), 16));
    }
    if (entity.startsWith("#")) {
      return String.fromCodePoint(Number.parseInt(entity.slice(1), 10));
    }
    return named[entity] ?? full;
  });
}

function stripTags(fragment) {
  return htmlEntityDecode(
    fragment
      .replace(/<a[^>]*class=["']header-anchor["'][\s\S]*?<\/a>/g, "")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<[^>]+>/g, "")
  )
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function inlineMarkdown(fragment) {
  let text = fragment
    .replace(/<a[^>]*class=["']header-anchor["'][\s\S]*?<\/a>/g, "")
    .replace(/<code>([\s\S]*?)<\/code>/g, (_, code) => `\`${stripTags(code)}\``)
    .replace(/<(strong|b)>([\s\S]*?)<\/\1>/g, (_, __, inner) => `**${inlineMarkdown(inner)}**`)
    .replace(/<(em|i)>([\s\S]*?)<\/\1>/g, (_, __, inner) => `*${inlineMarkdown(inner)}*`)
    .replace(/<a\s+[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/g, (_, href, label) => {
      const cleanLabel = stripTags(label).replace(/\s*\(opens new window\)\s*/g, "").trim();
      return cleanLabel ? `[${cleanLabel}](${href})` : "";
    })
    .replace(/<img\s+[^>]*src=["']([^"']+)["'][^>]*>/g, (_, src) => {
      const imageUrl = src.startsWith("/") ? `${SITE}${src}` : src;
      return `![](${imageUrl})`;
    })
    .replace(/<br\s*\/?>/gi, "\n");
  return stripTags(text);
}

function codeMarkdown(codeHtml) {
  return htmlEntityDecode(
    codeHtml
      .replace(/<span[^>]*>/g, "")
      .replace(/<\/span>/g, "")
      .replace(/<br\s*\/?>/gi, "\n")
  ).replace(/\s+$/g, "");
}

function tableMarkdown(tableHtml) {
  const rows = [];
  for (const rowMatch of tableHtml.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)) {
    const cells = [];
    for (const cellMatch of rowMatch[1].matchAll(/<(?:th|td)[^>]*>([\s\S]*?)<\/(?:th|td)>/g)) {
      cells.push(inlineMarkdown(cellMatch[1]).replace(/\|/g, "\\|").replace(/\s+/g, " ").trim());
    }
    if (cells.length > 0) {
      rows.push(cells);
    }
  }
  if (rows.length === 0) {
    return "";
  }
  const width = Math.max(...rows.map((row) => row.length));
  const normalized = rows.map((row) => [...row, ...Array(width - row.length).fill("")]);
  const header = normalized[0];
  const separator = Array(width).fill("---");
  const body = normalized.slice(1);
  return [
    `| ${header.join(" | ")} |`,
    `| ${separator.join(" | ")} |`,
    ...body.map((row) => `| ${row.join(" | ")} |`),
  ].join("\n");
}

function extractContentHtml(html) {
  const marker = '<div class="theme-default-content content__default">';
  const start = html.indexOf(marker);
  const footer = html.indexOf('<footer class="page-edit"', start);
  if (start === -1 || footer === -1) {
    throw new Error("Could not locate VuePress content block");
  }
  return html.slice(start + marker.length, footer).trim().replace(/<\/div>\s*$/, "").trim();
}

function extractTitle(contentHtml, fullHtml) {
  const match = contentHtml.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i);
  if (match) {
    return inlineMarkdown(match[1]).replace(/^#\s*/, "").trim();
  }
  const hashParagraph = contentHtml.match(/<p[^>]*>\s*#\s*([^<]+?)\s*<\/p>/i);
  if (hashParagraph) {
    return stripTags(hashParagraph[1]).trim();
  }
  const titleTag = fullHtml.match(/<title>([\s\S]*?)<\/title>/i);
  if (titleTag) {
    const pageTitle = stripTags(titleTag[1]).replace(/\s*\|\s*通达信量化平台\s*$/, "").trim();
    if (pageTitle) {
      return pageTitle;
    }
  }
  const lowerHeading = contentHtml.match(/<h[2-6][^>]*>([\s\S]*?)<\/h[2-6]>/i);
  if (lowerHeading) {
    return inlineMarkdown(lowerHeading[1]).replace(/^#\s*/, "").trim();
  }
  return "Untitled";
}

function extractFirstCode(contentHtml) {
  const match = contentHtml.match(/<pre[^>]*>\s*<code>([\s\S]*?)<\/code>\s*<\/pre>/i);
  return match ? codeMarkdown(match[1]).trim() : "";
}

function extractFunctionSlug(contentHtml, title) {
  const code = extractFirstCode(contentHtml);
  const candidates = [
    ...code.matchAll(/\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/g),
    ...code.matchAll(/(?:^|\n)\s*([a-z][A-Za-z0-9_]*_[A-Za-z0-9_]+)\s*\(/g),
    ...title.matchAll(/\b([a-z][a-z0-9]+(?:_[a-z0-9]+)+(?:\/[a-z0-9_]+)*)\b/g),
  ];
  for (const match of candidates) {
    const name = (match[1] ?? "").replace(/\//g, "_").toLowerCase();
    if (name && !["pd_dataframe"].includes(name)) {
      return name;
    }
  }
  return null;
}

function fallbackSlug(docPath, title) {
  const asciiTitle = title
    .toLowerCase()
    .replace(/[^a-z0-9_/\s-]/g, " ")
    .replace(/\//g, "_")
    .replace(/[-\s]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
  if (asciiTitle) {
    return asciiTitle;
  }
  return docPath
    .replace(/\.html$/i, "")
    .replace(/\.md$/i, "")
    .replace(/[^\w]+/g, "_")
    .replace(/^_|_$/g, "")
    .toLowerCase();
}

function htmlToMarkdown(contentHtml) {
  const codeBlocks = [];
  const tables = [];

  let html = contentHtml
    .replace(/<a[^>]*class=["']header-anchor["'][\s\S]*?<\/a>/g, "")
    .replace(/<div class=["']line-numbers-wrapper["'][\s\S]*?<\/div>/g, "");

  html = html.replace(
    /<div class=["']language-([^"'\s]*)[^"']*["'][^>]*>\s*<pre[^>]*>\s*<code>([\s\S]*?)<\/code>\s*<\/pre>\s*<\/div>/g,
    (_, lang, code) => {
      const normalizedLang = lang && lang !== "-" ? lang : "text";
      const placeholder = `@@CODE_BLOCK_${codeBlocks.length}@@`;
      const codeText = codeMarkdown(code);
      const fence = codeText.includes("```") ? "````" : "```";
      codeBlocks.push(`\n${fence}${normalizedLang}\n${codeText}\n${fence}\n`);
      return placeholder;
    }
  );

  html = html.replace(/<table[^>]*>[\s\S]*?<\/table>/g, (table) => {
    const placeholder = `@@TABLE_BLOCK_${tables.length}@@`;
    tables.push(`\n${tableMarkdown(table)}\n`);
    return placeholder;
  });

  html = html
    .replace(/<summary[^>]*>([\s\S]*?)<\/summary>/g, (_, inner) => `\n#### ${inlineMarkdown(inner)}\n`)
    .replace(/<h([1-6])[^>]*>([\s\S]*?)<\/h\1>/g, (_, level, inner) => `\n${"#".repeat(Number(level))} ${inlineMarkdown(inner)}\n`)
    .replace(/<blockquote[^>]*>([\s\S]*?)<\/blockquote>/g, (_, inner) => {
      const lines = htmlToMarkdown(inner).trim().split(/\n/).map((line) => `> ${line}`);
      return `\n${lines.join("\n")}\n`;
    })
    .replace(/<li[^>]*>([\s\S]*?)<\/li>/g, (_, inner) => {
      const content = htmlToMarkdown(inner).trim().replace(/\n{2,}/g, "\n");
      return `\n- ${content.replace(/\n/g, "\n  ")}\n`;
    })
    .replace(/<\/?(ul|ol)[^>]*>/g, "\n")
    .replace(/<p[^>]*>([\s\S]*?)<\/p>/g, (_, inner) => `\n${inlineMarkdown(inner)}\n`)
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<hr\s*\/?>/gi, "\n---\n")
    .replace(/<[^>]+>/g, "");

  let markdown = htmlEntityDecode(html)
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  codeBlocks.forEach((block, index) => {
    markdown = markdown.replace(`@@CODE_BLOCK_${index}@@`, block);
  });
  tables.forEach((block, index) => {
    markdown = markdown.replace(`@@TABLE_BLOCK_${index}@@`, block);
  });

  return markdown
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]+$/gm, "")
    .replace(/\[\d+\]\(@ref\)/g, "")
    .replace(/\(@ref\)/g, "")
    .replace(/\s*\(opens new window\)\s*/g, "")
    .trim();
}

function extractNextPath(html, currentUrl) {
  const navMatch = html.match(/<span class=["']next["'][\s\S]*?<a href=["']([^"']+)["'][\s\S]*?<\/span>/i);
  if (!navMatch) {
    return null;
  }
  return normalizeDocPath(navMatch[1], currentUrl);
}

function categoryForPath(docPath) {
  return categories.find((category) => category.matches(docPath));
}

function posixRelative(fromFile, toFile) {
  const fromDir = path.posix.dirname(fromFile.split(path.sep).join(path.posix.sep));
  const to = toFile.split(path.sep).join(path.posix.sep);
  let rel = path.posix.relative(fromDir, to);
  if (!rel.startsWith(".")) {
    rel = `./${rel}`;
  }
  return rel;
}

function rewriteDocLinks(markdown, currentOutput, outputBySource) {
  return markdown.replace(
    /\]\(((?:https:\/\/help\.tdx\.com\.cn)?\/quant\/docs\/markdown\/[^)#]+|https:\/\/help\.tdx\.com\.cn\/quant\/docs\/markdown\/[^)#]+)(#[^)]+)?\)/g,
    (full, href, anchor = "") => {
      const target = normalizeDocPath(href, DOC_ROOT);
      if (!target || !outputBySource.has(target)) {
        return full;
      }
      const rel = posixRelative(currentOutput, outputBySource.get(target));
      return `](${rel}${anchor})`;
    }
  );
}

function orderPages(paths, htmlByPath) {
  const nextByPath = new Map();
  const previousTargets = new Set();

  for (const docPath of paths) {
    const next = extractNextPath(htmlByPath.get(docPath), `${DOC_ROOT}${docPath}`);
    if (next && paths.includes(next)) {
      nextByPath.set(docPath, next);
      previousTargets.add(next);
    }
  }

  const ordered = [];
  const visited = new Set();
  const starts = paths.filter((docPath) => nextByPath.has(docPath) && !previousTargets.has(docPath));

  for (const start of starts) {
    let current = start;
    while (current && !visited.has(current)) {
      visited.add(current);
      ordered.push(current);
      current = nextByPath.get(current);
    }
  }

  for (const docPath of paths) {
    if (!visited.has(docPath)) {
      ordered.push(docPath);
    }
  }
  return ordered;
}

async function removeDirectoryContents(targetDir) {
  const resolved = path.resolve(targetDir);
  assertInside(docsDir, resolved);
  if (path.basename(resolved) !== "official") {
    throw new Error(`Refusing to remove unexpected directory: ${resolved}`);
  }
  await fs.rm(resolved, { recursive: true, force: true });
  await fs.mkdir(resolved, { recursive: true });
}

async function recreateDocsSubdir(name) {
  const resolved = path.resolve(docsDir, name);
  assertInside(docsDir, resolved);
  if (path.dirname(resolved) !== docsDir || !["compact"].includes(path.basename(resolved))) {
    throw new Error(`Refusing to recreate unexpected directory: ${resolved}`);
  }
  await fs.rm(resolved, { recursive: true, force: true });
  await fs.mkdir(resolved, { recursive: true });
}

async function writeMarkdown(filePath, content) {
  assertInside(docsDir, filePath);
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, `${content.replace(/\s+$/g, "")}\n`, "utf8");
}

function bodyMarkdown(title, sourceUrl, categoryTitle, rawMarkdown, currentOutput, outputBySource) {
  const lines = rawMarkdown.trim().split(/\n/);
  let body = rawMarkdown.trim();
  const firstLine = lines[0]?.trim() ?? "";
  const firstHeading = firstLine.match(/^(#{1,6})\s*(.+)$/);
  if (
    firstLine.startsWith("# ") ||
    firstLine === `#${title}` ||
    firstLine === title ||
    firstHeading?.[2]?.trim() === title
  ) {
    body = lines.slice(1).join("\n").trim();
  }
  body = rewriteDocLinks(body, currentOutput, outputBySource);
  return [
    `# ${title}`,
    "",
    `> 来源: ${sourceUrl}`,
    `> 栏目: ${categoryTitle}`,
    "",
    body,
  ].join("\n").replace(/\n{3,}/g, "\n\n");
}

async function writeCompactDocs() {
  await recreateDocsSubdir("compact");

  const compactReadme = path.join(compactDir, "README.md");
  const quickStart = path.join(compactDir, "quick-start.md");
  const apiCheatsheet = path.join(compactDir, "api-cheatsheet.md");
  const workflows = path.join(compactDir, "workflows.md");
  const faq = path.join(compactDir, "faq.md");
  const link = (fromFile, target, label) => readmeLink(fromFile, path.join(docsDir, target), label);

  await writeMarkdown(
    compactReadme,
    [
      "# TdxQuant 精简版",
      "",
      "这个目录把完整 79 页官方文档压缩成日常查阅的 4 份文档。完整资料仍保留在 `official/`，需要细节时从精简版跳过去看。",
      "",
      "## 推荐阅读顺序",
      "",
      `1. ${readmeLink(compactReadme, quickStart, "快速开始")}：初始化、取行情、输出结果的最短路径。`,
      `2. ${readmeLink(compactReadme, apiCheatsheet, "API 速查")}：按使用场景找函数。`,
      `3. ${readmeLink(compactReadme, workflows, "常见工作流")}：选股入板块、实时预警、公式调用、交易执行。`,
      `4. ${readmeLink(compactReadme, faq, "FAQ")}：路径、数据下载、代码位置和安全提醒。`,
      "",
      "## 官方栏目入口",
      "",
      `- ${link(compactReadme, "official/01-tdxquant-overview/README.md", "TdxQuant概述")}`,
      `- ${link(compactReadme, "official/02-general-functions/README.md", "通用函数")}`,
      `- ${link(compactReadme, "official/03-market-data/README.md", "行情类信息")}`,
      `- ${link(compactReadme, "official/04-financial-data/README.md", "财务类数据")}`,
      `- ${link(compactReadme, "official/05-sector-constituents/README.md", "分类/板块成份股")}`,
      `- ${link(compactReadme, "official/06-watchlist-custom-sector/README.md", "自选股/自定义板块")}`,
      `- ${link(compactReadme, "official/07-etf-bond-futures/README.md", "ETF/可转债/期货数据")}`,
      `- ${link(compactReadme, "official/08-tdx-formula/README.md", "调用通达信公式")}`,
      `- ${link(compactReadme, "official/09-trading-functions/README.md", "交易函数")}`,
      `- ${link(compactReadme, "official/10-constants/README.md", "常量枚举")}`,
      `- ${link(compactReadme, "official/11-backtesting-paper-trading/README.md", "回测及模拟交易")}`,
      `- ${link(compactReadme, "official/12-scenarios/README.md", "场景化示例")}`,
      `- ${link(compactReadme, "official/13-wechat-examples/README.md", "公众号文章例子")}`,
      `- ${link(compactReadme, "official/14-faq/README.md", "常见问题")}`,
      "",
      "## 完整资料",
      "",
      `- ${link(compactReadme, "FULL_INDEX.md", "完整索引")}`,
      `- ${link(compactReadme, "official/README.md", "官方整理版")}`,
      `- ${link(compactReadme, "raw", "原始导出归档")}`,
    ].join("\n")
  );

  await writeMarkdown(
    quickStart,
    [
      "# 快速开始",
      "",
      "## 1. 准备环境",
      "",
      `- 安装客户端并下载数据：${link(quickStart, "official/01-tdxquant-overview/install_tdx_terminal.md", "安装通达信终端并获取数据")}`,
      `- 安装 Python/IDE：${link(quickStart, "official/01-tdxquant-overview/install_python_dev_env.md", "安装 Python 及开发环境")}`,
      `- 第一个策略示例：${link(quickStart, "official/01-tdxquant-overview/quick_start_first_strategy.md", "快速开始第一个策略")}`,
      "",
      "## 2. 初始化",
      "",
      "```python",
      "from tqcenter import tq",
      "",
      "tq.initialize(__file__)",
      "```",
      "",
      "## 3. 获取日线行情",
      "",
      "```python",
      "df = tq.get_market_data(",
      "    field_list=['Open', 'High', 'Low', 'Close', 'Volume'],",
      "    stock_list=['688318.SH'],",
      "    period='1d',",
      "    start_time='20250101',",
      "    end_time='',",
      "    count=-1,",
      "    dividend_type='none',",
      "    fill_data=True,",
      ")",
      "print(df)",
      "```",
      "",
      `详见：${link(quickStart, "official/03-market-data/get_market_data.md", "get_market_data")}`,
      "",
      "## 4. 把选股结果写入自定义板块",
      "",
      "```python",
      "stocks = ['688318.SH']",
      "tq.create_sector(block_code='', block_name='策略结果')",
      "tq.send_user_block(block_code='', stocks=stocks)",
      "```",
      "",
      `相关接口：${link(quickStart, "official/06-watchlist-custom-sector/create_sector.md", "create_sector")} / ${link(quickStart, "official/06-watchlist-custom-sector/send_user_block.md", "send_user_block")}`,
      "",
      "## 5. 实时消息与预警",
      "",
      "```python",
      "tq.send_message('策略运行完成')",
      "# send_warn 适合把买卖信号推到客户端预警窗口",
      "```",
      "",
      `相关接口：${link(quickStart, "official/02-general-functions/send_message.md", "send_message")} / ${link(quickStart, "official/02-general-functions/send_warn.md", "send_warn")}`,
    ].join("\n")
  );

  await writeMarkdown(
    apiCheatsheet,
    [
      "# API 速查",
      "",
      "## 基础与缓存",
      "",
      "| 场景 | 常用函数 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| 初始化 | \`initialize\` | 每个脚本先调用 | ${link(apiCheatsheet, "official/02-general-functions/initialize.md", "文档")} |`,
      `| 刷新行情缓存 | \`refresh_cache\` | 快照/K线首次取数前可主动刷新 | ${link(apiCheatsheet, "official/02-general-functions/refresh_cache.md", "文档")} |`,
      `| 刷新历史K线 | \`refresh_kline\` | 下载指定品种、周期的历史K线 | ${link(apiCheatsheet, "official/02-general-functions/refresh_kline.md", "文档")} |`,
      `| 交易日 | \`get_trading_dates\` | 获取指定时间段交易日 | ${link(apiCheatsheet, "official/02-general-functions/get_trading_dates.md", "文档")} |`,
      `| 下载特定数据文件 | \`download_file\` | 下载股东、ETF申赎、舆情等数据文件 | ${link(apiCheatsheet, "official/02-general-functions/download_file.md", "文档")} |`,
      `| 导出到客户端 | \`print_to_tdx\` | 把多组 DataFrame 输出到客户端展示 | ${link(apiCheatsheet, "official/02-general-functions/print_to_tdx.md", "文档")} |`,
      `| 调用客户端功能 | \`exec_to_tdx\` | 让客户端按入参执行指定功能 | ${link(apiCheatsheet, "official/02-general-functions/exec_to_tdx.md", "文档")} |`,
      "",
      "## 行情与基础数据",
      "",
      "| 场景 | 常用函数 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| K线 | \`get_market_data\` | 历史行情主入口 | ${link(apiCheatsheet, "official/03-market-data/get_market_data.md", "文档")} |`,
      `| 快照 | \`get_market_snapshot\` | 最新行情快照 | ${link(apiCheatsheet, "official/03-market-data/get_market_snapshot.md", "文档")} |`,
      `| 证券基本信息 | \`get_stock_info\` | 基础财务/证券信息 | ${link(apiCheatsheet, "official/03-market-data/get_stock_info.md", "文档")} |`,
      `| 更多信息 | \`get_more_info\` | 股票更细节信息 | ${link(apiCheatsheet, "official/03-market-data/get_more_info.md", "文档")} |`,
      `| 所属板块 | \`get_relation\` | 查询股票所属板块 | ${link(apiCheatsheet, "official/03-market-data/get_relation.md", "文档")} |`,
      `| 股本 | \`get_gb_info\` / \`get_gb_info_by_date\` | 单日或时间段股本 | ${link(apiCheatsheet, "official/03-market-data/get_gb_info_by_date.md", "文档")} |`,
      "",
      "## 财务与特色交易数据",
      "",
      "| 场景 | 常用函数 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| 专业财务 | \`get_financial_data\` | 按区间取专业财务字段 | ${link(apiCheatsheet, "official/04-financial-data/get_financial_data.md", "文档")} |`,
      `| 指定日期财务 | \`get_financial_data_by_date\` | 按指定日期取财务字段 | ${link(apiCheatsheet, "official/04-financial-data/get_financial_data_by_date.md", "文档")} |`,
      `| 个股交易数据 | \`get_gpjy_value\` | 龙虎榜、融资融券、涨停等 GP 字段 | ${link(apiCheatsheet, "official/04-financial-data/get_gpjy_value.md", "文档")} |`,
      `| 市场交易数据 | \`get_scjy_value\` | 市场级交易数据 | ${link(apiCheatsheet, "official/04-financial-data/get_scjy_value.md", "文档")} |`,
      `| 板块交易数据 | \`get_bkjy_value\` | 板块级交易数据 | ${link(apiCheatsheet, "official/04-financial-data/get_bkjy_value.md", "文档")} |`,
      "",
      "## 板块与自选股",
      "",
      "| 场景 | 常用函数 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| 系统分类成份股 | \`get_stock_list\` | 按市场/分类取证券列表 | ${link(apiCheatsheet, "official/05-sector-constituents/get_stock_list.md", "文档")} |`,
      `| 板块列表 | \`get_sector_list\` | 获取 A 股板块代码 | ${link(apiCheatsheet, "official/05-sector-constituents/get_sector_list.md", "文档")} |`,
      `| 板块成份股 | \`get_stock_list_in_sector\` | 按板块代码取成份股 | ${link(apiCheatsheet, "official/05-sector-constituents/get_stock_list_in_sector.md", "文档")} |`,
      `| 自定义板块 | \`create_sector\` / \`send_user_block\` / \`clear_sector\` | 创建、写入、清空策略结果 | ${link(apiCheatsheet, "official/06-watchlist-custom-sector/README.md", "文档")} |`,
      "",
      "## ETF、可转债、常量与示例",
      "",
      "| 场景 | 常用函数/栏目 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| ETF 信息 | \`get_trackzs_etf_info\` | 获取跟踪指数的 ETF 信息 | ${link(apiCheatsheet, "official/07-etf-bond-futures/get_trackzs_etf_info.md", "文档")} |`,
      `| 可转债信息 | \`get_kzz_info\` | 根据可转债代码获取信息 | ${link(apiCheatsheet, "official/07-etf-bond-futures/get_kzz_info.md", "文档")} |`,
      `| 常量枚举 | 市场、周期、复权等常量 | 查接口入参枚举值 | ${link(apiCheatsheet, "official/10-constants/constants.md", "文档")} |`,
      `| 场景示例 | 场景化示例 | 选股入板块、实时预警、VBT回测等 | ${link(apiCheatsheet, "official/12-scenarios/README.md", "文档")} |`,
      `| 公众号长示例 | 公众号文章例子 | 更长的策略代码样例 | ${link(apiCheatsheet, "official/13-wechat-examples/README.md", "文档")} |`,
      "",
      "## 公式、预警与交易",
      "",
      "| 场景 | 常用函数 | 说明 | 详页 |",
      "| --- | --- | --- | --- |",
      `| 单次公式计算 | \`formula_set_data\` / \`formula_zb\` | 设置数据后调用指标/选股/专家公式 | ${link(apiCheatsheet, "official/08-tdx-formula/formula_zb.md", "文档")} |`,
      `| 批量公式计算 | \`formula_process_mul_xg\` | 批量调用公式，适合全市场筛选 | ${link(apiCheatsheet, "official/08-tdx-formula/formula_process_mul_xg.md", "文档")} |`,
      `| 订阅行情 | \`subscribe_hq\` / \`unsubscribe_hq\` | 实时回调和取消订阅 | ${link(apiCheatsheet, "official/02-general-functions/subscribe_hq.md", "文档")} |`,
      `| 预警/消息 | \`send_warn\` / \`send_message\` | 客户端展示信号或消息 | ${link(apiCheatsheet, "official/02-general-functions/send_warn.md", "文档")} |`,
      `| 交易 | \`stock_account\` / \`query_stock_asset\` / \`order_stock\` / \`cancel_order_stock\` | 获取账户、查资产、下单、撤单 | ${link(apiCheatsheet, "official/09-trading-functions/README.md", "文档")} |`,
    ].join("\n")
  );

  await writeMarkdown(
    workflows,
    [
      "# 常见工作流",
      "",
      "## 选股后加入客户端自定义板块",
      "",
      "1. 用 `get_market_data` 或公式接口生成股票列表。",
      "2. 用 `create_sector` 创建板块。",
      "3. 用 `send_user_block` 写入股票。",
      "",
      `完整示例：${link(workflows, "official/12-scenarios/stock_selection_to_custom_sector.md", "执行选股策略并加入客户端自定义板块")}`,
      "",
      "## 实时订阅并发送预警",
      "",
      "1. 用 `subscribe_hq` 订阅不超过接口限制的股票列表。",
      "2. 在回调中取快照或最新 K 线。",
      "3. 满足条件时调用 `send_warn`。",
      "4. 退出时调用 `unsubscribe_hq`。",
      "",
      `完整示例：${link(workflows, "official/12-scenarios/realtime_breakout_subscription.md", "订阅行情涨幅突破实时预计")}`,
      "",
      "## 使用通达信公式筛选股票",
      "",
      "小批量可用 `formula_set_data` + `formula_zb/xg/exp`；全市场筛选优先看 `formula_process_mul_xg/zb`。",
      "",
      `公式接口：${link(workflows, "official/08-tdx-formula/README.md", "调用通达信公式")}`,
      "",
      "## 交易执行前的最小检查",
      "",
      "1. `stock_account` 获取账户句柄。",
      "2. `query_stock_asset` / `query_stock_positions` 确认资金和持仓。",
      "3. `order_stock` 下单。",
      "4. 必要时 `cancel_order_stock` 撤单。",
      "",
      `交易接口：${link(workflows, "official/09-trading-functions/README.md", "交易函数")}`,
      "",
      "## 回测与模拟",
      "",
      `先读概念页：${link(workflows, "official/11-backtesting-paper-trading/backtesting_paper_trading.md", "什么是量化交易")}`,
      "",
      `VBT 示例：${link(workflows, "official/12-scenarios/vbt_backtest_plot.md", "VBT简单回测并输出图形")}`,
    ].join("\n")
  );

  await writeMarkdown(
    faq,
    [
      "# FAQ",
      "",
      "## Python 文件一定要放在 `PYPlugins/user` 吗？",
      "",
      `不一定。官方 FAQ 说明了外部路径运行方式和初始化注意事项：${link(faq, "official/14-faq/python_file_location_faq.md", "查看完整 FAQ")}`,
      "",
      "## 为什么取不到完整行情或财务数据？",
      "",
      "常见原因是客户端本地数据未下载或缓存未刷新。先确认客户端登录、盘后数据下载，再按需调用 `refresh_cache` 或 `refresh_kline`。",
      "",
      "## 分钟线很多时怎么取？",
      "",
      "`get_market_data` 单次最多返回 24000 条数据，完整分钟线需要分批取。",
      "",
      "## 实时订阅适合全市场吗？",
      "",
      "不适合。实时订阅更适合较小股票池；全市场条件筛选通常用定时轮询或批量公式/行情接口。",
      "",
      "## 交易接口有什么注意事项？",
      "",
      "先查账户、资产、持仓，再下单；真实交易前用模拟账户或小范围验证。交易函数只整理官方接口说明，不构成交易建议。",
    ].join("\n")
  );
}

function readmeLink(fromFile, toFile, label) {
  return `[${label}](${posixRelative(fromFile, toFile)})`;
}

async function main() {
  await archiveOriginalMarkdown();

  const officialPaths = await crawlOfficialPaths();
  if (officialPaths.length !== 79) {
    throw new Error(`Expected 79 official pages, found ${officialPaths.length}`);
  }

  const htmlByPath = new Map();
  for (const docPath of officialPaths) {
    htmlByPath.set(docPath, await fetchText(`${DOC_ROOT}${docPath}`));
  }

  const sequence = orderPages(officialPaths, htmlByPath);
  const orderIndex = new Map(sequence.map((docPath, index) => [docPath, index]));

  const pages = [];
  const usedSlugsByCategory = new Map();
  for (const docPath of officialPaths) {
    const category = categoryForPath(docPath);
    if (!category) {
      throw new Error(`No category mapping for ${docPath}`);
    }

    const contentHtml = extractContentHtml(htmlByPath.get(docPath));
    const title = extractTitle(contentHtml, htmlByPath.get(docPath));
    const functionSlug = extractFunctionSlug(contentHtml, title);
    const baseSlug = manualSlugs.get(docPath) ?? functionSlug ?? fallbackSlug(docPath, title);
    const used = usedSlugsByCategory.get(category.dir) ?? new Set();
    let slug = baseSlug;
    let suffix = 2;
    while (used.has(slug)) {
      slug = `${baseSlug}_${suffix}`;
      suffix += 1;
    }
    used.add(slug);
    usedSlugsByCategory.set(category.dir, used);

    const output = path.join(officialDir, category.dir, `${slug}.md`);
    pages.push({
      docPath,
      sourceUrl: `${DOC_ROOT}${docPath}`,
      category,
      title,
      slug,
      output,
      markdown: htmlToMarkdown(contentHtml),
      order: orderIndex.get(docPath) ?? Number.MAX_SAFE_INTEGER,
      isRoot: docPath === category.root,
    });
  }

  const outputBySource = new Map(pages.map((page) => [page.docPath, page.output]));

  await removeDirectoryContents(officialDir);

  for (const page of pages) {
    await writeMarkdown(
      page.output,
      bodyMarkdown(page.title, page.sourceUrl, page.category.title, page.markdown, page.output, outputBySource)
    );
  }

  for (const category of categories) {
    const categoryPages = pages
      .filter((page) => page.category === category)
      .sort((a, b) => {
        if (a.isRoot !== b.isRoot) {
          return a.isRoot ? -1 : 1;
        }
        return a.order - b.order || a.title.localeCompare(b.title, "zh-Hans-CN");
      });
    const categoryReadme = path.join(officialDir, category.dir, "README.md");
    const list = categoryPages.map((page) => `- ${readmeLink(categoryReadme, page.output, page.title)}`).join("\n");
    await writeMarkdown(
      categoryReadme,
      [
        `# ${category.title}`,
        "",
        `> 官方栏目: ${DOC_ROOT}${category.root}`,
        `> 文档页数: ${categoryPages.length}`,
        "",
        "## 文档列表",
        "",
        list,
      ].join("\n")
    );
  }

  const officialRootReadme = path.join(officialDir, "README.md");
  await writeMarkdown(
    officialRootReadme,
    [
      "# 官方整理版",
      "",
      `> 来源: ${QUANT_ROOT}`,
      `> 官方正文页数: ${pages.length}`,
      "",
      "## 栏目",
      "",
      ...categories.map((category) =>
        `- ${readmeLink(officialRootReadme, path.join(officialDir, category.dir, "README.md"), category.title)}`
      ),
    ].join("\n")
  );

  await writeCompactDocs();

  const rootReadme = path.join(docsDir, "README.md");
  const fullIndex = path.join(docsDir, "FULL_INDEX.md");
  const fullIndexLines = [
    "# 通达信量化平台 (TdxQuant) 完整索引",
    "",
    `> 来源: ${QUANT_ROOT}`,
    `> 整理日期: ${new Date().toISOString().slice(0, 10)}`,
    `> 官方正文页数: ${pages.length}`,
    "",
    "## 目录",
    "",
  ];

  for (const category of categories) {
    const categoryPages = pages
      .filter((page) => page.category === category)
      .sort((a, b) => {
        if (a.isRoot !== b.isRoot) {
          return a.isRoot ? -1 : 1;
        }
        return a.order - b.order || a.title.localeCompare(b.title, "zh-Hans-CN");
      });
    const categoryReadme = path.join(officialDir, category.dir, "README.md");
    fullIndexLines.push(`### ${category.title}`);
    fullIndexLines.push("");
    fullIndexLines.push(`- ${readmeLink(fullIndex, categoryReadme, `${category.title}索引`)}`);
    for (const page of categoryPages) {
      fullIndexLines.push(`- ${readmeLink(fullIndex, page.output, page.title)}`);
    }
    fullIndexLines.push("");
  }

  fullIndexLines.push("## 原始文件");
  fullIndexLines.push("");
  fullIndexLines.push("旧的扁平 Markdown 导出已原样归档到 [raw](./raw/)。");
  await writeMarkdown(fullIndex, fullIndexLines.join("\n"));

  await writeMarkdown(
    rootReadme,
    [
      "# 通达信量化平台 (TdxQuant) 文档",
      "",
      `> 来源: ${QUANT_ROOT}`,
      `> 整理日期: ${new Date().toISOString().slice(0, 10)}`,
      `> 官方正文页数: ${pages.length}`,
      "",
      "## 先看精简版",
      "",
      "- [精简版入口](./compact/README.md)",
      "- [快速开始](./compact/quick-start.md)",
      "- [API 速查](./compact/api-cheatsheet.md)",
      "- [常见工作流](./compact/workflows.md)",
      "- [FAQ](./compact/faq.md)",
      "",
      "## 完整资料",
      "",
      "- [完整索引](./FULL_INDEX.md)",
      "- [按官方栏目整理的 79 页文档](./official/README.md)",
      "- [原始扁平导出归档](./raw/)",
    ].join("\n")
  );

  const bodyDocs = pages.length;
  const readmes = 2 + categories.length;
  console.log(
    JSON.stringify(
      { bodyDocs, compactDocs: 5, officialReadmes: readmes - 1, rootReadme: 1, fullIndex: 1, officialDir },
      null,
      2
    )
  );
}

await main();
