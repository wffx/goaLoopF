import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

export const name = "goaloop-krepo-query";
export const inject = ["tools"];

const TOOL_NAME = "query_krepo_symbol";
const KINDS = ["struct", "union", "enum", "enumerator", "typedef", "variable", "macro", "macro_define"];
const MAX_STDOUT_BYTES = 64 * 1024;
const COMMAND_TIMEOUT_MS = 125000;

function validateArgs(args) {
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("query_krepo_symbol arguments must be an object");
  }
  const keys = Object.keys(args);
  const allowedKeys = new Set(["symbol", "repo", "function", "file", "kind"]);
  if (keys.some((key) => !allowedKeys.has(key))) {
    throw new Error("query_krepo_symbol accepts only symbol, repo, function, file, and kind");
  }
  if (typeof args.symbol !== "string" || !/^[A-Za-z_]\w{0,127}$/.test(args.symbol)) {
    throw new Error("symbol must be a C/C++ identifier of at most 128 characters");
  }
  if (typeof args.repo !== "string" || !args.repo.trim()) {
    throw new Error("repo is required");
  }
  if (typeof args.function !== "string" || !/^[A-Za-z_]\w{0,127}$/.test(args.function)) {
    throw new Error("function must be a C/C++ identifier of at most 128 characters");
  }
  if (
    typeof args.file !== "string"
    || !args.file.trim()
    || args.file.length > 512
    || args.file.includes("\0")
  ) {
    throw new Error("file must be a non-empty repository-relative path of at most 512 characters");
  }
  if (args.kind !== undefined && (typeof args.kind !== "string" || !KINDS.includes(args.kind))) {
    throw new Error(`kind must be one of: ${KINDS.join(", ")}`);
  }
  return {
    symbol: args.symbol,
    repo: args.repo,
    function: args.function,
    file: args.file,
    kind: args.kind,
  };
}

async function loadBinding(sessionId) {
  const bindingRoot = process.env.GOALOOP_KREPO_BINDINGS_DIR;
  if (!bindingRoot) throw new Error("GOALOOP_KREPO_BINDINGS_DIR is not configured");
  const filename = `${createHash("sha256").update(sessionId).digest("hex")}.json`;
  const bindingPath = join(resolve(bindingRoot), filename);
  let binding;
  try {
    binding = JSON.parse(await readFile(bindingPath, "utf8"));
  } catch (error) {
    throw new Error(`kRepo target binding is unavailable for session ${sessionId}: ${String(error)}`);
  }
  if (
    binding?.schema_version !== "1.0"
    || binding.session_id !== sessionId
    || typeof binding.python_executable !== "string"
    || typeof binding.repo_root !== "string"
    || typeof binding.target_function !== "string"
    || typeof binding.target_file !== "string"
  ) {
    throw new Error(`kRepo target binding is invalid for session ${sessionId}`);
  }
  return { binding, bindingPath };
}

function childEnvironment() {
  const environment = {};
  for (const key of ["HOME", "LANG", "LC_ALL", "PATH", "PYTHONPATH"]) {
    if (typeof process.env[key] === "string") environment[key] = process.env[key];
  }
  environment.PYTHONDONTWRITEBYTECODE = "1";
  return environment;
}

function runBridge(file, args, options) {
  return new Promise((resolveCommand, rejectCommand) => {
    execFile(file, args, {
      cwd: options.cwd,
      env: childEnvironment(),
      encoding: "utf8",
      maxBuffer: MAX_STDOUT_BYTES,
      timeout: COMMAND_TIMEOUT_MS,
      signal: options.signal,
      windowsHide: true,
    }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        rejectCommand(error);
        return;
      }
      resolveCommand({ stdout, stderr });
    });
  });
}

async function executeQuery(commandRunner, args, exec) {
  const query = validateArgs(args);
  const sessionId = exec.agent?.session.header.id;
  if (typeof sessionId !== "string" || !sessionId) {
    throw new Error("query_krepo_symbol requires an owning DSH session");
  }
  const { binding, bindingPath } = await loadBinding(sessionId);
  if (resolve(query.repo) !== resolve(binding.repo_root)) {
    throw new Error("repo does not match the controller-bound repository");
  }
  if (query.function !== binding.target_function) {
    throw new Error("function does not match the controller-bound target function");
  }
  if (query.file !== binding.target_file) {
    throw new Error("file does not match the controller-bound target file");
  }
  const argv = [
    "-m",
    "goaloop.krepo_tool_bridge",
    "--binding",
    bindingPath,
    "--symbol",
    query.symbol,
    "--repo",
    query.repo,
    "--function",
    query.function,
    "--file",
    query.file,
  ];
  if (query.kind !== undefined) argv.push("--kind", query.kind);
  let commandResult;
  try {
    commandResult = await commandRunner(binding.python_executable, argv, {
      cwd: binding.repo_root,
      signal: exec.signal,
    });
  } catch (error) {
    if (exec.signal.aborted) throw new Error("kRepo query was cancelled");
    const detail = typeof error?.stderr === "string" && error.stderr.trim()
      ? error.stderr.trim()
      : String(error);
    throw new Error(`kRepo bridge failed: ${detail}`);
  }
  let result;
  try {
    result = JSON.parse(commandResult.stdout);
  } catch (error) {
    throw new Error(`kRepo bridge returned invalid JSON: ${String(error)}`);
  }
  if (result?.ok !== true || typeof result.output !== "string") {
    const command = typeof result?.command === "string" ? ` command=${result.command};` : "";
    throw new Error(`kRepo query failed:${command} ${String(result?.error ?? "unknown error")}`);
  }
  return {
    symbol: query.symbol,
    repo: query.repo,
    function: query.function,
    file: query.file,
    ...(query.kind === undefined ? {} : { kind: query.kind }),
    output: result.output,
    cache_hit: result.cache_hit === true,
    command: typeof result.command === "string" ? result.command : null,
  };
}

export function createKrepoQueryTool(commandRunner = runBridge) {
  return {
    name: TOOL_NAME,
    description: "Read one non-function C/C++ symbol from the controller-bound repository with kRepo. symbol, repo, function, and file are required; kind is optional. Copy repo, function, and file exactly from the goaloop run context.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        symbol: {
          type: "string",
          pattern: "^[A-Za-z_]\\w{0,127}$",
          description: "Exact non-function C/C++ symbol name.",
        },
        repo: {
          type: "string",
          minLength: 1,
          description: "Exact repository path supplied in the goaloop run context.",
        },
        function: {
          type: "string",
          pattern: "^[A-Za-z_]\\w{0,127}$",
          description: "Exact target function supplied in the goaloop run context.",
        },
        file: {
          type: "string",
          minLength: 1,
          maxLength: 512,
          description: "Exact repository-relative target file supplied in the goaloop run context.",
        },
        kind: {
          type: "string",
          enum: KINDS,
          description: "Optional symbol kind when known.",
        },
      },
      required: ["symbol", "repo", "function", "file"],
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          symbol: { type: "string" },
          repo: { type: "string" },
          function: { type: "string" },
          file: { type: "string" },
          kind: { type: "string", enum: KINDS },
          output: { type: "string" },
          cache_hit: { type: "boolean" },
          command: { oneOf: [{ type: "string" }, { type: "null" }] },
        },
        required: ["symbol", "repo", "function", "file", "output", "cache_hit", "command"],
      },
      render(_args, value) {
        const command = value.command === null ? "kRepo command: cache hit" : `kRepo command: ${value.command}`;
        return [{ type: "text", text: `${command}\n${value.output}` }];
      },
    },
    timeoutMs: 130000,
    execute(args, exec) {
      return executeQuery(commandRunner, args, exec);
    },
    presentCall(args) {
      const symbol = typeof args?.symbol === "string" ? args.symbol : "symbol";
      return { card: "generic", title: `Query kRepo: ${symbol}`, kind: "read", rawInput: symbol };
    },
  };
}

export function apply(ctx) {
  ctx.tools.register(createKrepoQueryTool());
}
