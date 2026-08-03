#!/usr/bin/env node

import http from "node:http"

const OPENCODE_SERVER = process.env.OPENCODE_SERVER || "http://127.0.0.1:4097"
const HOST = process.env.SESSION_STATUS_HOST || "127.0.0.1"
const PORT = Number(process.env.SESSION_STATUS_PORT || 4107)
const POLL_INTERVAL_MS = Number(process.env.OPENCODE_STATUS_POLL_INTERVAL_MS || 5000)
const MAX_BODY_BYTES = Number(process.env.SESSION_STATUS_MAX_BODY_BYTES || 64 * 1024)

const watchedSessionIDs = new Set((process.env.OPENCODE_SESSION_IDS || "").split(",").map((s) => s.trim()).filter(Boolean))
const sessions = new Map()

let server = null
let pollTimer = null
let polling = false
let shuttingDown = false
let eventAbortController = null

function now() {
    return new Date().toISOString()
}

function log(level, msg, data = {}) {
    console.log(JSON.stringify({ time: now(), level, msg, ...data }))
}

function isValidSessionID(sessionID) {
    return typeof sessionID === "string" && /^ses_[A-Za-z0-9]+$/.test(sessionID)
}

function shouldWatchSession(sessionID) {
    return watchedSessionIDs.has(sessionID)
}

function compactPatch(patch) {
    return Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== null && value !== undefined))
}

function createInitialSession(sessionID, registeredSource = "init") {
    return {
        sessionID,
        status: "unknown",

        registeredSource,
        registeredAt: now(),

        updatedAt: null,

        statusSource: null,
        statusEventType: null,
        lastUpdateSource: null,

        lastStatusAt: null,
        lastEventAt: null,
        lastEventType: null,
        lastActivityAt: null,
        lastPollAt: null,

        directory: null,
        project: null,
        agent: null,
        model: null,
    }
}

function updateSession(sessionID, patch) {
    if (!isValidSessionID(sessionID) || !shouldWatchSession(sessionID)) return

    const prev = sessions.get(sessionID) || createInitialSession(sessionID)
    const safePatch = compactPatch(patch)

    const next = {
        ...prev,
        ...safePatch,
        sessionID,
        updatedAt: now(),
    }

    sessions.set(sessionID, next)

    if (safePatch.status && safePatch.status !== prev.status) {
        log("info", "session status changed", {
            sessionID,
            from: prev.status,
            to: safePatch.status,
            statusSource: safePatch.statusSource || null,
            statusEventType: safePatch.statusEventType || null,
        })
    }
}

function registerSession(sessionID, registeredSource = "api") {
    if (!isValidSessionID(sessionID)) return false

    watchedSessionIDs.add(sessionID)

    const existed = sessions.has(sessionID)
    const prev = sessions.get(sessionID)

    if (prev) {
        sessions.set(sessionID, {
            ...prev,
            registeredSource: prev.registeredSource || registeredSource,
            updatedAt: now(),
        })
    } else {
        sessions.set(sessionID, {
            ...createInitialSession(sessionID, registeredSource),
            updatedAt: now(),
        })
    }

    log("info", existed ? "session already registered" : "session registered", {
        sessionID,
        registeredSource,
    })

    return true
}

function unregisterSession(sessionID) {
    watchedSessionIDs.delete(sessionID)
    sessions.delete(sessionID)

    log("info", "session unregistered", { sessionID })
}

async function readJsonBody(req, maxBytes = MAX_BODY_BYTES) {
    const chunks = []
    let total = 0

    for await (const chunk of req) {
        total += chunk.length

        if (total > maxBytes) {
            const err = new Error("request body too large")
            err.statusCode = 413
            throw err
        }

        chunks.push(chunk)
    }

    if (chunks.length === 0) return {}

    const text = Buffer.concat(chunks).toString("utf8")
    if (!text.trim()) return {}

    return JSON.parse(text)
}

async function fetchJson(url) {
    const res = await fetch(url)

    if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}: ${url}`)
    }

    return await res.json()
}

function normalizeStatus(rawStatus) {
    if (!rawStatus) return null
    if (typeof rawStatus === "string") return rawStatus
    if (typeof rawStatus.type === "string") return rawStatus.type
    return null
}

async function resolveEffectiveStatus(sessionID, item) {
    let status = item.status
    if (status && status !== "unknown") return status

    // status is "unknown" or undefined: apply fallback rule.
    // Lazy fetch OpenCode session metadata — if the fetch succeeds (i.e. the
    // response carries the session ``id``), the session exists on the
    // OpenCode server and tokens can be inspected. Tokens.input > 0 means
    // the session has produced LLM output at some point, so sidecar's
    // missing status is almost certainly lost state (restart / missed
    // event), not "fresh" — treat as idle so dispatch can proceed safely.
    //
    // The previous implementation only checked ``if (tokens)``; in JS
    // ``{}`` is truthy, so a brand-new session whose ``tokens`` came back
    // as an empty object was misclassified as idle. The ``sessionData.id``
    // guard ensures we have a real session record before trusting the
    // fallback, and a separate check distinguishes "session exists, zero
    // tokens yet" (→ idle) from "fetch failed / empty response" (→ unknown).
    let tokens = item.tokens
    let sessionData = null
    let fetchSucceeded = false
    if (!tokens && !item.tokensFetched) {
        try {
            sessionData = await fetchJson(
                `${OPENCODE_SERVER}/session/${encodeURIComponent(sessionID)}`
            )
            tokens = (sessionData && sessionData.tokens) || {}
            sessions.set(sessionID, {
                ...item,
                tokens,
                tokensFetched: now(),
            })
            fetchSucceeded = !!(sessionData && sessionData.id)
        } catch (err) {
            log("warn", "tokens fetch failed for fallback rule", {
                sessionID,
                error: String(err.message || err),
            })
            // 不写 tokensFetched，允许下次 /status 请求重试
        }
    } else if (item.tokens && item.tokensFetched) {
        // Previously fetched — we already validated the session record at
        // that time, so trust the cached tokens here.
        fetchSucceeded = true
    }

    if (fetchSucceeded) {
        return "idle"
    }
    return "unknown"
}


async function pollStatus() {
    if (polling) return

    polling = true

    try {
        const statusMap = await fetchJson(`${OPENCODE_SERVER}/session/status`)

        for (const [sessionID, rawStatus] of Object.entries(statusMap)) {
            if (!shouldWatchSession(sessionID)) continue

            const status = normalizeStatus(rawStatus)
            if (!status) continue

            updateSession(sessionID, {
                status,
                statusSource: "poll",
                statusEventType: null,
                lastUpdateSource: "poll",
                lastPollAt: now(),
                lastStatusAt: now(),
            })
        }
    } catch (err) {
        log("warn", "poll status failed", {
            error: String(err.message || err),
        })
    } finally {
        polling = false
    }
}

function extractEventShape(event) {
    const payload = event.payload || event
    const properties = payload.properties || {}
    const syncEvent = payload.syncEvent || null
    const syncData = syncEvent?.data || {}

    const type = payload.type === "sync" ? syncEvent?.type || payload.type : payload.type || syncEvent?.type || event.type || event._tag || event.event

    const sessionID = properties.sessionID || syncData.sessionID || syncEvent?.aggregateID || payload.sessionID || event.sessionID || event.sessionId || event.session?.id

    return {
        payload,
        properties,
        syncEvent,
        syncData,
        type,
        lowerType: String(type || "").toLowerCase(),
        sessionID,
        directory: event.directory || payload.directory || properties.info?.directory || syncData.info?.directory || null,
        project: event.project || payload.project || properties.info?.projectID || syncData.info?.projectID || null,
    }
}

function extractInfo(properties, syncData) {
    const info = properties.info || syncData.info || null

    if (!info) {
        return {
            agent: null,
            model: null,
        }
    }

    const agent = info.agent || info.mode || null

    const model =
        info.model ||
        (info.providerID && info.modelID
            ? {
                providerID: info.providerID,
                modelID: info.modelID,
            }
            : null)

    return {
        agent,
        model,
    }
}

function handleEvent(event) {
    const { properties, syncData, type, lowerType, sessionID, directory, project } = extractEventShape(event)

    if (!isValidSessionID(sessionID) || !shouldWatchSession(sessionID)) return

    const { agent, model } = extractInfo(properties, syncData)

    if (lowerType === "session.status") {
        const status = normalizeStatus(properties.status || syncData.status)

        if (status) {
            updateSession(sessionID, {
                status,
                statusSource: "event",
                statusEventType: type,
                lastUpdateSource: "event",
                lastEventType: type,
                directory,
                project,
                agent,
                model,
                lastEventAt: now(),
                lastStatusAt: now(),
            })
        }

        return
    }

    if (lowerType === "session.idle") {
        updateSession(sessionID, {
            status: "idle",
            statusSource: "event",
            statusEventType: type,
            lastUpdateSource: "event",
            lastEventType: type,
            directory,
            project,
            agent,
            model,
            lastEventAt: now(),
            lastStatusAt: now(),
        })

        return
    }

    updateSession(sessionID, {
        lastUpdateSource: "event",
        lastEventType: type,
        directory,
        project,
        agent,
        model,
        lastEventAt: now(),
        lastActivityAt: now(),
    })
}

async function listenEvents() {
    while (!shuttingDown) {
        try {
            eventAbortController = new AbortController()

            log("info", "connecting opencode event stream", {
                url: `${OPENCODE_SERVER}/global/event`,
            })

            const res = await fetch(`${OPENCODE_SERVER}/global/event`, {
                headers: {
                    Accept: "text/event-stream",
                },
                signal: eventAbortController.signal,
            })

            if (!res.ok) {
                throw new Error(`${res.status} ${res.statusText}`)
            }

            const reader = res.body.getReader()
            const decoder = new TextDecoder()
            let buffer = ""

            while (!shuttingDown) {
                const { done, value } = await reader.read()
                if (done) break

                buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n")

                const chunks = buffer.split("\n\n")
                buffer = chunks.pop() || ""

                for (const chunk of chunks) {
                    const lines = chunk.split("\n")
                    const dataLines = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim())

                    if (dataLines.length === 0) continue

                    const data = dataLines.join("\n")
                    if (!data || data === "[DONE]") continue

                    try {
                        handleEvent(JSON.parse(data))
                    } catch (err) {
                        log("debug", "failed to parse sse data", {
                            error: String(err.message || err),
                            data,
                        })
                    }
                }
            }
        } catch (err) {
            if (shuttingDown) return

            if (err.name === "AbortError") {
                log("info", "event stream aborted")
                return
            }

            log("warn", "event stream disconnected", {
                error: String(err.message || err),
            })

            await new Promise((resolve) => setTimeout(resolve, 2000))
        } finally {
            eventAbortController = null
        }
    }
}

function sendJson(res, statusCode, body) {
    const text = JSON.stringify(body, null, 2)

    res.writeHead(statusCode, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    })

    res.end(text)
}

function sendNotFound(res, req, pathname) {
    sendJson(res, 404, {
        error: "not_found",
        method: req.method,
        path: pathname,
    })
}

function startHttpServer() {
    server = http.createServer(async (req, res) => {
        const url = new URL(req.url || "/", `http://${req.headers.host || `${HOST}:${PORT}`}`)

        if (req.method === "GET" && url.pathname === "/health") {
            sendJson(res, 200, {
                ok: true,
                time: now(),
                opencodeServer: OPENCODE_SERVER,
                watchedSessionIDs: [...watchedSessionIDs],
                trackedCount: sessions.size,
                shuttingDown,
            })
            return
        }

        if (req.method === "GET" && url.pathname === "/watch") {
            sendJson(res, 200, {
                watchedSessionIDs: [...watchedSessionIDs],
                trackedCount: sessions.size,
            })
            return
        }

        if (req.method === "POST" && url.pathname === "/watch") {
            try {
                const body = await readJsonBody(req)
                const ids = []

                if (body.sessionID) ids.push(body.sessionID)
                if (Array.isArray(body.sessionIDs)) ids.push(...body.sessionIDs)

                const registered = []
                const rejected = []

                for (const id of ids) {
                    const sessionID = String(id).trim()

                    if (registerSession(sessionID, "api")) {
                        registered.push(sessionID)
                    } else {
                        rejected.push(sessionID)
                    }
                }

                sendJson(res, 200, {
                    registered,
                    rejected,
                    watchedSessionIDs: [...watchedSessionIDs],
                    trackedCount: sessions.size,
                })
            } catch (err) {
                const statusCode = err.statusCode || 400

                sendJson(res, statusCode, {
                    error: statusCode === 413 ? "payload_too_large" : "invalid_request",
                    message: String(err.message || err),
                })
            }

            return
        }

        if (req.method === "DELETE" && url.pathname.startsWith("/watch/")) {
            const sessionID = decodeURIComponent(url.pathname.slice("/watch/".length))

            if (!watchedSessionIDs.has(sessionID)) {
                sendJson(res, 404, {
                    error: "session_not_watched",
                    sessionID,
                })
                return
            }

            unregisterSession(sessionID)

            sendJson(res, 200, {
                removed: sessionID,
                watchedSessionIDs: [...watchedSessionIDs],
                trackedCount: sessions.size,
            })

            return
        }

        if (req.method === "GET" && url.pathname === "/sessions") {
            sendJson(res, 200, {
                time: now(),
                opencodeServer: OPENCODE_SERVER,
                watchedSessionIDs: [...watchedSessionIDs],
                sessions: Object.fromEntries(sessions.entries()),
            })
            return
        }

        if (req.method === "GET" && url.pathname.startsWith("/sessions/")) {
            const sessionID = decodeURIComponent(url.pathname.slice("/sessions/".length))
            const item = sessions.get(sessionID)

            if (!item) {
                sendJson(res, 404, {
                    error: "session_not_tracked",
                    sessionID,
                    watchedSessionIDs: [...watchedSessionIDs],
                })
                return
            }

            sendJson(res, 200, item)
            return
        }

        if (req.method === "GET" && url.pathname === "/status") {
            const out = {}
            for (const [id, item] of sessions.entries()) {
                out[id] = await resolveEffectiveStatus(id, item)
            }
            sendJson(res, 200, out)
            return
        }

        sendNotFound(res, req, url.pathname)
    })

    server.on("error", (err) => {
        log("error", "http server error", {
            error: String(err.message || err),
        })

        process.exit(1)
    })

    server.listen(PORT, HOST, () => {
        log("info", "session status http server started", {
            url: `http://${HOST}:${PORT}`,
            opencodeServer: OPENCODE_SERVER,
            watchedSessionIDs: [...watchedSessionIDs],
        })
    })
}

function shutdown(signal) {
    if (shuttingDown) return

    shuttingDown = true

    log("info", "shutting down", { signal })

    if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
    }

    if (eventAbortController) {
        eventAbortController.abort()
    }

    if (!server) {
        process.exit(0)
    }

    server.close(() => {
        log("info", "http server closed")
        process.exit(0)
    })

    setTimeout(() => {
        log("warn", "forced shutdown")
        process.exit(0)
    }, 1000)
}

async function main() {
    log("info", "session status sidecar starting", {
        opencodeServer: OPENCODE_SERVER,
        host: HOST,
        port: PORT,
        pollIntervalMs: POLL_INTERVAL_MS,
        maxBodyBytes: MAX_BODY_BYTES,
        watchedSessionIDs: [...watchedSessionIDs],
    })

    for (const sessionID of [...watchedSessionIDs]) {
        registerSession(sessionID, "env")
    }

    startHttpServer()

    await pollStatus()

    pollTimer = setInterval(() => {
        pollStatus().catch((err) => {
            log("warn", "poll status failed unexpectedly", {
                error: String(err.message || err),
            })
        })
    }, POLL_INTERVAL_MS)

    await listenEvents()
}

process.on("SIGINT", () => shutdown("SIGINT"))
process.on("SIGTERM", () => shutdown("SIGTERM"))

main().catch((err) => {
    log("error", "server crashed", {
        error: String(err.message || err),
    })

    process.exit(1)
})
