"use strict";

const { spawnSync } = require("node:child_process");

// 先跑领域/契约测试，再单独确认 /health 服务契约模块。
const runs = [
  ["python3", ["-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]],
  ["python3", ["-m", "unittest", "service_contract"]],
];

for (const [command, args] of runs) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
