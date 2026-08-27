// DeckPipe dev launcher: runs the same sidecar entrypoint as the desktop shell.
const { spawn } = require("child_process");
const crypto = require("crypto");
const path = require("path");

const CHILD_ENV_ALLOWLIST = [
  "APPDATA",
  "LOCALAPPDATA",
  "USERPROFILE",
  "TEMP",
  "TMP",
  "SYSTEMROOT",
  "COMSPEC",
  "PATH",
];

function argValue(flag, def) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

function pickChildEnv() {
  const env = {};
  for (const name of CHILD_ENV_ALLOWLIST) {
    if (Object.prototype.hasOwnProperty.call(process.env, name)) {
      env[name] = process.env[name];
    }
  }
  return env;
}

const host = argValue("--host", "127.0.0.1");
const port = argValue("--port", process.env.PORT || "0");
const py = path.join(__dirname, ".venv", "Scripts", "python.exe");
const token = process.env.DECKPIPE_API_TOKEN || crypto.randomBytes(32).toString("hex");

const child = spawn(py, ["run_backend.py", "--host", host, "--port", port], {
  cwd: __dirname,
  env: {
    ...pickChildEnv(),
    DECKPIPE_API_TOKEN: token,
    DECKPIPE_PARENT_PID: String(process.pid),
  },
  stdio: "inherit",
});

child.on("exit", (code) => process.exit(code ?? 0));
