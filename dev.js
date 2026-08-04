// DeckPipe dev launcher: запускает FastAPI (uvicorn) из venv, пробрасывая --host/--port.
const { spawn } = require("child_process");
const path = require("path");

function argValue(flag, def) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

const host = argValue("--host", "127.0.0.1");
const port = argValue("--port", process.env.PORT || "7100");
const py = path.join(__dirname, ".venv", "Scripts", "python.exe");

const child = spawn(py, ["-m", "uvicorn", "app.main:app", "--host", host, "--port", port], {
  cwd: __dirname,
  stdio: "inherit",
});
child.on("exit", (code) => process.exit(code ?? 0));
