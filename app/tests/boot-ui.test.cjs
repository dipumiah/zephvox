const { JSDOM } = require(require("path").join(__dirname, "..", "..", "web", "node_modules", "jsdom"));
const fs = require("fs");
const html = fs.readFileSync(require("path").join(__dirname, "..", "ui", "index.html"), "utf8");
let handler; const invoked = [];
const dom = new JSDOM(html, { runScripts: "dangerously", beforeParse(w) {
  w.__TAURI__ = {
    event: { listen: (n, cb) => { handler = cb; } },
    core: { invoke: (cmd) => { invoked.push(cmd); return Promise.resolve({ step: null, items: [], logs: [], error: null }); } },
  };
}});
const d = dom.window.document;
setTimeout(() => {
  const fire = (p) => handler({ payload: p });
  fire({ type: "step", n: null, title: "Unpacking zephvox" });
  fire({ type: "step", n: 2, total: 7, title: "Downloading models" });
  fire({ type: "download", id: "a", label: "whisper", state: "downloading", done: 5e8, total: 1e9 });
  fire({ type: "download", id: "b", label: "wav2vec2", state: "done", done: 1e9, total: 1e9 });
  const assert = (c, m) => { if (!c) { console.error("FAIL", m); process.exit(1); } };
  assert(d.getElementById("title").textContent === "Downloading models", "title");
  assert(d.getElementById("sub").textContent === "Setup step 2 of 7", "sub");
  assert(d.getElementById("steps").textContent.includes("Unpacking zephvox"), "history");
  assert(d.querySelectorAll("li.item").length === 2, "items");
  assert(d.getElementById("overall").getAttribute("aria-valuenow") === "75", "overall " + d.getElementById("overall").getAttribute("aria-valuenow"));
  fire({ type: "error", message: "boom" });
  assert(d.getElementById("title").textContent === "zephvox couldn't start", "error title");
  assert(!d.getElementById("retry").hidden, "retry shown");
  d.getElementById("retry").click();
  assert(invoked.includes("boot_retry"), "retry invoked");
  console.log("boot ui ok");
}, 50);
