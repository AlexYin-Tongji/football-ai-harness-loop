"use strict";

const $ = (selector) => document.querySelector(selector);
const ui = {
  form: $("#report-form"),
  cards: [...document.querySelectorAll(".report-card")],
  subject: $("#subject"),
  date: $("#report-date"),
  length: $("#length"),
  focus: $("#focus"),
  stageField: $("#match-stage-field"),
  stage: $("#match-stage"),
  hint: $("#brief-hint"),
  readiness: $("#readiness"),
  button: $("#generate-button"),
  progress: $("#research-progress"),
  error: $("#form-error"),
  result: $("#result"),
  meta: $("#result-meta"),
  output: $("#report-output"),
  edit: $("#edit-report"),
  copy: $("#copy-report"),
  download: $("#download-report"),
};

const defaults = {
  daily_football_digest: {
    subject: "今日球脉｜世界杯与夏季转会窗",
    focus: "世界杯, 今日看点, 转会进展, 绯闻雷达",
    hint: "赛事与转会由两个研究桌分别处理，再由总编辑整合成一份每日情报。",
  },
  world_cup_daily: {
    subject: "FIFA World Cup 2026｜今日重点与淘汰赛观察",
    focus: "昨日赛果, 晋级形势, 今日看点",
    hint: "整理近期世界杯赛果、晋级变化和今日重点比赛。",
  },
  transfer_daily: {
    subject: "Summer transfer window｜今日重要进展",
    focus: "实质进展, 报价与协议, 冲突消息",
    hint: "聚合近期转会报道，只保留状态或可信度真正变化的消息。",
  },
  match_prediction: {
    subject: "England vs Ghana｜World Cup 赛前预测",
    focus: "胜平负概率, 晋级倾向, 正反证据",
    hint: "请写明两队英文名；不同分析视角会独立判断，再给出概率与未知项。",
  },
};

let selected = "daily_football_digest";
let latest = null;
let latestUsedEvidence = [];
let editing = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function choose(type) {
  selected = type;
  ui.cards.forEach((card) => {
    const active = card.dataset.reportType === type;
    card.classList.toggle("selected", active);
    card.setAttribute("aria-checked", String(active));
  });
  ui.subject.value = defaults[type].subject;
  ui.focus.value = defaults[type].focus;
  ui.hint.textContent = defaults[type].hint;
  ui.stageField.hidden = type !== "match_prediction";
}

function requestPayload() {
  const data = {
    report_type: selected,
    subject: ui.subject.value.trim(),
    report_date: ui.date.value,
    length: ui.length.value,
    focus: ui.focus.value
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 8),
  };
  if (selected === "match_prediction") data.match_stage = ui.stage.value;
  return data;
}

function predictionView(prediction) {
  if (!prediction) return null;
  const block = el("section", "prediction-block");
  block.append(el("h3", "", "球脉综合预测"));
  const grid = el("div", "prediction-grid");
  [
    ["主胜", prediction.home_win],
    ["平局", prediction.draw],
    ["客胜", prediction.away_win],
  ].forEach(([label, value]) => {
    const cell = el("div", "prediction-cell");
    cell.append(
      el("strong", "", `${Math.round(value * 100)}%`),
      el("span", "", label),
    );
    grid.append(cell);
  });
  block.append(grid);
  if (prediction.statistical_baseline) {
    const baseline = prediction.statistical_baseline;
    const baselineRow = el("div", "statistical-baseline");
    baselineRow.append(
      el("strong", "", "可复现统计基线"),
      el(
        "p",
        "",
        `主胜 ${Math.round(baseline.home_win * 100)}% · 平 ${Math.round(baseline.draw * 100)}% · 客胜 ${Math.round(baseline.away_win * 100)}%`,
      ),
      el(
        "small",
        "",
        `${baseline.method === "elo_poisson" ? "Elo + Poisson" : "Poisson"} · 预期进球 ${baseline.expected_home_goals}:${baseline.expected_away_goals} · 样本 ${baseline.sample_size_home}/${baseline.sample_size_away} 场`,
      ),
    );
    block.append(baselineRow);
  }
  if (prediction.external_predictions?.length) {
    const comparisons = el("div", "external-predictions");
    comparisons.append(el("h4", "", "外部观点对照"));
    prediction.external_predictions.forEach((item) => {
      const row = el("div", "external-prediction");
      row.append(el("strong", "", item.source_name), el("p", "", item.summary));
      if (item.home_win !== null && item.home_win !== undefined) {
        row.append(
          el(
            "small",
            "",
            `主胜 ${Math.round(item.home_win * 100)}% · 平 ${Math.round(item.draw * 100)}% · 客胜 ${Math.round(item.away_win * 100)}%`,
          ),
        );
      }
      comparisons.append(row);
    });
    block.append(comparisons);
  }
  return block;
}

function editableNode(tag, className, text, exportKind) {
  const node = el(tag, className, text);
  node.dataset.editable = "true";
  node.dataset.export = exportKind;
  return node;
}

function sourceLink(evidence) {
  const lead = evidence.verification_status === "unverified_lead";
  const link = el(
    "a",
    `evidence-link${lead ? " unverified" : ""}`,
    `${lead ? "未核实线索" : "来源"}：${evidence.title}`,
  );
  link.href = evidence.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.title = evidence.title;
  return link;
}

function render(data) {
  latest = data;
  editing = false;
  ui.edit.textContent = "编辑报告";
  const report = data.report.report;
  const evidenceById = new Map(data.evidence.map((item) => [item.id, item]));
  const usedIds = new Set(
    report.sections.flatMap((section) => section.evidence_ids),
  );
  if (report.prediction) {
    [
      ...report.prediction.supporting_factors,
      ...report.prediction.counter_factors,
    ].forEach((factor) => factor.evidence_ids.forEach((id) => usedIds.add(id)));
  }
  latestUsedEvidence = data.evidence.filter((item) => usedIds.has(item.id));
  const publisherCount = new Set(data.evidence.map((item) => item.source_name)).size;
  ui.result.hidden = false;
  ui.meta.textContent = `生成于 ${new Date(data.report.generated_at).toLocaleString("zh-CN")} · ${publisherCount} 家来源 · ${data.evidence.length} 条近期资料`;
  ui.output.replaceChildren(
    editableNode("h2", "", report.title, "title"),
    editableNode("p", "report-summary", report.executive_summary, "summary"),
  );

  const prediction = predictionView(report.prediction);
  if (prediction) ui.output.append(prediction);

  report.sections.forEach((section, index) => {
    const wrapper = el("section", "report-section");
    wrapper.dataset.section = String(index);
    wrapper.append(
      editableNode("h3", "", section.heading, "heading"),
      editableNode("p", "", section.body, "body"),
    );
    const links = el("div", "evidence-tags");
    section.evidence_ids.forEach((id) => {
      const evidence = evidenceById.get(id);
      if (evidence) links.append(sourceLink(evidence));
    });
    wrapper.append(links);
    ui.output.append(wrapper);
  });

  if (report.warnings.length) {
    const warnings = el("section", "warnings");
    warnings.append(el("strong", "", "发布前请复核"));
    report.warnings.forEach((item) => warnings.append(el("p", "", item)));
    ui.output.append(warnings);
  }

  const sources = el("section", "source-list");
  sources.append(el("h3", "", "本报告使用的来源"));
  const list = el("ol", "");
  latestUsedEvidence.forEach((item) => {
    const row = el("li", "");
    const link = el("a", "", item.title);
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(
      link,
      el(
        "small",
        "",
        `${item.source_name} · ${new Date(item.published_at).toLocaleString("zh-CN")}`,
      ),
    );
    list.append(row);
  });
  sources.append(list);
  ui.output.append(sources);
  ui.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function reportAsText() {
  if (!latest) return "";
  const title = ui.output.querySelector('[data-export="title"]')?.innerText || "";
  const summary =
    ui.output.querySelector('[data-export="summary"]')?.innerText || "";
  const lines = [`# ${title}`, "", summary, ""];
  ui.output.querySelectorAll("[data-section]").forEach((section) => {
    const heading = section.querySelector('[data-export="heading"]')?.innerText;
    const body = section.querySelector('[data-export="body"]')?.innerText;
    lines.push(`## ${heading}`, "", body, "");
  });
  lines.push("## 来源", "");
  latestUsedEvidence.forEach((item) =>
    lines.push(`- ${item.title}：${item.url}`),
  );
  return lines.join("\n");
}

function startProgress() {
  ui.progress.hidden = false;
  const steps = [...ui.progress.querySelectorAll("span")];
  steps.forEach((step, index) => step.classList.toggle("active", index === 0));
}

function stopProgress() {
  ui.progress.hidden = true;
}

function updateProgress(progress) {
  const steps = [...ui.progress.querySelectorAll("span")];
  const current = Math.min(
    steps.length - 1,
    Math.floor((Math.max(0, progress) / 100) * steps.length),
  );
  steps.forEach((step, index) => step.classList.toggle("active", index <= current));
}

async function waitForJob(jobId) {
  for (let attempt = 0; attempt < 900; attempt += 1) {
    const response = await fetch(`/v1/research/jobs/${jobId}`);
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || "无法读取任务进度");
    updateProgress(job.progress);
    if (job.status === "completed") return job.result;
    if (job.status === "failed") throw new Error(job.error || "报告生成失败");
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error("研究任务超时，请稍后重试");
}

async function loadStatus() {
  try {
    const response = await fetch("/v1/product/status");
    const status = await response.json();
    ui.readiness.classList.toggle("live", status.generation_ready);
    ui.readiness.querySelector("b").textContent = status.generation_ready
      ? "实时资料与 AI 已连接"
      : "当前为体验模式";
  } catch {
    ui.readiness.querySelector("b").textContent = "服务暂时不可用";
  }
}

ui.cards.forEach((card) => {
  card.addEventListener("click", () => choose(card.dataset.reportType));
});

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  ui.error.textContent = "";
  if (ui.subject.value.trim().length < 3) {
    ui.error.textContent = "请填写更具体的报告主题。";
    return;
  }
  ui.button.disabled = true;
  ui.button.querySelector("span").textContent = "正在研究…";
  startProgress();
  try {
    const response = await fetch("/v1/research/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload()),
    });
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || "报告生成失败");
    const data = await waitForJob(job.id);
    render(data);
  } catch (error) {
    ui.error.textContent = error.message || "暂时无法生成，请稍后再试。";
  } finally {
    stopProgress();
    ui.button.disabled = false;
    ui.button.querySelector("span").textContent = "生成我的报告";
  }
});

ui.edit.addEventListener("click", () => {
  editing = !editing;
  ui.output.querySelectorAll("[data-editable]").forEach((node) => {
    node.contentEditable = String(editing);
  });
  ui.output.classList.toggle("editing", editing);
  ui.edit.textContent = editing ? "完成编辑" : "编辑报告";
});

ui.copy.addEventListener("click", async () => {
  await navigator.clipboard.writeText(reportAsText());
  ui.copy.textContent = "已复制";
  window.setTimeout(() => (ui.copy.textContent = "复制全文"), 1200);
});

ui.download.addEventListener("click", () => {
  if (!latest) return;
  const blob = new Blob([reportAsText()], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `footpulse-${latest.run.run_id}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
});

const now = new Date();
const offset = now.getTimezoneOffset() * 60_000;
ui.date.value = new Date(now.getTime() - offset).toISOString().slice(0, 10);
loadStatus();
