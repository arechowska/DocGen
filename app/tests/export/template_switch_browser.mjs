import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";

const here = dirname(fileURLToPath(import.meta.url));
const appDir = resolve(here, "../..");
const worktreeDir = resolve(appDir, "..");

const chromeCandidates = [
  process.env.DOCGEN_CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].filter(Boolean);
const chromePath = chromeCandidates.find((candidate) => existsSync(candidate));

if (!chromePath) {
  console.log("SKIP: no local Chrome or Edge executable is available for the browser regression.");
  process.exit(0);
}

const pythonCandidates = [
  process.env.DOCGEN_TEST_PYTHON,
  resolve(appDir, ".venv/Scripts/python.exe"),
  resolve(worktreeDir, "../../app/.venv/Scripts/python.exe"),
].filter(Boolean);
const pythonPath = pythonCandidates.find((candidate) => existsSync(candidate));
assert.ok(pythonPath, "Set DOCGEN_TEST_PYTHON to a Python environment with DocGen dependencies.");

const sleep = (milliseconds) => new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds));

async function unusedPort() {
  const server = net.createServer();
  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const { port } = server.address();
  await new Promise((resolveClose) => server.close(resolveClose));
  return port;
}

async function waitFor(description, predicate, timeoutMilliseconds = 10_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(50);
  }
  throw new Error(`Timed out waiting for ${description}${lastError ? `: ${lastError.message}` : ""}`);
}

async function stopProcess(child) {
  if (child.exitCode !== null) return;
  const exited = new Promise((resolveExit) => child.once("exit", resolveExit));
  child.kill();
  await Promise.race([exited, sleep(5_000)]);
}

async function removeTemporaryDirectory(directory) {
  let lastError;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      rmSync(directory, { recursive: true, force: true });
      return;
    } catch (error) {
      lastError = error;
      await sleep(100);
    }
  }
  throw lastError;
}

class CdpClient {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (!pending) return;
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
      for (const listener of this.listeners.get(message.method) || []) listener(message.params);
    });
  }

  async open() {
    await new Promise((resolveOpen, rejectOpen) => {
      this.socket.addEventListener("open", resolveOpen, { once: true });
      this.socket.addEventListener("error", rejectOpen, { once: true });
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolveCall, rejectCall) => {
      this.pending.set(id, { resolve: resolveCall, reject: rejectCall });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(client, expression) {
  const response = await client.call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text);
  return response.result.value;
}

async function createProject(baseUrl) {
  const response = await fetch(`${baseUrl}/projects`, {
    method: "POST",
    body: new URLSearchParams({ name: "Browser template switch" }),
    headers: { "content-type": "application/x-www-form-urlencoded" },
    redirect: "manual",
  });
  assert.equal(response.status, 303, "project creation must redirect to its detail page");
  const location = response.headers.get("location");
  assert.ok(location, "project creation must return a project detail location");
  return new URL(location, baseUrl).href;
}

const testDirectory = mkdtempSync(resolve(tmpdir(), "docgen-template-switch-"));
const browserProfile = mkdtempSync(resolve(tmpdir(), "docgen-template-switch-chrome-"));
const appPort = await unusedPort();
const chromePort = await unusedPort();
const baseUrl = `http://127.0.0.1:${appPort}`;
const databasePath = resolve(testDirectory, "docgen.db").replaceAll("\\", "/");
const server = spawn(
  pythonPath,
  ["-m", "uvicorn", "docgen.main:app", "--app-dir", "src", "--host", "127.0.0.1", "--port", String(appPort)],
  {
    cwd: appDir,
    env: {
      ...process.env,
      DOCGEN_DATABASE_URL: `sqlite:///${databasePath}`,
      DOCGEN_DATA_DIR: resolve(testDirectory, "data"),
    },
    stdio: "pipe",
    windowsHide: true,
  },
);
const browser = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    `--remote-debugging-port=${chromePort}`,
    `--user-data-dir=${browserProfile}`,
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true },
);

try {
  await waitFor("the DocGen test server", async () => (await fetch(`${baseUrl}/health`)).ok);
  const projectUrl = await createProject(baseUrl);
  const pageTarget = await waitFor("a Chrome DevTools page target", async () => {
    const response = await fetch(`http://127.0.0.1:${chromePort}/json/list`);
    if (!response.ok) return null;
    return (await response.json()).find((target) => target.type === "page") || null;
  });
  const client = new CdpClient(pageTarget.webSocketDebuggerUrl);
  await client.open();
  await client.call("Page.enable");
  await client.call("Runtime.enable");
  await client.call("Network.enable");

  const templateRequests = [];
  client.on("Network.requestWillBeSent", ({ request }) => {
    if (!request.url.includes("/export/templates")) return;
    const url = new URL(request.url);
    templateRequests.push({
      format: url.searchParams.get("format"),
      semanticTemplateId: url.searchParams.get("semantic_template_id"),
    });
  });

  await client.call("Page.navigate", { url: projectUrl });
  await waitFor("the initial HTMX export options swap", () =>
    evaluate(client, "Boolean(document.querySelector('#export-template-select select[hx-trigger]'))"),
  );

  templateRequests.length = 0;
  await evaluate(
    client,
    "(() => { const select = document.querySelector('#formatSelect'); select.value = 'html'; select.dispatchEvent(new Event('change', { bubbles: true })); })()",
  );

  await waitFor("the HTML no-template request", () =>
    templateRequests.find((request) => request.format === "html" && request.semanticTemplateId === "no-template"),
  );
  await waitFor("the manual HTML trigger", async () =>
    (await evaluate(client, "document.querySelector('#export-template-select select')?.getAttribute('hx-trigger')"))?.includes("docgen:html-build from:body"),
  );

  templateRequests.length = 0;
  await evaluate(
    client,
    "(() => { const select = document.querySelector('#templateSelect'); select.value = 'faq'; select.dispatchEvent(new Event('change', { bubbles: true })); })()",
  );
  await waitFor("the FAQ template-options request", () =>
    templateRequests.find((request) => request.format === "html" && request.semanticTemplateId === "faq"),
  );
  const faqTrigger = await waitFor("the automatic FAQ trigger", async () => {
    const trigger = await evaluate(client, "document.querySelector('#export-template-select select')?.getAttribute('hx-trigger')");
    return trigger?.includes("docgen:document-updated from:body") ? trigger : null;
  });
  assert.ok(!faqTrigger.includes("docgen:html-build from:body"));

  templateRequests.length = 0;
  await evaluate(
    client,
    "(() => { const select = document.querySelector('#templateSelect'); select.value = 'no-template'; select.dispatchEvent(new Event('change', { bubbles: true })); })()",
  );
  await waitFor("the no-template template-options request", () =>
    templateRequests.find((request) => request.format === "html" && request.semanticTemplateId === "no-template"),
  );
  const noTemplateTrigger = await waitFor("the manual no-template trigger", async () => {
    const trigger = await evaluate(client, "document.querySelector('#export-template-select select')?.getAttribute('hx-trigger')");
    return trigger?.includes("docgen:html-build from:body") ? trigger : null;
  });
  assert.ok(!noTemplateTrigger.includes("docgen:document-updated from:body"));

  client.close();
  console.log("PASS: HTML export options refresh for FAQ and no-template semantic switches.");
} finally {
  await stopProcess(server);
  await stopProcess(browser);
  await removeTemporaryDirectory(testDirectory);
  await removeTemporaryDirectory(browserProfile);
}