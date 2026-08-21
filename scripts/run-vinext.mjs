import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";

const command = process.argv[2];
if (!new Set(["dev", "build", "start"]).has(command)) {
  console.error("Usage: node scripts/run-vinext.mjs <dev|build|start> [args]");
  process.exit(2);
}

const cli = path.resolve("node_modules", "vinext", "dist", "cli.js");
const child = spawn(process.execPath, [cli, command, ...process.argv.slice(3)], {
  cwd: process.cwd(),
  env: {
    ...process.env,
    WRANGLER_LOG_PATH:
      process.env.WRANGLER_LOG_PATH ?? path.join(".wrangler", "wrangler.log"),
  },
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error(`Unable to start vinext: ${error.message}`);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 1);
});
