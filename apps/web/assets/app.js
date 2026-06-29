"use strict";

const ui = {
  form: document.querySelector("#report-form"),
  reportTypes: Array.from(document.querySelectorAll(".report-type")),
  subject: document.querySelector("#subject"),
  reportDate: document.querySelector("#report-date"),
  length: document.querySelector("#length"),
  focus: document.querySelector("#focus"),
  matchStageField: document.querySelector("#match-stage-field"),
  matchStage: document.querySelector("#match-stage"),
  generateButton: document.querySelector("#generate-button"),
  formError: document.querySelector("#form-error"),
  providerStatus: document.querySelector("#provider-status"),
  modelBudget: document.querySelector("#model-budget"),
  toolBudget: document.querySelector("#tool-budget"),
  harnessSteps: document.querySelector("#harness-steps"),
  result: document.querySelector("#result"),
  resultMeta: document.querySelector("#result-meta"),
  reportOutput: document.querySelector("#report-output"),
  copyReport: document.querySelector("#copy-report"),
  downloadReport: document.querySelector("#download-report"),
  runsList: document.querySelector("#runs-list"),
};

const typeDefaults = {
  world_cup_daily: {
    subject: "世界杯每日观察｜淘汰赛焦点与今日看点",
    focus: "昨日赛果, 晋级形势, 今日看点",
    skill: "world-cup-daily",
  },
  transfer_daily: {
    subject: "夏季转会窗｜今日重点进展与可信度整理",
    focus: "实质进展, 报价与协议, 冲突消息",
    skill: "transfer-daily",
  },
  match_prediction: {
    subject: "世界杯淘汰赛焦点战｜赛前预测报告",
    focus: "90分钟概率, 晋级倾向, 正反证据",
    skill: "match-prediction",
  },
};

let selectedType = "world_cup_daily";
let capabilities = null;
let latestPayload = null;

function localDateValue() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function currentSkill() {
  if (!capabilities) return null;
  return capabilities.skills.find((skill) => skill.report_type === selectedType);
}

function applySelectedType(type) {
  selectedType = type;
  ui.reportTypes.forEach((button) => {
    const isSelected = button.dataset.reportType === type;
    button.classList.toggle("selected", isSelected);
    button.setAttribute("aria-checked", String(isSelected));
  });
  const defaults = typeDefaults[type];
  ui.subject.value = defaults.subject;
  ui.focus.value = defaults.focus;
  ui.matchStageField.hidden = type !== "match_prediction";
  updateHarnessPreview();
}

function updateHarnessPreview() {
  const skill = currentSkill();
  const defaults = typeDefaults[selectedType];
  ui.modelBudget.textContent = skill?.max_model_rounds ?? "—";
  ui.toolBudget.textContent = skill?.max_tool_rounds ?? "—";
  const firstStep = ui.harnessSteps.querySelector("li small");
  if (firstStep) firstStep.textContent = defaults.skill;
  ui.harnessSteps.querySelectorAll("li").forEach((step, index) => {
    step.classList.toggle("active", index === 0);
    step.classList.remove("done");
  });
}

function setRunning(running) {
  ui.generateButton.disabled = running;
  ui.generateButton.querySelector("span").textContent = running
    ? "Harness 运行中"
    : "生成报告";
  if (running) {
    ui.harnessSteps.querySelectorAll("li").forEach((step, index) => {
      step.classList.toggle("active", index === 1);
      step.classList.toggle("done", index === 0);
    });
  }
}

function evidenceFor(type, cutoff) {
  const titles = {
    world_cup_daily: "世界杯日报演示资料包",
    transfer_daily: "转会报告演示资料包",
    match_prediction: "比赛预测演示上下文",
  };
  const published = new Date(new Date(cutoff).getTime() - 60 * 60 * 1000);
  return [
    {
      id: `demo-${type}-1`,
      title: titles[type],
      url: `https://example.com/demo/${type}`,
      published_at: published.toISOString(),
      source_name: "本地演示数据源",
      summary:
        "这是用于验证页面、Harness 和后端闭环的演示证据，不代表真实比赛或转会事实。",
    },
  ];
}

function requestPayload() {
  const cutoff = new Date().toISOString();
  const payload = {
    report_type: selectedType,
    subject: ui.subject.value.trim(),
    report_date: ui.reportDate.value,
    data_cutoff: cutoff,
    length: ui.length.value,
    focus: ui.focus.value
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 8),
    evidence: evidenceFor(selectedType, cutoff),
  };
  if (selectedType === "match_prediction") {
    payload.match_stage = ui.matchStage.value;
  }
  return payload;
}

function renderHarness(trace) {
  const items = Array.from(ui.harnessSteps.querySelectorAll("li"));
  items.forEach((item, index) => {
    item.classList.remove("active");
    item.classList.toggle("done", index < trace.steps.length);
    const step = trace.steps[index];
    if (step) {
      item.querySelector("strong").textContent = step.label;
      item.querySelector("small").textContent = step.detail;
    }
  });
  ui.modelBudget.textContent = `${trace.model_rounds_used}/${trace.max_model_rounds}`;
  ui.toolBudget.textContent = `${trace.tool_rounds_used}/${trace.max_tool_rounds}`;
}

function renderPrediction(prediction) {
  if (!prediction) return null;
  const block = createElement("section", "prediction-block");
  const heading = createElement("h3", "", "AI 概率判断");
  const grid = createElement("div", "prediction-grid");
  [
    ["主胜", prediction.home_win],
    ["平局", prediction.draw],
    ["客胜", prediction.away_win],
  ].forEach(([label, value]) => {
    const cell = createElement("div", "prediction-cell");
    cell.append(
      createElement("strong", "", `${Math.round(value * 100)}%`),
      createElement("span", "", label),
    );
    grid.append(cell);
  });
  block.append(heading, grid);
  return block;
}

function renderReport(payload) {
  latestPayload = payload;
  const { report } = payload.report;
  ui.result.hidden = false;
  ui.resultMeta.textContent = [
    payload.report.provider,
    payload.report.model,
    `Skill ${payload.run.skill_id}@${payload.run.skill_version}`,
    `${payload.run.model_rounds_used} 轮`,
  ].join(" · ");
  ui.reportOutput.replaceChildren();
  ui.reportOutput.append(
    createElement("h2", "", report.title),
    createElement("p", "report-summary", report.executive_summary),
  );
  const prediction = renderPrediction(report.prediction);
  if (prediction) ui.reportOutput.append(prediction);

  report.sections.forEach((section) => {
    const wrapper = createElement("section", "report-section");
    wrapper.append(
      createElement("h3", "", section.heading),
      createElement("p", "", section.body),
    );
    const tags = createElement("div", "evidence-tags");
    section.evidence_ids.forEach((id) => tags.append(createElement("span", "", id)));
    wrapper.append(tags);
    ui.reportOutput.append(wrapper);
  });

  if (report.warnings.length) {
    const warnings = createElement("section", "warnings");
    warnings.append(createElement("strong", "", "使用前请复核"));
    report.warnings.forEach((warning) =>
      warnings.append(createElement("p", "", warning)),
    );
    ui.reportOutput.append(warnings);
  }
  renderHarness(payload.run);
  ui.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function reportAsText() {
  if (!latestPayload) return "";
  const report = latestPayload.report.report;
  const lines = [`# ${report.title}`, "", report.executive_summary, ""];
  report.sections.forEach((section) => {
    lines.push(`## ${section.heading}`, "", section.body, "");
  });
  if (report.warnings.length) {
    lines.push("## 使用前请复核", "", ...report.warnings.map((item) => `- ${item}`));
  }
  return lines.join("\n");
}

async function loadCapabilities() {
  try {
    const response = await fetch("/v1/system/capabilities");
    if (!response.ok) throw new Error("status unavailable");
    capabilities = await response.json();
    ui.providerStatus.textContent = `${capabilities.provider.toUpperCase()} · ${capabilities.model}`;
    updateHarnessPreview();
  } catch {
    ui.providerStatus.textContent = "后端状态暂不可用";
  }
}

async function loadRuns() {
  try {
    const response = await fetch("/v1/runs");
    if (!response.ok) return;
    const runs = await response.json();
    if (!runs.length) return;
    ui.runsList.replaceChildren();
    runs.forEach((run) => {
      const row = createElement("div", "run-row");
      row.append(
        createElement("strong", "", run.skill_id),
        createElement("span", "", `${run.evidence_count} 条证据 · ${run.steps.length} 个检查点`),
        createElement("small", "", `${run.model_rounds_used}/${run.max_model_rounds} 模型轮次`),
        createElement("span", "run-state", run.status),
      );
      ui.runsList.append(row);
    });
  } catch {
    // Keep the empty state; run history is non-critical.
  }
}

ui.reportTypes.forEach((button) => {
  button.addEventListener("click", () => applySelectedType(button.dataset.reportType));
});

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  ui.formError.textContent = "";
  if (!ui.subject.value.trim()) {
    ui.formError.textContent = "请填写报告主题。";
    return;
  }
  setRunning(true);
  try {
    const response = await fetch("/v1/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload()),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "报告生成失败");
    }
    renderReport(payload);
    await loadRuns();
  } catch (error) {
    ui.formError.textContent = error.message || "报告生成失败，请稍后重试。";
  } finally {
    setRunning(false);
  }
});

ui.copyReport.addEventListener("click", async () => {
  const text = reportAsText();
  if (!text) return;
  await navigator.clipboard.writeText(text);
  ui.copyReport.textContent = "已复制";
  window.setTimeout(() => {
    ui.copyReport.textContent = "复制文本";
  }, 1400);
});

ui.downloadReport.addEventListener("click", () => {
  if (!latestPayload) return;
  const blob = new Blob([JSON.stringify(latestPayload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `footpulse-${latestPayload.run.run_id}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
});

ui.reportDate.value = localDateValue();
loadCapabilities();
loadRuns();

