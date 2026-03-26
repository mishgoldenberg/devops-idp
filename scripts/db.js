#!/usr/bin/env node

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");

function loadEnvFile() {
	const envPath = path.join(repoRoot, ".env");
	const result = {};

	if (!fs.existsSync(envPath)) {
		return result;
	}

	const lines = fs.readFileSync(envPath, "utf8").split(/\r?\n/);
	for (const line of lines) {
		const trimmed = line.trim();
		if (!trimmed || trimmed.startsWith("#")) {
			continue;
		}
		const eq = trimmed.indexOf("=");
		if (eq === -1) {
			continue;
		}
		const key = trimmed.slice(0, eq).trim();
		const value = trimmed.slice(eq + 1).trim();
		result[key] = value;
	}

	return result;
}

const envFromFile = loadEnvFile();
const dbName =
	process.env.POSTGRES_DB || envFromFile.POSTGRES_DB || "devops_control_center";
const dbUserCandidates = Array.from(
	new Set(
		[
			process.env.POSTGRES_USER,
			envFromFile.POSTGRES_USER,
			"devops_user",
			"devops",
		].filter(Boolean),
	),
);
let detectedDbUser = null;

const schemaFile = "deployment/charts/infrastructure/database/00_schema.sql";
const seedFiles = [
	"deployment/charts/infrastructure/database/01_roles.sql",
	"deployment/charts/infrastructure/database/02_users.sql",
	"deployment/charts/infrastructure/database/03_widget_types.sql",
	"deployment/charts/infrastructure/database/04_approval_rules.sql",
	"deployment/charts/infrastructure/database/05_service_health.sql",
];

function sleep(ms) {
	Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function run(command, args, options = {}) {
	const stdio = Object.prototype.hasOwnProperty.call(options, "input")
		? ["pipe", "inherit", "inherit"]
		: "inherit";

	const result = spawnSync(command, args, {
		cwd: repoRoot,
		stdio,
		shell: false,
		...options,
	});

	if (result.status !== 0) {
		const cmd = `${command} ${args.join(" ")}`;
		throw new Error(`Command failed: ${cmd}`);
	}
}

function runQuiet(command, args, options = {}) {
	return spawnSync(command, args, {
		cwd: repoRoot,
		stdio: "ignore",
		shell: false,
		...options,
	});
}

function runCompose(args) {
	run("docker", ["compose", ...args]);
}

function resolveDbUser() {
	if (detectedDbUser) {
		return detectedDbUser;
	}

	for (const candidate of dbUserCandidates) {
		const probe = runQuiet("docker", [
			"compose",
			"exec",
			"-T",
			"postgres",
			"psql",
			"-U",
			candidate,
			"-d",
			dbName,
			"-c",
			"SELECT 1;",
		]);

		if (probe.status === 0) {
			detectedDbUser = candidate;
			return detectedDbUser;
		}
	}

	throw new Error(
		`Could not connect with any known DB user (${dbUserCandidates.join(", ")}). Set POSTGRES_USER in .env or shell.`,
	);
}

function runSqlFile(sqlFilePath) {
	const absolutePath = path.join(repoRoot, sqlFilePath);
	if (!fs.existsSync(absolutePath)) {
		throw new Error(`SQL file not found: ${sqlFilePath}`);
	}

	const sqlContent = fs.readFileSync(absolutePath, "utf8");
	const dbUser = resolveDbUser();
	run(
		"docker",
		[
			"compose",
			"exec",
			"-T",
			"postgres",
			"psql",
			"-v",
			"ON_ERROR_STOP=1",
			"-U",
			dbUser,
			"-d",
			dbName,
		],
		{
			input: sqlContent,
		},
	);
}

function migrateWithRetry() {
	const attempts = 20;

	for (let i = 1; i <= attempts; i += 1) {
		try {
			console.log(
				`Checking database connectivity (attempt ${i}/${attempts})...`,
			);
			resolveDbUser();
			console.log("Database is ready. Running migration...");
			runSqlFile(schemaFile);
			console.log("Migration completed.");
			return;
		} catch (error) {
			if ((error?.message || "").includes("SQL file not found")) {
				throw error;
			}

			if (
				(error?.message || "").includes(
					"Command failed: docker compose exec -T postgres psql -v ON_ERROR_STOP=1",
				)
			) {
				throw new Error(
					"Migration failed due to SQL errors in schema file. If your DB already has tables/types, use a clean DB (npm run db:reset) or make schema idempotent.",
				);
			}

			if (i === attempts) {
				throw error;
			}
			console.log("Database is not ready yet, retrying in 2s...");
			sleep(2000);
		}
	}
}

function seed() {
	for (const file of seedFiles) {
		console.log(`Applying seed: ${file}`);
		runSqlFile(file);
	}
	console.log("Seeding completed.");
}

function reset() {
	runCompose(["down", "-v"]);
	runCompose(["up", "-d", "postgres", "redis"]);
	migrateWithRetry();
	seed();
}

function main() {
	const command = process.argv[2];

	try {
		if (command === "migrate") {
			migrateWithRetry();
			return;
		}

		if (command === "seed") {
			seed();
			return;
		}

		if (command === "reset") {
			reset();
			return;
		}

		console.error("Usage: node scripts/db.js <migrate|seed|reset>");
		process.exit(1);
	} catch (error) {
		console.error(error.message || error);
		process.exit(1);
	}
}

main();
