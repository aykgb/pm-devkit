import crypto from "crypto";
import fs from "fs";
import path from "path";

const SERVICE = "pm-guardian";

const TRACE = "trace";
const DEBUG = "debug";
const INFO = "info";
const WARN = "warn";
const ERROR = "error";

const SESSION_MARKER = "opencode-current-session";
const INSTRUCT_MARKER = "opencode-pm-instruct";

const DEFAULT_CONFIG_FILE = "pm-guardian.conf.json";

const DEFAULT_CONFIG = Object.freeze({
    schemaVersion: 1,

    // Prompt-affecting config. These are frozen after plugin initialization.
    targetAgent: "pm",
    sessionInfoFile: "pm-session-info.json",
    sessionGuardForceTakeover: false,
    sessionOwnerPid: process.pid,
    logFile: "pm-guardian.jsonl",
    instructFiles: [
        "persona.md",
        "instruct.md",
    ],

    // Runtime log/debug config. These may be watched and refreshed.
    logLevel: "info",
    logToOpenCodeApp: false,
    debugDump: false,

    // Runtime log/debug watcher interval. Keep fixed at 10s by default.
    runtimeLogConfigPollIntervalMs: 10000,
});

export default {
    id: SERVICE,
    setup,
};

/**
 * PMGuardianPlugin (OpenCode V2 plugin API)
 *
 * Behavior:
 * 1. session context hook uses event.sessionID / event.agent as source of truth.
 * 2. Only targetAgent receives PM session info + instruct markdown.
 * 3. system prompt-affecting config is frozen at process startup.
 * 4. instruct markdown files are loaded once at process startup.
 * 5. runtime log/debug config is refreshed every 10 seconds.
 * 6. persona.md is no longer a separate injection path; it is just the first
 *    default instruct markdown file.
 */
async function setup(ctx) {
    const directory = ctx.location.directory;

    const pmDir = path.join(directory, ".pm");

    const configFileName = String(
        process.env.OPENCODE_PM_SESSION_CONFIG_FILE || DEFAULT_CONFIG_FILE
    );

    const configPath = resolvePathUnderPm(directory, configFileName);

    let frozenPromptConfig = pickPromptConfig(buildInitialConfig());
    let runtimeLogConfig = pickRuntimeLogConfig(buildInitialConfig());
    let runtimeLogConfigSnapshot = stableJsonStringify(runtimeLogConfig);
    let frozenInstructSections = Object.freeze([]);

    function getSessionInfoPath() {
        return resolvePathUnderPm(directory, frozenPromptConfig.sessionInfoFile);
    }

    function getSessionLogPath() {
        return resolvePathUnderPm(directory, frozenPromptConfig.logFile);
    }

    const logMessage = async (level, message, extra = {}) => {
        if (!shouldLog(level, runtimeLogConfig.logLevel)) {
            return;
        }

        const sessionInfoPath = getSessionInfoPath();
        const sessionLogPath = getSessionLogPath();

        const entry = buildLogEntry({
            level,
            message,
            extra,
            directory,
            sessionInfoPath,
            sessionLogPath,
            promptConfig: frozenPromptConfig,
            runtimeLogConfig,
        });

        await appendJsonLine(sessionLogPath, entry);

        if (!runtimeLogConfig.logToOpenCodeApp) {
            return;
        }

        // V2 plugin context has no client.app.log endpoint; use console instead.
        console.log(`[${SERVICE}] ${level} ${message}`, extra);
    };

    async function loadConfigOnce() {
        let fileConfig = {};

        try {
            const raw = await fs.promises.readFile(configPath, "utf8");
            fileConfig = JSON.parse(raw);
        } catch (err) {
            if (err?.code !== "ENOENT") {
                const fallbackConfig = buildInitialConfig();

                await appendJsonLine(
                    resolvePathUnderPm(directory, fallbackConfig.logFile),
                    buildLogEntry({
                        level: WARN,
                        message: "Failed to read PM guardian config file during initialization; using default/env config",
                        extra: {
                            configPath,
                            error: String(err),
                            code: err?.code,
                        },
                        directory,
                        sessionInfoPath: resolvePathUnderPm(directory, fallbackConfig.sessionInfoFile),
                        sessionLogPath: resolvePathUnderPm(directory, fallbackConfig.logFile),
                        promptConfig: pickPromptConfig(fallbackConfig),
                        runtimeLogConfig: pickRuntimeLogConfig(fallbackConfig),
                    })
                );
            }
        }

        const baseConfig = buildInitialConfig();

        return normalizeGuardianConfig({
            ...baseConfig,
            ...fileConfig,

            // Support:
            // 1. { "instructFiles": [...] }
            // 2. { "instruct": { "files": [...] } }
            // 3. { "instructions": [...] }
            instructFiles: normalizeInstructFiles(
                fileConfig?.instruct?.files
                ?? fileConfig?.instructFiles
                ?? fileConfig?.instructions
                ?? baseConfig.instructFiles
            ),
        });
    }

    async function loadInstructFileOnce(configuredPath) {
        const resolvedPath = resolvePathUnderPm(directory, configuredPath);

        try {
            const content = await fs.promises.readFile(resolvedPath, "utf8");

            if (!content.trim()) {
                return null;
            }

            const sourceID = buildInstructSourceID(directory, configuredPath, resolvedPath);

            return Object.freeze({
                sourceID,
                configuredPath,
                resolvedPath,
                content,
            });
        } catch (err) {
            if (err?.code !== "ENOENT") {
                await logMessage(WARN, "PM instruct markdown file not readable during initialization", {
                    configuredPath,
                    resolvedPath,
                    error: String(err),
                    code: err?.code,
                });
            }

            return null;
        }
    }

    async function loadInstructSectionsOnce() {
        const sections = [];

        for (const configuredPath of frozenPromptConfig.instructFiles) {
            const section = await loadInstructFileOnce(configuredPath);

            if (section) {
                sections.push(section);
            }
        }

        return Object.freeze(sections);
    }

    async function refreshRuntimeLogConfig() {
        let fileConfig = {};

        try {
            const raw = await fs.promises.readFile(configPath, "utf8");
            fileConfig = JSON.parse(raw);
        } catch (err) {
            if (err?.code !== "ENOENT") {
                await logMessage(WARN, "Failed to read PM guardian runtime log config; keeping previous runtime log config", {
                    configPath,
                    error: String(err),
                    code: err?.code,
                });
            }

            return;
        }

        const baseConfig = buildInitialConfig();

        // Important:
        // Prompt-affecting fields are intentionally ignored at runtime.
        // Only log/debug fields are refreshed.
        const nextFullConfig = normalizeGuardianConfig({
            ...baseConfig,
            ...fileConfig,

            targetAgent: frozenPromptConfig.targetAgent,
            sessionInfoFile: frozenPromptConfig.sessionInfoFile,
            sessionGuardForceTakeover: frozenPromptConfig.sessionGuardForceTakeover,
            sessionOwnerPid: frozenPromptConfig.sessionOwnerPid,
            logFile: frozenPromptConfig.logFile,
            instructFiles: frozenPromptConfig.instructFiles,
        });

        const nextRuntimeLogConfig = pickRuntimeLogConfig(nextFullConfig);
        const nextSnapshot = stableJsonStringify(nextRuntimeLogConfig);

        if (nextSnapshot === runtimeLogConfigSnapshot) {
            return;
        }

        const previous = runtimeLogConfig;

        runtimeLogConfig = nextRuntimeLogConfig;
        runtimeLogConfigSnapshot = nextSnapshot;

        await logMessage(INFO, "PM guardian runtime log config changed", {
            configPath,
            from: previous,
            to: runtimeLogConfig,
        });
    }

    function startRuntimeLogConfigWatcher() {
        const intervalMs = Math.max(
            1000,
            parsePositiveInteger(runtimeLogConfig.runtimeLogConfigPollIntervalMs)
            || DEFAULT_CONFIG.runtimeLogConfigPollIntervalMs
        );

        const timer = setInterval(() => {
            refreshRuntimeLogConfig().catch((err) => {
                appendJsonLine(
                    getSessionLogPath(),
                    buildLogEntry({
                        level: ERROR,
                        message: "runtime log config watcher failed",
                        extra: {
                            configPath,
                            error: String(err),
                            stack: err?.stack,
                        },
                        directory,
                        sessionInfoPath: getSessionInfoPath(),
                        sessionLogPath: getSessionLogPath(),
                        promptConfig: frozenPromptConfig,
                        runtimeLogConfig,
                    })
                ).catch(() => { });
            });
        }, intervalMs);

        timer.unref();
        return timer;
    }

    async function guardAndPersistCurrentSession({ sessionID, agent, input }) {
        const nowIso = new Date().toISOString();
        const sessionInfoPath = getSessionInfoPath();

        const nextRecord = buildSessionRecord({
            sessionID,
            agent,
            directory,
            input,
            nowIso,
            promptConfig: frozenPromptConfig,
            sessionInfoPath,
        });

        await fs.promises.mkdir(pmDir, { recursive: true });

        const current = await readSessionInfo(sessionInfoPath);

        let takeoverReason = undefined;

        if (current?.current_session_id && current.current_session_id !== sessionID) {
            const liveness = checkPersistedSessionProcess(current);

            const conflict = {
                allowed: false,
                action: "conflict",
                record: nextRecord,
                currentSessionID: current.current_session_id,
                requestedSessionID: sessionID,
                current,
                next: nextRecord,
                liveness,
            };

            if (!frozenPromptConfig.sessionGuardForceTakeover && liveness.alive !== false) {
                await logMessage(WARN, "Blocked conflicting PM session", {
                    sessionInfoPath,
                    ...conflict,
                });

                // 持久化 conflict：写回 pm-session-info.json，让 status.py / overview 可检测
                const conflictRecord = {
                    ...current,
                    blocked_session_id: sessionID,
                    blocked_at: nowIso,
                    updated_at: nowIso,
                };
                await writeJsonAtomic(sessionInfoPath, stripUndefined(conflictRecord));

                return conflict;
            }

            takeoverReason = frozenPromptConfig.sessionGuardForceTakeover
                ? "force"
                : "stale_owner_process_exited";

            await logMessage(
                WARN,
                frozenPromptConfig.sessionGuardForceTakeover
                    ? "Force takeover of current PM session"
                    : "Reclaimed stale PM session because owner process exited",
                {
                    sessionInfoPath,
                    previousSessionID: current.current_session_id,
                    nextSessionID: sessionID,
                    liveness,
                }
            );
        }

        const record = {
            ...nextRecord,
            created_at: current?.current_session_id === sessionID
                ? current.created_at || nowIso
                : nowIso,
            updated_at: nowIso,
            takeover_from_session_id:
                current?.current_session_id && current.current_session_id !== sessionID
                    ? current.current_session_id
                    : undefined,
            takeover_reason: takeoverReason,
        };

        await writeJsonAtomic(sessionInfoPath, stripUndefined(record));

        return {
            allowed: true,
            action: current?.current_session_id === sessionID ? "refreshed" : "created",
            record,
        };
    }

    await fs.promises.mkdir(pmDir, { recursive: true });

    const loadedConfig = await loadConfigOnce();

    frozenPromptConfig = pickPromptConfig(loadedConfig);
    runtimeLogConfig = pickRuntimeLogConfig(loadedConfig);
    runtimeLogConfigSnapshot = stableJsonStringify(runtimeLogConfig);

    frozenInstructSections = await loadInstructSectionsOnce();

    await logMessage(INFO, "PM guardian initialized with frozen prompt config and runtime log watch", {
        configPath,
        promptConfig: redactPromptConfigForLog(frozenPromptConfig),
        runtimeLogConfig,
        instructFiles: frozenInstructSections.map((section) => ({
            sourceID: section.sourceID,
            configuredPath: section.configuredPath,
            resolvedPath: section.resolvedPath,
            size: section.content.length,
        })),
    });

    const runtimeLogConfigTimer = startRuntimeLogConfigWatcher();

    await ctx.session.hook("context", async (event) => {
        try {
            const sessionID = String(event?.sessionID || "");
            const agent = String(event?.agent || "").toLowerCase();

            if (runtimeLogConfig.debugDump) {
                await logMessage(DEBUG, "context hook input schema", {
                    sessionID,
                    agent,
                    inputSchema: dumpStructure(event),
                    systemHead: previewSystemHead(event?.system),
                    promptConfig: redactPromptConfigForLog(frozenPromptConfig),
                    runtimeLogConfig,
                });
            }

            if (!sessionID) {
                await logMessage(WARN, "Skipped PM instruct injection: missing sessionID", {
                    inputSchema: runtimeLogConfig.debugDump
                        ? dumpStructure(event)
                        : undefined,
                });

                return;
            }

            if (agent !== frozenPromptConfig.targetAgent) {
                if (runtimeLogConfig.debugDump) {
                    await logMessage(DEBUG, "Skipped PM session/instruct injection: non-target agent", {
                        sessionID,
                        agent: agent || "<missing>",
                        targetAgent: frozenPromptConfig.targetAgent,
                    });
                }

                return;
            }

            normalizeSystemSegments(event);

            const guard = await guardAndPersistCurrentSession({
                sessionID,
                agent,
                input: event,
            });

            const beforeSystemHead = previewSystemHead(event.system);

            const sessionInfoInjected = !hasSessionInfo(event.system);

            if (sessionInfoInjected) {
                event.system.push(textSegment(wrapSessionInfo(guard.record, frozenPromptConfig)));
            }

            const injectedInstructs = [];
            const skippedInstructs = [];
            let afterSystemHead = beforeSystemHead;

            if (guard.allowed) {
                for (const section of frozenInstructSections) {
                    if (hasInstructSource(event.system, section.sourceID)) {
                        skippedInstructs.push(section.configuredPath);
                        continue;
                    }

                    event.system.push(textSegment(wrapInstruct(section)));
                    injectedInstructs.push(section.configuredPath);
                }

                afterSystemHead = previewSystemHead(event.system);
            }

            await logMessage(DEBUG, "Injected PM session/instruct info", {
                sessionID,
                agent,
                targetAgent: frozenPromptConfig.targetAgent,
                systemItems: event.system.length,
                sessionInfoPath: getSessionInfoPath(),
                sessionGuardAction: guard.action,
                sessionInfoInjected,
                instructFiles: frozenPromptConfig.instructFiles,
                injectedInstructs,
                skippedInstructs,
                beforeSystemHead,
                afterSystemHead,
            });
        } catch (err) {
            await logMessage(ERROR, "context hook failed", {
                error: String(err),
                stack: err?.stack,
            });
        }
    });

    return () => clearInterval(runtimeLogConfigTimer);
}

function buildInitialConfig() {
    return normalizeGuardianConfig({
        ...DEFAULT_CONFIG,

        targetAgent: process.env.OPENCODE_PM_PERSONA_AGENT
            || process.env.OPENCODE_PM_TARGET_AGENT
            || DEFAULT_CONFIG.targetAgent,

        sessionInfoFile: process.env.OPENCODE_PM_SESSION_INFO_FILE
            || DEFAULT_CONFIG.sessionInfoFile,

        sessionGuardForceTakeover: envBoolean(
            process.env.OPENCODE_PM_SESSION_GUARD_FORCE,
            DEFAULT_CONFIG.sessionGuardForceTakeover
        ),

        sessionOwnerPid: parsePositiveInteger(
            process.env.OPENCODE_PM_SESSION_OWNER_PID
        ) || DEFAULT_CONFIG.sessionOwnerPid,

        logFile: process.env.OPENCODE_PM_SESSION_LOG_FILE
            || DEFAULT_CONFIG.logFile,

        logLevel: process.env.OPENCODE_PM_SESSION_LOG_LEVEL
            || DEFAULT_CONFIG.logLevel,

        logToOpenCodeApp: envBoolean(
            process.env.OPENCODE_PM_SESSION_LOG_TO_APP,
            DEFAULT_CONFIG.logToOpenCodeApp
        ),

        debugDump: envBoolean(
            process.env.OPENCODE_PM_PERSONA_DEBUG,
            DEFAULT_CONFIG.debugDump
        ),

        runtimeLogConfigPollIntervalMs: parsePositiveInteger(
            process.env.OPENCODE_PM_RUNTIME_LOG_CONFIG_POLL_INTERVAL_MS
            || process.env.OPENCODE_PM_CONFIG_POLL_INTERVAL_MS
        ) || DEFAULT_CONFIG.runtimeLogConfigPollIntervalMs,

        instructFiles: normalizeInstructFiles(
            process.env.OPENCODE_PM_INSTRUCT_FILES
                ? process.env.OPENCODE_PM_INSTRUCT_FILES.split(/[,:]/)
                : process.env.OPENCODE_PM_INSTRUCT_FILE
                    ? [process.env.OPENCODE_PM_INSTRUCT_FILE]
                    : DEFAULT_CONFIG.instructFiles
        ),
    });
}

function normalizeGuardianConfig(config) {
    return {
        schemaVersion: parsePositiveInteger(config?.schemaVersion) || 1,

        targetAgent: String(
            config?.targetAgent || DEFAULT_CONFIG.targetAgent
        ).toLowerCase(),

        sessionInfoFile: nonEmptyString(
            config?.sessionInfoFile,
            DEFAULT_CONFIG.sessionInfoFile
        ),

        sessionGuardForceTakeover: booleanValue(
            config?.sessionGuardForceTakeover,
            DEFAULT_CONFIG.sessionGuardForceTakeover
        ),

        sessionOwnerPid: parsePositiveInteger(config?.sessionOwnerPid)
            || parsePositiveInteger(config?.ownerPid)
            || DEFAULT_CONFIG.sessionOwnerPid,

        logFile: nonEmptyString(
            config?.logFile,
            DEFAULT_CONFIG.logFile
        ),

        logLevel: normalizeLogLevel(
            config?.logLevel || DEFAULT_CONFIG.logLevel
        ),

        logToOpenCodeApp: booleanValue(
            config?.logToOpenCodeApp,
            DEFAULT_CONFIG.logToOpenCodeApp
        ),

        debugDump: booleanValue(
            config?.debugDump,
            DEFAULT_CONFIG.debugDump
        ),

        runtimeLogConfigPollIntervalMs: parsePositiveInteger(
            config?.runtimeLogConfigPollIntervalMs
            || config?.configPollIntervalMs
        ) || DEFAULT_CONFIG.runtimeLogConfigPollIntervalMs,

        instructFiles: normalizeInstructFiles(
            config?.instructFiles || DEFAULT_CONFIG.instructFiles
        ),
    };
}

function pickPromptConfig(config) {
    return Object.freeze({
        schemaVersion: config.schemaVersion,
        targetAgent: config.targetAgent,
        sessionInfoFile: config.sessionInfoFile,
        sessionGuardForceTakeover: config.sessionGuardForceTakeover,
        sessionOwnerPid: config.sessionOwnerPid,
        logFile: config.logFile,
        instructFiles: Object.freeze([...config.instructFiles]),
    });
}

function pickRuntimeLogConfig(config) {
    return {
        logLevel: config.logLevel,
        logToOpenCodeApp: config.logToOpenCodeApp,
        debugDump: config.debugDump,
        runtimeLogConfigPollIntervalMs: config.runtimeLogConfigPollIntervalMs,
    };
}

function normalizeInstructFiles(value) {
    const files = Array.isArray(value)
        ? value
        : typeof value === "string"
            ? value.split(/[,:]/)
            : DEFAULT_CONFIG.instructFiles;

    const result = [];
    const seen = new Set();

    for (const item of files) {
        const file = String(item || "").trim();

        if (!file || seen.has(file)) {
            continue;
        }

        seen.add(file);
        result.push(file);
    }

    return result.length > 0 ? result : [...DEFAULT_CONFIG.instructFiles];
}

function redactPromptConfigForLog(config) {
    return {
        schemaVersion: config.schemaVersion,
        targetAgent: config.targetAgent,
        sessionInfoFile: config.sessionInfoFile,
        sessionGuardForceTakeover: config.sessionGuardForceTakeover,
        logFile: config.logFile,
        instructFiles: config.instructFiles,
    };
}

/**
 * Ensure event.system holds text segments in place.
 */
function normalizeSystemSegments(event) {
    if (Array.isArray(event.system)) {
        return event.system;
    }

    event.system = event.system === null || event.system === undefined || event.system === ""
        ? []
        : [textSegment(event.system)];

    return event.system;
}

/**
 * Build a text segment for the V2 system prompt parts array.
 */
function textSegment(text) {
    return {
        type: "text",
        text: String(text),
    };
}

/**
 * Read the text of a V2 system segment, tolerating plain strings.
 */
function segmentText(segment) {
    if (typeof segment === "string") {
        return segment;
    }

    if (segment && typeof segment === "object" && typeof segment.text === "string") {
        return segment.text;
    }

    return "";
}

/**
 * Check whether current session info has already been injected.
 */
function hasSessionInfo(system) {
    if (!Array.isArray(system)) {
        return segmentText(system).includes(SESSION_MARKER);
    }

    return system.some((item) =>
        segmentText(item).includes(SESSION_MARKER)
    );
}

/**
 * Check whether a concrete instruction source has already been injected.
 */
function hasInstructSource(system, sourceID) {
    const needle = `${INSTRUCT_MARKER} id="${sourceID}"`;

    if (!Array.isArray(system)) {
        return segmentText(system).includes(needle);
    }

    return system.some((item) =>
        segmentText(item).includes(needle)
    );
}

/**
 * Wrap project instruction markdown with a stable per-source marker.
 */
function wrapInstruct(section) {
    return [
        `<${INSTRUCT_MARKER} id="${section.sourceID}" source="${escapeXmlAttribute(section.configuredPath)}">`,
        section.content.trim(),
        `</${INSTRUCT_MARKER}>`,
    ].join("\n");
}

/**
 * Wrap current session info with a stable marker.
 *
 * Keep this block stable. Do not include updated_at, owner_process, or other
 * volatile fields in system prompt.
 */
function wrapSessionInfo(record, promptConfig) {
    return [
        `<${SESSION_MARKER}>`,
        `PM_CURRENT_SESSION_ID: ${record.current_session_id}`,
        `session_info_file: .pm/${promptConfig.sessionInfoFile}`,
        `</${SESSION_MARKER}>`,
    ].join("\n");
}

function buildSessionRecord({ sessionID, agent, directory, input, nowIso, promptConfig, sessionInfoPath }) {
    return stripUndefined({
        schema_version: 1,
        current_session_id: sessionID,
        agent,
        target_agent: promptConfig.targetAgent,
        directory,
        session_info_path: sessionInfoPath,
        source: SERVICE,
        status: "active",
        owner_process: buildOwnerProcessInfo(promptConfig),
        created_at: nowIso,
        updated_at: nowIso,
        input_session_id: input?.sessionID,
        input_message_id: input?.messageID,
        input_part_id: input?.partID,
    });
}

function buildOwnerProcessInfo(promptConfig) {
    return stripUndefined({
        pid: parsePositiveInteger(promptConfig?.sessionOwnerPid) || process.pid,
        ppid: process.ppid,
        node_pid: process.pid,
        cwd: safeProcessCwd(),
        platform: process.platform,
        started_at: new Date(Date.now() - Math.round(process.uptime() * 1000)).toISOString(),
        source: process.env.OPENCODE_PM_SESSION_OWNER_PID
            ? "env:OPENCODE_PM_SESSION_OWNER_PID"
            : "config_or_process.pid",
    });
}

function checkPersistedSessionProcess(record) {
    const pid = parsePositiveInteger(
        record?.owner_process?.pid
        || record?.owner_process_pid
        || record?.pid
        || record?.process_id
    );

    if (!pid) {
        return {
            alive: null,
            reason: "missing_owner_process_pid",
        };
    }

    if (pid === process.pid) {
        return {
            alive: true,
            pid,
            reason: "same_plugin_process",
        };
    }

    try {
        process.kill(pid, 0);
        return {
            alive: true,
            pid,
            reason: "process_exists",
        };
    } catch (err) {
        if (err?.code === "ESRCH") {
            return {
                alive: false,
                pid,
                reason: "process_not_found",
            };
        }

        if (err?.code === "EPERM") {
            return {
                alive: true,
                pid,
                reason: "process_exists_but_no_permission",
                error: String(err),
            };
        }

        return {
            alive: null,
            pid,
            reason: "process_liveness_unknown",
            error: String(err),
            code: err?.code,
        };
    }
}

function buildInstructSourceID(directory, configuredPath, resolvedPath) {
    const relPath = path.isAbsolute(configuredPath)
        ? path.resolve(configuredPath)
        : path.relative(path.join(directory, ".pm"), resolvedPath);

    const normalized = relPath.split(path.sep).join("/");

    const hash = crypto
        .createHash("sha256")
        .update(normalized)
        .digest("hex")
        .slice(0, 12);

    const slug = normalized
        .toLowerCase()
        .replace(/[^a-z0-9._/-]+/g, "-")
        .replace(/[/.]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 48)
        || "instruct";

    return `${slug}-${hash}`;
}

function parsePositiveInteger(value) {
    const n = Number.parseInt(String(value || ""), 10);
    return Number.isSafeInteger(n) && n > 0 ? n : null;
}

function nonEmptyString(value, fallback) {
    const text = String(value || "").trim();
    return text || fallback;
}

function booleanValue(value, fallback = false) {
    if (typeof value === "boolean") {
        return value;
    }

    if (typeof value === "number") {
        return value !== 0;
    }

    const text = String(value ?? "").trim().toLowerCase();

    if (["1", "true", "yes", "y", "on"].includes(text)) {
        return true;
    }

    if (["0", "false", "no", "n", "off"].includes(text)) {
        return false;
    }

    return fallback;
}

function envBoolean(value, fallback = false) {
    if (value === undefined) {
        return fallback;
    }

    return booleanValue(value, fallback);
}

function normalizeLogLevel(value) {
    const level = String(value || "info").trim().toLowerCase();

    if ([
        "trace",
        "debug",
        "info",
        "warn",
        "error",
        "silent",
        "off",
        "none",
    ].includes(level)) {
        return level;
    }

    return "info";
}

function logLevelPriority(level) {
    switch (String(level || "").toLowerCase()) {
        case TRACE:
            return 10;
        case DEBUG:
            return 20;
        case INFO:
            return 30;
        case WARN:
            return 40;
        case ERROR:
            return 50;
        case "silent":
        case "off":
        case "none":
            return 999;
        default:
            return 30;
    }
}

function shouldLog(level, configuredLevel) {
    const configured = normalizeLogLevel(configuredLevel);

    if (["silent", "off", "none"].includes(configured)) {
        return false;
    }

    return logLevelPriority(level) >= logLevelPriority(configured);
}

function resolvePathUnderPm(directory, filePath) {
    const text = String(filePath || "");

    if (path.isAbsolute(text)) {
        return path.normalize(text);
    }

    return path.join(directory, ".pm", text);
}

function buildLogEntry({ level, message, extra, directory, sessionInfoPath, sessionLogPath, promptConfig, runtimeLogConfig }) {
    return stripUndefined({
        ts: new Date().toISOString(),
        service: SERVICE,
        level: String(level || INFO).toLowerCase(),
        message: String(message || ""),
        directory,
        session_info_path: sessionInfoPath,
        session_log_path: sessionLogPath,
        process: buildOwnerProcessInfo(promptConfig),
        prompt_config: redactPromptConfigForLog(promptConfig),
        runtime_log_config: runtimeLogConfig,
        extra: makeJsonSafe(extra),
    });
}

async function appendJsonLine(filePath, entry) {
    try {
        await fs.promises.mkdir(path.dirname(filePath), { recursive: true });
        await fs.promises.appendFile(filePath, `${safeJsonStringify(entry)}\n`, "utf8");
    } catch (_) {
        // Logging must never break the plugin.
    }
}

function stableJsonStringify(value) {
    return JSON.stringify(sortObjectKeys(makeJsonSafe(value)));
}

function safeJsonStringify(value) {
    try {
        return JSON.stringify(makeJsonSafe(value));
    } catch (err) {
        return JSON.stringify({
            ts: new Date().toISOString(),
            service: SERVICE,
            level: ERROR,
            message: "failed to serialize log entry",
            error: String(err),
        });
    }
}

function sortObjectKeys(value) {
    if (Array.isArray(value)) {
        return value.map(sortObjectKeys);
    }

    if (!value || typeof value !== "object") {
        return value;
    }

    const result = {};

    for (const key of Object.keys(value).sort()) {
        result[key] = sortObjectKeys(value[key]);
    }

    return result;
}

function makeJsonSafe(value, maxDepth = 6) {
    const seen = new WeakSet();

    function convert(current, depth) {
        if (current === null || current === undefined) {
            return current;
        }

        if (current instanceof Error) {
            return stripUndefined({
                name: current.name,
                message: current.message,
                stack: current.stack,
                code: current.code,
            });
        }

        const type = typeof current;

        if (type === "bigint") {
            return current.toString();
        }

        if (type === "string" || type === "number" || type === "boolean") {
            return current;
        }

        if (type === "function") {
            return `[Function: ${current.name || "anonymous"}]`;
        }

        if (type !== "object") {
            return String(current);
        }

        if (seen.has(current)) {
            return "[Circular]";
        }

        if (depth >= maxDepth) {
            return Array.isArray(current)
                ? "[Array: Max Depth Reached]"
                : "[Object: Max Depth Reached]";
        }

        seen.add(current);

        if (Array.isArray(current)) {
            return current.map((item) => convert(item, depth + 1));
        }

        const result = {};

        for (const [key, item] of Object.entries(current)) {
            if (item !== undefined) {
                result[key] = convert(item, depth + 1);
            }
        }

        return result;
    }

    return convert(value, 0);
}

function safeProcessCwd() {
    try {
        return process.cwd();
    } catch (_) {
        return undefined;
    }
}

async function readSessionInfo(filePath) {
    try {
        const raw = await fs.promises.readFile(filePath, "utf8");
        const parsed = JSON.parse(raw);

        if (!parsed || typeof parsed !== "object") {
            return null;
        }

        return parsed;
    } catch (err) {
        if (err?.code === "ENOENT") {
            return null;
        }

        return null;
    }
}

async function writeJsonAtomic(filePath, value) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);

    const tmpPath = path.join(
        dir,
        `.${base}.${process.pid}.${Date.now()}.tmp`
    );

    const data = `${JSON.stringify(value, null, 2)}\n`;

    await fs.promises.mkdir(dir, { recursive: true });
    await fs.promises.writeFile(tmpPath, data, "utf8");
    await fs.promises.rename(tmpPath, filePath);
}

function stripUndefined(value) {
    if (!value || typeof value !== "object") {
        return value;
    }

    if (Array.isArray(value)) {
        return value.map(stripUndefined);
    }

    const result = {};

    for (const [key, item] of Object.entries(value)) {
        if (item !== undefined) {
            result[key] = stripUndefined(item);
        }
    }

    return result;
}

function escapeXmlAttribute(value) {
    return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

/**
 * Safely reflect object structure for debug logs.
 */
function dumpStructure(obj, maxDepth = 2) {
    const seen = new WeakSet();

    function reflect(current, depth) {
        if (current === null || current === undefined) {
            return String(current);
        }

        const type = typeof current;

        if (type !== "object" && type !== "function") {
            return current;
        }

        if (type === "function") {
            return `[Function: ${current.name || "anonymous"}]`;
        }

        if (seen.has(current)) {
            return "[Circular]";
        }

        seen.add(current);

        if (depth >= maxDepth) {
            try {
                return {
                    __type: Array.isArray(current)
                        ? "Array (Max Depth Reached)"
                        : "Object (Max Depth Reached)",
                    keys: Object.keys(current),
                };
            } catch (_) {
                return "[Unreadable Object]";
            }
        }

        try {
            if (Array.isArray(current)) {
                return current
                    .slice(0, 50)
                    .map((item) => reflect(item, depth + 1));
            }

            const snapshot = {};

            for (const key of Object.keys(current)) {
                try {
                    snapshot[key] = reflect(current[key], depth + 1);
                } catch (err) {
                    snapshot[key] = `[Property Unreadable: ${err.message}]`;
                }
            }

            return snapshot;
        } catch (err) {
            return `[Object Unreflectable: ${err.message}]`;
        }
    }

    return reflect(obj, 0);
}

/**
 * Preview system prompt head for logs.
 */
function previewSystemHead(system, maxItems = 3, maxChars = 500) {
    if (!Array.isArray(system)) {
        return {
            type: typeof system,
            value: segmentText(system).slice(0, maxChars),
        };
    }

    return system.slice(0, maxItems).map((item, index) => {
        const text = segmentText(item);

        return {
            index,
            type: typeof item,
            preview: text.slice(0, maxChars),
            size: text.length,
        };
    });
}
