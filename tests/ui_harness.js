// Runs the page's real <script> against a fake DOM + fake server, then simulates clicks.
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const els = {};
function makeEl(id) {
  const e = { id, innerHTML: "", textContent: "", value: "", checked: false, hidden: false, disabled: false, className: "",
    dataset: {}, style: {}, files: [], classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(t, f) { e["on_" + t] = f; }, querySelector() { return null; }, closest() { return null; },
    click() {}, focus() {}, select() {}, showModal() {}, close() {} };
  return e;
}
const el = (s) => (els[s] = els[s] || makeEl(s));
const calls = [];
let lastSource = null, steamKey = false, cancelRequested = false, jobStateOverride = null, lastUpdateMode = null;
const STEAM = {
  steam_match: { pc_games: 5000, already_linked: 10, newly_linked: 3000, linked_via_igdb: 2900, linked_via_steam_list: 100,
    ambiguous_skipped: 50, unmatched: 1940, methods: ["IGDB (exact IDs)", "Steam game list (exact titles)"] },
  steam_probe: { probe: true, asked: 200, with_size: 150, manifest_sizes: 140, maxsize_only: 10, no_size: 40, not_returned: 10, has_original_date: 120, not_a_game: 2, elapsed_s: 12.5, games_per_sec: 16, total_linked: 30000, projected_minutes: 31.3,
    examples: [{ name: "Half-Life", gb: 1.2, kind: "manifest", depots: 2 }], compared_with_gog: [], median_ratio_vs_gog: 1.4, compared_count: 30 },
  steam_sizes: { updated: 1234, notes: [] },
  steam_catalog: { to_check: 40000, games_added: 3000, linked_to_existing: 500, skipped_not_games: 8000, already_linked: 110000, already_checked: 20000, apps_on_steam: 180000 },
  steamspy_scores: { pages: 5, resumed_from_page: 10, scores_saved: 4000, genres_saved: 3800, too_few_reviews: 900 },
  steam_prices: { updated: 5000, notes: [] },
  rawg_consoles: { updated: 8000, notes: ["rawg: needs setup (rawg.api_key)"] },
  wikidata_consoles: { updated: 3000, notes: [] },
};
const UPDATE_JOBS = {
  new: { mode: "new", lines: ["GOG catalog: 500 read, 20 new, 480 already had", "IGDB new games: skipped (not set up yet)", "Steam new games: skipped (not set up yet)"], notes: ["IGDB new games: skipped (not set up yet)", "Steam new games: skipped (not set up yet)"] },
};
const respond = (url, body) => {
  if (url.startsWith("/api/facets")) return { platforms: [{ value: "pc", n: 3 }, { value: "ps2", n: 5 }], genres: [{ value: "indie", n: 2 }],
    regions: [{ value: "USA", n: 4 }], drm: [{ value: "drm-free", n: 2 }], sets: [], ranges: {}, total: 8 };
  if (url.startsWith("/api/query")) return { rows: [{ id: 1, game_id: 1, name: "Okami", platform: "ps2", size_bytes: 4200000000, size_gb: 4.2, fav: 0 }], total: 1, page: 1, page_size: 100 };
  if (url.startsWith("/api/info")) return { name: "TestName", version: "9.9.9" };
  if (url.startsWith("/api/refresh_estimate")) return { seconds: 30, count: 8, to_look_up: 5, sources: ["wikidata"], needs: [] };
  if (url.startsWith("/api/flag_bulk")) return { games: 3 };
  if (url.startsWith("/api/update_source")) { lastSource = body.source; return { job: 1 }; }
  if (url.startsWith("/api/job_cancel")) { cancelRequested = true; return { ok: true }; }
  if (url.startsWith("/api/jobs")) return { jobs: [{ id: 1, title: "Steam depot sizes", state: "running", cancelling: cancelRequested,
    message: cancelRequested ? "Cancelling... stopping at the next safe point" : "1,200/29,000 games", progress: 0.04 }] };
  if (url.startsWith("/api/update_library")) { lastUpdateMode = body.mode; return { job: 2 }; }
  if (url.startsWith("/api/job?id=2")) return { state: "done", message: "Finished", progress: 1, result: UPDATE_JOBS[lastUpdateMode] || { error: "unexpected mode" } };
  if (url.startsWith("/api/job") && jobStateOverride) return jobStateOverride;
  if (url.startsWith("/api/job")) return { state: "done", message: "Finished", progress: 1, result: STEAM[lastSource] || {} };
  if (url.startsWith("/api/stats")) return { platforms: [], total: 0 };
  if (url.startsWith("/api/adapters")) return { ingestors: [], refreshers: [], planned: {}, fields: [], extras: { steam_pics: { installed: false } } };
  if (url.startsWith("/api/config")) return { igdb: { client_id: "", client_secret_set: false }, steam: { api_key_set: (steamKey = steamKey || !!(body && body.steam)) } };
  if (url.startsWith("/api/log?since=0")) return { last: 3, lines: [
    { id: 1, t: "10:00:00", level: "INFO", msg: "Job 1 started: Update steam_match" },
    { id: 2, t: "10:00:01", level: "WARNING", msg: "GET api.igdb.com/v4/games -> 429; retry 1/3 in 7s" },
    { id: 3, t: "10:00:09", level: "ERROR", msg: "Job 1 failed: boom" }] };
  if (url.startsWith("/api/log?since=")) return { last: 3, lines: [] };
  if (url.startsWith("/api/db_info")) return { path: "x", size: 1, releases: 1 };
  if (url.startsWith("/api/status")) return { generated_at: "2024-01-01T00:00:00+00:00",
    counts: { games: 100, releases: 120, claims: 500 },
    file: { path: "x", size: 1048576, modified: "2024-01-01T00:00:00+00:00" },
    schema_version: 4, safety_copies: [], last_library_update: null,
    sources: [{ source: "gog", claims: 50, last_claim: "2024-01-01T00:00:00+00:00", last_run: null, last_activity: "2024-01-01T00:00:00+00:00", color: "green" }],
    platforms: [{ platform: "pc", releases: 100, stats: { platform: "pc", releases: 100, size_pct: 98, size_exact_pct: 40, year_pct: 90, score_pct: 60, genre_pct: 100, price_pct: 70, drm_pct: 60 },
      color: "orange", last_updated: null, last_checked: null, overall_missing_pct: 12.3,
      fields: { year: { label: "Release year", missing: 10, missing_pct: 10, examples: ["Unknown Game A", "Unknown Game B"], last_updated: null, last_checked: null, color: "red" },
                score: { label: "Review score", missing: 40, missing_pct: 40, examples: ["No Score Game"], last_updated: null, last_checked: null, color: "red" },
                genre: { label: "Genre", missing: 0, missing_pct: 0, examples: [], last_updated: "2024-01-01T00:00:00+00:00", last_checked: null, color: "green" },
                size: { label: "Size", missing: 2, missing_pct: 2, examples: ["No Size Game"], last_updated: null, last_checked: null, color: "orange" } } }],
    recent_activity: [{ at: "2024-01-01T00:00:00+00:00", kind: "source", source: "gog_catalog", platforms: ["pc"], ok: true, summary: "20 new games" }] };
  return {};
};
const sandbox = {
  document: { querySelector: el, querySelectorAll: () => [], addEventListener() {}, createElement: () => makeEl("x"), body: makeEl("body"), title: "" },
  localStorage: { getItem: () => null, setItem() {} }, URL: { createObjectURL: () => "" },
  confirm: () => true, prompt: () => null, alert() {}, console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Set, Map, Object, Array, Number, String, Date, encodeURIComponent, URLSearchParams,
  fetch: async (url, opt) => { const body = opt && opt.body && typeof opt.body === "string" ? JSON.parse(opt.body) : null; calls.push([url, body]);
    const data = respond(url, body); return { ok: true, status: 200, json: async () => data, blob: async () => ({}) }; },
};
vm.createContext(sandbox);
vm.runInContext(js + "\n;globalThis.__t={get F(){return F},buildFilters,visCols,clearAllFilters,COLS,S,fieldMode,estimate,loadData,showTab,pollLog,pollJobs,cancelJob,get LOG(){return LOG}};", sandbox);
const T = sandbox.__t;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chip = (v) => ({ target: { closest: () => ({ dataset: { v }, className: "" }) } });
const lastQuery = () => [...calls].reverse().find((c) => c[0].startsWith("/api/query"))[1];
const out = {};
(async () => {
  await sleep(50);
  out.title = { name: els["#app-name"].textContent, ver: els["#app-ver"].textContent };
  out.chipsRendered = els["#f-platform"].innerHTML.includes('data-v="pc"');
  out.rowRendered = els["#grid tbody"].innerHTML.includes("Okami");
  // platform chip: include -> exclude -> clear
  els["#f-platform"].onclick(chip("pc")); out.afterOne = T.buildFilters();
  els["#f-platform"].onclick(chip("pc")); out.afterTwo = T.buildFilters();
  await sleep(350); out.sentTwo = lastQuery().filters; out.countText = els["#f-count"].textContent; out.clearEnabled = !els["#f-clear"].disabled;
  els["#f-platform"].onclick(chip("pc")); out.afterThree = T.buildFilters();
  // mixed: exclude a genre and a region, hide favourites, exclude title text
  els["#f-genre"].onclick(chip("indie")); els["#f-genre"].onclick(chip("indie"));
  els["#f-region"].onclick(chip("USA")); els["#f-region"].onclick(chip("USA"));
  els["#f-drm"].onclick(chip("drm-free"));
  els["#f-qx"].on_input({ target: { value: "demo, beta" } });
  els["#f-fav"].on_input({ target: { value: "no" } });
  els["#f-smax"].on_input({ target: { value: "2" } });
  await sleep(350); out.mixed = lastQuery().filters;
  // Clear all
  els["#f-clear"].onclick(); await sleep(50);
  out.afterClear = { filters: lastQuery().filters, count: els["#f-count"].textContent, disabled: els["#f-clear"].disabled, qx: els["#f-qx"].value, fav: els["#f-fav"].value };
  // columns: deselect all -> reset
  const menu = els["#colmenu"], evt = (k) => ({ stopPropagation() {}, target: { closest: (s) => (s === "[data-all]" ? { dataset: { all: k } } : null) } });
  menu.onclick(evt("0")); out.colsNone = T.visCols().length;
  menu.onclick(evt("1")); out.colsAll = T.visCols().length;
  menu.onclick(evt("d")); out.colsDefault = T.visCols().map((c) => c.id);
  // refresh dialog: all columns forces "only empty"
  els["#rf-field"].value = "all"; els["#rf-field"].onchange(); await sleep(20);
  out.allMode = { checked: els["#rf-empty"].checked, disabled: els["#rf-empty"].disabled };
  const est = [...calls].reverse().find((c) => c[0].startsWith("/api/refresh_estimate"))[1];
  out.estBody = { field: est.field, only_missing: est.only_missing };
  out.estText = els["#rf-est"].textContent;
  els["#rf-field"].value = "price"; els["#rf-field"].onchange(); out.singleMode = { disabled: els["#rf-empty"].disabled };
  // ---- marking: menu applies to all filtered results (after confirm) or just the selection
  const markEvt = (flag, val) => ({ stopPropagation() {}, target: { closest: () => ({ dataset: { flag, val } }) } });
  const lastCall = (u) => [...calls].reverse().find((c) => c[0] === u)[1];
  await els["#markmenu"].onclick(markEvt("played", "1"));
  out.markAll = lastCall("/api/flag_bulk"); out.toast = els["#toast"].textContent;
  await els["#grid"].on_click({ target: { id: "", checked: true, classList: { contains: (c) => c === "rs" }, closest: (s) => (s === "tr" ? { dataset: { id: "1" } } : null) } });
  await els["#markmenu"].onclick(markEvt("favorite", "1"));
  out.markSelected = lastCall("/api/flag_bulk");
  await els["#grid"].on_click({ target: { id: "", classList: { contains: () => false }, closest: (s) => (s === ".tick" ? { dataset: { game: "7" }, classList: { contains: () => false } } : null) } });
  out.tickClick = lastCall("/api/flag");
  out.rowHasTick = els["#grid tbody"].innerHTML.includes('class="tick');
  // ---- Steam sizes card: three steps
  await els["#st-match"].onclick(); await sleep(30);
  out.step1 = els["#st-m1"].textContent;
  await els["#st-probe"].onclick(); await sleep(30);
  out.step2 = { msg: els["#st-m2"].textContent, html: els["#st-probe-out"].innerHTML, sent: lastCall("/api/update_source") };
  await els["#st-sizes"].onclick(); await sleep(30);
  out.step3 = els["#st-m3"].textContent;
  await T.loadData(); out.addon = { text: els["#st-addon"].textContent, installHidden: els["#st-install"].hidden };
  // ---- Steam catalog / SteamSpy / RAWG cards
  await els["#st-catalog"].onclick(); await sleep(30); out.catalogMsg = els["#st-catmsg"].textContent;
  await els["#st-spy"].onclick(); await sleep(30); out.spyMsg = els["#st-spymsg"].textContent;
  els["#rw-key"].value = "RAWGKEY"; await els["#rw-keysave"].onclick(); await sleep(20);
  out.rawgKeySent = [...calls].reverse().find((c) => c[0] === "/api/config" && c[1] && c[1].rawg)[1];
  out.rawgKeyCleared = els["#rw-key"].value;
  await els["#rw-go"].onclick(); await sleep(30); out.rawgMsg = els["#rw-msg"].textContent;
  // ---- Status tab
  // ---- Update: new only
  await els["#ul-new"].onclick(); await sleep(40);
  out.updateNew = { sentMode: lastUpdateMode, out: els["#ul-out"].innerHTML };
  T.showTab("status"); await sleep(40);
  out.status = { platformsHtml: els["#st-platforms"].innerHTML, overviewHtml: els["#st-overview"].innerHTML,
                 sourcesHtml: els["#st-sources"].innerHTML, tabShown: els["#tab-status"].style.display };
  T.showTab("browse");
  // ---- second step-1 run: IGDB only, games left over -> hint about the optional key
  STEAM.steam_match = { ...STEAM.steam_match, linked_via_steam_list: 0, linked_via_igdb: 10, newly_linked: 10, unmatched: 5, methods: ["IGDB (exact IDs)"] };
  await els["#st-match"].onclick(); await sleep(30); out.step1IgdbOnly = els["#st-m1"].textContent;
  // ---- Steam key: saved to the server, field cleared, never echoed back
  els["#st-key"].value = "MYKEY"; await els["#st-keysave"].onclick(); await sleep(20);
  out.keySent = [...calls].reverse().find((c) => c[0] === "/api/config" && c[1])[1]; out.keyCleared = els["#st-key"].value; out.keyStatus = els["#st-keystat"].textContent;
  // ---- live log tab
  T.showTab("log"); await sleep(40);
  out.log = { html: els["#lg-box"].innerHTML, status: els["#lg-status"].textContent, tabShown: els["#tab-log"].style.display, browseHidden: els["#tab-browse"].style.display };
  await T.pollLog(); out.logNextPoll = calls.map((c) => c[0]).filter((u) => u.startsWith("/api/log?since=")).slice(-1)[0];
  els["#lg-level"].on_change({ target: { value: "warn" } }); out.logWarnOnly = els["#lg-box"].innerHTML;
  T.showTab("browse");
  // ---- header pill shows what is running
  await T.pollJobs(); out.pill = { hidden: els["#jobpill"].hidden, text: els["#jobpill"].textContent };
  // ---- Cancel: buttons appear while a job runs, click asks the server, then the UI shows "cancelling"
  out.cancelShown = { header: !els["#jobcancel"].hidden, log: !els["#lg-cancel"].hidden };
  await els["#jobcancel"].onclick(); await sleep(30);
  out.cancelSent = [...calls].reverse().find((c) => c[0] === "/api/job_cancel")[1];
  out.cancelToast = els["#toast"].textContent;
  await T.pollJobs(); out.cancelling = { pill: els["#jobpill"].textContent, headerHidden: els["#jobcancel"].hidden, logHidden: els["#lg-cancel"].hidden };
  // ---- a cancelled job reports calmly and refreshes what was kept
  jobStateOverride = { state: "cancelled", message: "Cancelled. Everything saved before you cancelled was kept.", progress: 0.4 };
  const before = calls.length;
  await els["#st-sizes"].onclick(); await sleep(40);
  out.cancelledMsg = { text: els["#st-m3"].textContent, cls: els["#st-m3"].className, refreshed: calls.slice(before).some((c) => c[0].startsWith("/api/query")) };
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(1); });
