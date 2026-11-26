// jobapply-guard — Pi adapter for the job-application routine's guard.
//
// "One brain, many mouths": the enforcement logic stays in the original, untouched
// script in claude_applicator/ — guard_hook.py (deny). This file is only a mouth: it
// translates Pi tool names into the Claude vocabulary the guard speaks, feeds it the
// same stdin JSON it gets from Claude Code, and maps its exit code onto Pi's
// tool_call verdict:
//
//   guard_hook.py  exit 2  →  { block: true, reason: <stderr> }   (deny reaches the model)
//
// Pacing is not ported: this product's routine paces on the model side (the sleeps in
// CLAUDE.md), so there is no pace_hook.py here and no second handler.
//
// Arming: JOBAPPLY_GUARD=1 — the SAME env var the wrappers (run_scheduled.sh,
// run_mail_agent.sh) export. Without it the handler below is a complete no-op, so
// interactive human sessions in this repo are never guarded.
//
// MCP servers: at load (session start) this extension registers the two servers the
// routine needs — playwright (attaches to the user's Chrome over CDP, writes into the
// user's output dir) and sqlite (the repo's applications.db) — from the JOBAPPLY_*
// environment, so nothing is hard-coded per user. Registered while the extension loads,
// they connect when the session starts, together with the mcp.json servers.
//
// Install/load: this file lives at <repo>/.pi/extensions/, so Pi auto-loads it for any
// session started in the repo. Explicit: pi -ne -e .pi/extensions/jobapply-guard.ts
// (the worker_lib.sh pi branch does exactly that, keeping discovery off).
//
// Design rules (portable hooks, HOOKS-001): defensive stdin JSON, tool-name aliases in
// exactly one place, verdicts via exit code AND structured result, stdlib-only spawns.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";

// Resolve the brains (and the repo's engine directory) relative to THIS file, so the
// adapter is location-independent and the brains stay script-relative (they resolve
// their own config/db paths).
const GUARD = new URL("../../claude_applicator/guard_hook.py", import.meta.url).pathname;
const ENGINE_DIR = new URL("../../claude_applicator", import.meta.url).pathname;

// Per-user paths from the environment — the wrappers export these before launching the
// worker (MULTI-001/MULTI-002b); defaults keep a repo-relative session working.
const DATA_ROOT = process.env.JOBAPPLY_DATA ?? ENGINE_DIR;
const CDP_PORT = process.env.JOBAPPLY_CDP_PORT ?? "9222";
const OUTPUT_DIR = `${DATA_ROOT}/output`;
const DB_PATH = `${ENGINE_DIR}/applications.db`;

// Pi tool name → Claude Code tool name the brains expect.
// mcp__sqlite__* and mcp__playwright__browser_* pass through unchanged — Pi uses the
// same mcp__<server>__<tool> naming Claude Code does.
const TOOL_ALIASES: Record<string, string> = {
	bash: "Bash",
	edit: "Edit",
	write: "Write",
};

// Pi's edit/write input uses `path`; the brains read `file_path`/`notebook_path`.
// Augment a copy — never mutate the caller's input object.
function toHookInput(piToolName: string, input: unknown): Record<string, unknown> {
	const tin: Record<string, unknown> = { ...(input as Record<string, unknown> ?? {}) };
	if (tin.path !== undefined && tin.file_path === undefined) {
		tin.file_path = tin.path;
	}
	return tin;
}

function runHook(script: string, payload: unknown, timeoutMs: number) {
	return spawnSync("python3", [script], {
		input: JSON.stringify(payload),
		encoding: "utf8",
		timeout: timeoutMs,
		// JOBAPPLY_GUARD + optional JOBAPPLY_* path overrides pass straight through.
		env: process.env,
	});
}

export default function (pi: ExtensionAPI) {
	// ---- MCP servers the routine needs, from the environment (session start) ----
	pi.registerMcpServer("playwright", {
		command: "npx",
		args: [
			"-y",
			"@playwright/mcp@0.0.80",
			"--cdp-endpoint",
			`http://127.0.0.1:${CDP_PORT}`,
			"--output-dir",
			OUTPUT_DIR,
		],
	});
	pi.registerMcpServer("sqlite", {
		command: "uvx",
		args: ["--with", "mcp==1.6.0", "mcp-server-sqlite", "--db-path", DB_PATH],
	});

	// ---- PreToolUse: guard verdict, then pacing sleep (in that order, like Claude) ----
	pi.on("tool_call", async (event) => {
		if (process.env.JOBAPPLY_GUARD !== "1") return; // unarmed: full pass-through

		const tool = TOOL_ALIASES[event.toolName] ?? event.toolName;
		const payload = {
			hook_event_name: "PreToolUse",
			tool_name: tool,
			tool_input: toHookInput(event.toolName, event.input),
		};

		// 1) The gate. exit 2 = deny; stderr is the reason the model sees and logs.
		const guard = runHook(GUARD, payload, 30_000);
		if (guard.status === 2) {
			return { block: true, reason: (guard.stderr || "").trim() || "blocked by jobapply guard" };
		}
		if (guard.status !== 0) {
			// Guard broke on itself: its documented posture is fail-open for internal
			// errors (accident-guard, not a sandbox). Surface loudly, never block.
			console.error(`[jobapply-guard] guard exited ${guard.status}: ${(guard.stderr || "").trim()}`);
		}
		return; // proceed
	});

}
