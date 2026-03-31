#!/usr/bin/env node
/**
 * Apply 06_observability.sql to the in-cluster Postgres used by the portal.
 *
 * Prerequisites: kubectl configured for the target cluster (e.g. gcloud container clusters get-credentials).
 *
 *   npm run db:observability:k8s
 *
 * Override namespace:
 *   set K8S_NAMESPACE=my-ns && npm run db:observability:k8s
 */

const { spawnSync } = require("child_process");
const path = require("path");

function sleepMs(ms) {
	Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

const repoRoot = path.resolve(__dirname, "..");
const ns = process.env.K8S_NAMESPACE || "devops-control-center-prod";
const sqlRel = "deployment/charts/infrastructure/database/06_observability.sql";
const sqlPath = path.join(repoRoot, sqlRel);
// kubectl cp on Windows rejects some absolute paths; use /-separated path relative to cwd
const sqlForKubectl = sqlRel.split(path.sep).join("/");

function runCapture(cmd, args) {
	const r = spawnSync(cmd, args, {
		cwd: repoRoot,
		encoding: "utf8",
		stdio: ["ignore", "pipe", "pipe"],
		shell: false,
	});
	const err = (r.stderr || "").trim();
	const out = (r.stdout || "").trim();
	if (r.status !== 0) {
		throw new Error(
			`${cmd} ${args.join(" ")} failed (${r.status}): ${err || out || "no output"}`,
		);
	}
	return out;
}

function main() {
	const fs = require("fs");
	if (!fs.existsSync(sqlPath)) {
		console.error("Missing file:", sqlPath);
		process.exit(1);
	}

	let pod;
	try {
		pod = runCapture("kubectl", [
			"get",
			"pods",
			"-n",
			ns,
			"-l",
			"app=postgres",
			"-o",
			"jsonpath={.items[0].metadata.name}",
		]);
	} catch (e) {
		console.error(e.message || e);
		console.error(
			"\nFix: run gcloud/google auth and ensure namespace + postgres StatefulSet exist.",
		);
		process.exit(1);
	}

	if (!pod) {
		console.error(`No pod with label app=postgres in namespace ${ns}.`);
		process.exit(1);
	}

	const maxWaitSec = Number(process.env.K8S_WAIT_SEC || "120");
	const stepMs = 3000;
	const deadline = Date.now() + maxWaitSec * 1000;
	console.log(`Waiting for pod ${pod} to be assignable (up to ${maxWaitSec}s)...`);
	while (Date.now() < deadline) {
		const phase = runCapture("kubectl", [
			"get",
			"pod",
			"-n",
			ns,
			pod,
			"-o",
			"jsonpath={.status.phase}",
		]);
		const ready = runCapture("kubectl", [
			"get",
			"pod",
			"-n",
			ns,
			pod,
			"-o",
			'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
		]);
		if (phase === "Running" && ready === "True") {
			break;
		}
		console.log(`  phase=${phase || "?"} ready=${ready || "?"} …`);
		sleepMs(stepMs);
	}

	const phaseFinal = runCapture("kubectl", [
		"get",
		"pod",
		"-n",
		ns,
		pod,
		"-o",
		"jsonpath={.status.phase}",
	]);
	const readyFinal = runCapture("kubectl", [
		"get",
		"pod",
		"-n",
		ns,
		pod,
		"-o",
		'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
	]);
	if (phaseFinal !== "Running" || readyFinal !== "True") {
		console.error(
			`Pod ${pod} is not Ready (phase=${phaseFinal}, Ready=${readyFinal}). Fix the cluster, then re-run.`,
		);
		process.exit(1);
	}

	console.log(`Namespace: ${ns}`);
	console.log(`Postgres pod: ${pod}`);
	console.log(`Copying SQL → pod:/tmp/06_observability.sql`);

	const cp = spawnSync(
		"kubectl",
		["cp", sqlForKubectl, `${ns}/${pod}:/tmp/06_observability.sql`],
		{ cwd: repoRoot, stdio: "inherit", shell: false },
	);
	if (cp.status !== 0) {
		process.exit(cp.status ?? 1);
	}

	console.log("Running psql inside pod (uses POSTGRES_USER / POSTGRES_DB from the container)...");

	const execArgs = [
		"exec",
		"-n",
		ns,
		pod,
		"--",
		"sh",
		"-c",
		'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/06_observability.sql',
	];
	const psql = spawnSync("kubectl", execArgs, {
		cwd: repoRoot,
		stdio: "inherit",
		shell: false,
	});

	if (psql.status !== 0) {
		console.error(
			"\nIf CREATE or GRANT failed, exec as superuser, e.g. add -U postgres if that role exists in your image.",
		);
		process.exit(psql.status ?? 1);
	}

	console.log("Done. Refresh /ui/observability (or restart backend pods if needed).");
}

main();
