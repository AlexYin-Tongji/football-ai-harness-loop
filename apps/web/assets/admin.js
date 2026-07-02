"use strict";

const $ = (selector) => document.querySelector(selector);
const form = $("#admin-form");
const token = $("#admin-token");
const output = $("#admin-health");
const error = $("#admin-error");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusLabel(status) {
  return {
    healthy: "可用",
    degraded: "部分可用",
    needs_attention: "需要处理",
    not_configured: "未配置",
    unknown: "已配置待验证",
    covered: "已覆盖",
    not_covered: "未覆盖",
    unauthorized: "无权限",
    rate_limited: "限流",
    error: "异常",
  }[status] || status;
}

function card(title, item) {
  const node = el("article", `health-card ${item.status}`);
  node.append(el("span", "", statusLabel(item.status)), el("h3", "", title));
  node.append(el("p", "", item.message));
  return node;
}

function renderHealth(data) {
  output.hidden = false;
  output.replaceChildren();
  const head = el("div", "health-summary");
  head.append(
    el("span", "", `总体：${statusLabel(data.overall_status)}`),
    el("small", "", new Date(data.generated_at).toLocaleString("zh-CN")),
  );
  output.append(head);

  const grid = el("div", "health-grid");
  grid.append(
    card("Sportmonks", data.sportmonks),
    card("football-data.org", data.football_data),
    card("NewsAPI", data.news_api),
    card("Commons 图片", data.media.commons),
    card("YouTube 视频", data.media.youtube),
    card("视觉相关性", data.media.visual_relevance),
  );
  output.append(grid);

  const leagues = el("section", "league-coverage");
  leagues.append(el("h2", "", "Sportmonks 五大联赛覆盖"));
  data.sportmonks.big_five_leagues.forEach((league) => {
    const row = el("div", `league-row ${league.status}`);
    row.append(
      el("strong", "", `${league.name} · ${league.country}`),
      el("span", "", statusLabel(league.status)),
      el("p", "", league.message),
    );
    if (league.matched_league_ids?.length) {
      row.append(el("small", "", `League IDs: ${league.matched_league_ids.join(", ")}`));
    }
    leagues.append(row);
  });
  output.append(leagues);

  if (data.recommendations?.length) {
    const recommendations = el("section", "health-recommendations");
    recommendations.append(el("h2", "", "建议"));
    data.recommendations.forEach((item) => recommendations.append(el("p", "", item)));
    output.append(recommendations);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  output.hidden = true;
  try {
    const response = await fetch("/v1/admin/connector-health", {
      headers: { "X-Admin-Token": token.value },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "无法读取连接器健康状态");
    renderHealth(data);
  } catch (exc) {
    error.textContent = exc.message || "检查失败";
  }
});
