local G = getgenv and getgenv() or _G

if G.CronusRejoin and G.CronusRejoin.Stop then
    pcall(function()
        G.CronusRejoin:Stop()
    end)
end

local CronusRejoin = {
    Host = __CRONUS_HOST__,
    Port = __CRONUS_PORT__,
    Token = __CRONUS_TOKEN__,
    Account = __CRONUS_ACCOUNT__,
    SessionId = __CRONUS_SESSION_ID__,
    LaunchNonce = __CRONUS_LAUNCH_NONCE__,
    ExpectedPid = __CRONUS_PROCESS_ID__,
    Version = "1.7.1",
    ShutdownDelay = __CRONUS_SHUTDOWN_DELAY__,
    RequeueSource = __CRONUS_REQUEUE_SOURCE__,
    Running = true,
    Connections = {},
    LastSent = {},
    LastErrorCode = "",
    LastDisconnectAt = 0,
    LastPostOk = {},
    ConnectionAliveLogged = false,
    FallbackScheduled = {},
    EventCounter = 0,
    FallbackEvents = {
        heartbeat = true,
        teleport_state = true,
    },
    TeleportQueueInstalled = false,
    TeleportStartedAt = 0,
    TeleportState = "",
}

local HttpService = game:GetService("HttpService")
local GuiService = game:GetService("GuiService")
local Players = game:GetService("Players")
local TeleportService = game:GetService("TeleportService")

local LocalPlayer = Players.LocalPlayer
if not LocalPlayer then
    repeat
        task.wait()
        LocalPlayer = Players.LocalPlayer
    until LocalPlayer
end

local Request =
    (syn and syn.request)
    or (http and http.request)
    or http_request
    or request

local function emit(message, level)
    local line = "[Cronus] " .. tostring(message or "")
    if rconsoleprint then
        pcall(rconsoleprint, line .. "\n")
    end
    if level == "warn" and warn then
        pcall(warn, line)
    elseif print then
        pcall(print, line)
    end
end

local function log(message)
    emit(message, "info")
end

local function logWarn(message)
    emit(message, "warn")
end

local function now()
    return os.clock()
end

local function safeString(value)
    if value == nil then
        return ""
    end
    local ok, text = pcall(tostring, value)
    return ok and text or ""
end

local function urlEncode(value)
    local text = safeString(value)
    return text:gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
end

local function getErrorCode()
    local code = 0
    pcall(function()
        local err = GuiService:GetErrorCode()
        code = tonumber(err and err.Value) or 0
    end)
    return code
end

local function getErrorMessage()
    local message = ""
    pcall(function()
        if GuiService.GetErrorMessage then
            message = safeString(GuiService:GetErrorMessage())
        end
    end)
    return message
end

local function getProcessId()
    local providers = {
        rawget(_G, "getprocessid"),
        rawget(_G, "get_process_id"),
        rawget(_G, "getpid"),
        rawget(_G, "get_pid"),
    }
    for _, provider in ipairs(providers) do
        if type(provider) == "function" then
            local ok, result = pcall(provider)
            local pid = tonumber(result)
            if ok and pid and pid > 0 then
                return tostring(math.floor(pid))
            end
        end
    end
    return ""
end

local function getServerInfo()
    -- Client Lua cannot safely inspect private server identifiers; Cronus infers that from launch intent.
    return {
        private_server_id = "",
        private_server_owner_id = "",
        is_vip_server = "false",
        server_type = "PUBLIC",
    }
end

local function getQueueOnTeleport()
    local providers = {
        queue_on_teleport,
        queueonteleport,
        queueonTeleport,
        rawget(_G, "queue_on_teleport"),
        rawget(_G, "queueonteleport"),
        rawget(_G, "queueonTeleport"),
    }
    if syn then
        table.insert(providers, syn.queue_on_teleport)
    end
    if fluxus then
        table.insert(providers, fluxus.queue_on_teleport)
    end
    for _, provider in ipairs(providers) do
        if type(provider) == "function" then
            return provider
        end
    end
    return nil
end

function CronusRejoin:Endpoint()
    local host = safeString(self and self.Host or CronusRejoin.Host)
    if host == "" then
        host = "127.0.0.1"
    end

    local port = safeString(self and self.Port or CronusRejoin.Port)
    if port == "" then
        port = "7777"
    end

    return "http://" .. host .. ":" .. port .. "/api/lua/rejoin-event"
end

function CronusRejoin:EndpointWithToken()
    return self:Endpoint() .. "?cronus_token=" .. urlEncode(self.Token)
end

function CronusRejoin:QueryEndpoint(payload)
    local url = self:EndpointWithToken()
    local keys = {
        "event",
        "account",
        "username",
        "configured_account",
        "session_id",
        "launch_nonce",
        "event_id",
        "user_id",
        "pid",
        "place_id",
        "job_id",
        "universe_id",
        "private_server_id",
        "private_server_owner_id",
        "is_vip_server",
        "server_type",
        "teleport_state",
        "teleport_place_id",
        "teleport_elapsed",
        "requeue",
        "error_code",
        "message",
        "reason_key",
        "detail",
        "executor",
        "helper_version",
        "visual_disconnect",
        "evidence_source",
        "detection_source",
        "ts",
    }

    for _, key in ipairs(keys) do
        local value = payload and payload[key]
        local text = safeString(value)
        if text ~= "" then
            url = url .. "&" .. urlEncode(key) .. "=" .. urlEncode(text)
        end
    end

    return url
end

function CronusRejoin:NextEventId(eventName)
    self.EventCounter = (tonumber(self.EventCounter) or 0) + 1
    return table.concat({
        safeString(self.SessionId),
        safeString(self.LaunchNonce),
        safeString(eventName),
        safeString(os.time()),
        safeString(math.floor(now() * 1000)),
        safeString(self.EventCounter),
        safeString(math.random(100000, 999999)),
    }, ":")
end

function CronusRejoin:Payload(eventName, extra)
    extra = extra or {}
    local serverInfo = getServerInfo()
    local processId = getProcessId()
    if processId == "" then
        processId = safeString(self.ExpectedPid)
    end
    local payload = {
        event = safeString(eventName),
        account = safeString(LocalPlayer.Name),
        username = safeString(LocalPlayer.Name),
        configured_account = safeString(self.Account),
        session_id = safeString(self.SessionId),
        launch_nonce = safeString(self.LaunchNonce),
        event_id = self:NextEventId(eventName),
        user_id = safeString(LocalPlayer.UserId),
        pid = processId,
        place_id = safeString(game.PlaceId),
        job_id = safeString(game.JobId),
        universe_id = safeString(game.GameId),
        private_server_id = serverInfo.private_server_id,
        private_server_owner_id = serverInfo.private_server_owner_id,
        is_vip_server = serverInfo.is_vip_server,
        server_type = serverInfo.server_type,
        error_code = safeString(extra.error_code or ""),
        message = safeString(extra.message or ""),
        reason_key = safeString(extra.reason_key or ""),
        detail = safeString(extra.detail or ""),
        executor = identifyexecutor and safeString(identifyexecutor()) or "",
        helper_version = safeString(self.Version),
        token = safeString(self.Token),
        cronus_token = safeString(self.Token),
        api_token = safeString(self.Token),
        _cronus_token = safeString(self.Token),
        ts = safeString(os.time()),
    }

    for key, value in pairs(extra) do
        if payload[key] == nil then
            payload[key] = safeString(value)
        end
    end

    return payload
end

function CronusRejoin:CanUseGetFallback(eventName)
    local key = safeString(eventName):lower()
    return self.FallbackEvents[key] == true
end

function CronusRejoin:FallbackOrFail(eventName, payload, previousStatus)
    if self:CanUseGetFallback(eventName) then
        return self:GetFallback(eventName, payload, previousStatus)
    end
    self.LastPostOk[eventName] = false
    return false, false
end

function CronusRejoin:Post(eventName, extra)
    if not self.Running then
        return false
    end

    if not Request and not game.HttpGet then
        if eventName ~= "heartbeat" then
            logWarn("Launcher not responding")
        end
        return false
    end

    local payload = self:Payload(eventName, extra)
    local dedupeKey = payload.event .. ":" .. payload.error_code .. ":" .. payload.reason_key
    local t = now()
    if self.LastSent[dedupeKey] and (t - self.LastSent[dedupeKey]) < 2 then
        return true
    end
    self.LastSent[dedupeKey] = t

    local encodeOk, body = pcall(function()
        return HttpService:JSONEncode(payload)
    end)
    if not encodeOk then
        if eventName ~= "heartbeat" then
            logWarn("Launcher not responding")
        end
        return false
    end

    local endpoint = self:EndpointWithToken()
    local publicEndpoint = self:Endpoint()
    local requestHeaders = {
        ["Content-Type"] = "application/json",
        ["X-Cronus-Token"] = self.Token,
        ["x-cronus-token"] = self.Token,
        ["User-Agent"] = "CronusLuaRejoin/1.7",
    }

    if not Request then
        return self:FallbackOrFail(eventName, payload, "no_request")
    end

    if eventName ~= "heartbeat" then
        log("Syncing status...")
    end
    local ok, response = pcall(Request, {
        Method = "POST",
        Url = endpoint,
        Headers = requestHeaders,
        headers = requestHeaders,
        Body = body,
        body = body,
        Data = body,
        data = body,
        Timeout = 5,
    })

    if not ok then
        if eventName ~= "heartbeat" then
            logWarn("Launcher not responding")
        end
        return self:FallbackOrFail(eventName, payload, "request_error")
    end

    local status = tonumber(response and (response.StatusCode or response.Status)) or 0
    local success = status >= 200 and status < 300
    local accepted = success
    local responseBody = response and (response.Body or response.body)
    if success and responseBody and responseBody ~= "" then
        local decodedOk, decoded = pcall(function()
            return HttpService:JSONDecode(responseBody)
        end)
        if decodedOk and type(decoded) == "table" and decoded.accepted ~= nil then
            accepted = decoded.accepted == true
        end
    end
    if success and accepted then
        if eventName == "heartbeat" then
            if not self.ConnectionAliveLogged then
                self.ConnectionAliveLogged = true
                log("Connection alive")
            end
        else
            log("Status synced")
        end
    else
        if eventName ~= "heartbeat" then
            logWarn("Launcher not responding")
        end
    end
    if not success then
        return self:FallbackOrFail(eventName, payload, status)
    end
    self.LastPostOk[eventName] = success and accepted
    return success, accepted
end

function CronusRejoin:GetFallback(eventName, payload, previousStatus)
    local quiet = eventName == "heartbeat"
    local url = self:QueryEndpoint(payload)
    local requestHeaders = {
        ["User-Agent"] = "CronusLuaRejoin/1.7",
    }
    if not quiet then
        logWarn("Trying fallback recovery...")
    end

    local ok, response
    if Request then
        ok, response = pcall(Request, {
            Method = "GET",
            Url = url,
            Headers = requestHeaders,
            headers = requestHeaders,
            Timeout = 5,
        })
    else
        ok, response = pcall(function()
            return game:HttpGet(url)
        end)
    end

    if not ok then
        if not quiet then
            logWarn("Launcher not responding")
        end
        self.LastPostOk[eventName] = false
        return false, false
    end

    local status = 200
    local responseBody = response
    if type(response) == "table" then
        status = tonumber(response.StatusCode or response.Status) or 0
        responseBody = response.Body or response.body or response.Data or response.data or ""
    end

    local success = status >= 200 and status < 300
    local accepted = success
    if success and responseBody and responseBody ~= "" then
        local decodedOk, decoded = pcall(function()
            return HttpService:JSONDecode(responseBody)
        end)
        if decodedOk and type(decoded) == "table" and decoded.accepted ~= nil then
            accepted = decoded.accepted == true
        end
    end

    if success and accepted then
        if eventName == "heartbeat" then
            if not self.ConnectionAliveLogged then
                self.ConnectionAliveLogged = true
                log("Connection alive")
            end
        else
            log("Status synced")
        end
    else
        if not quiet then
            logWarn("Launcher not responding")
        end
    end
    self.LastPostOk[eventName] = success and accepted
    return success, accepted
end

function CronusRejoin:QueueOnTeleport(reason)
    if self.TeleportQueueInstalled then
        return true
    end
    local source = safeString(self.RequeueSource)
    if source == "" then
        logWarn("Rejoin helper failed to load")
        return false
    end
    local provider = getQueueOnTeleport()
    if not provider then
        logWarn("Executor does not support auto-run")
        return false
    end

    local ok, err = pcall(provider, source)
    if ok then
        self.TeleportQueueInstalled = true
        log("Rejoin helper restored")
        return true
    end

    logWarn("Executor does not support auto-run")
    return false
end

function CronusRejoin:IsTeleportTransitionActive()
    if not self.TeleportStartedAt or self.TeleportStartedAt <= 0 then
        return false
    end
    return (now() - self.TeleportStartedAt) <= 45.0
end

function CronusRejoin:PostAsync(eventName, extra)
    task.spawn(function()
        local ok, err = pcall(function()
            self:Post(eventName, extra)
        end)
        if not ok and eventName ~= "heartbeat" then
            logWarn("Launcher not responding")
        end
    end)
    return true
end

function CronusRejoin:ClientRecoveryFallback(codeText)
    local key = safeString(codeText)
    if self.FallbackScheduled[key] then
        return
    end
    self.FallbackScheduled[key] = true
    task.delay(2.0, function()
        if not self.Running then
            return
        end
        if self.LastPostOk.disconnect == true then
            return
        end
        logWarn("Trying fallback recovery...")
        log("Attempting rejoin...")
        pcall(function()
            TeleportService:Teleport(game.PlaceId, LocalPlayer)
        end)
        task.delay(1.0, function()
            if not CronusRejoin.Running then
                return
            end
            pcall(function()
                LocalPlayer:Kick("Cronus recovery fallback")
            end)
            pcall(game.Shutdown, game)
        end)
    end)
end

function CronusRejoin:Stop()
    self.Running = false
    for _, connection in ipairs(self.Connections) do
        pcall(function()
            connection:Disconnect()
        end)
    end
    table.clear(self.Connections)
end

function CronusRejoin:Rejoin()
    self:PostAsync("rejoin_requested", {
        reason_key = "lua_manual_rejoin",
        detail = "Manual Lua rejoin requested",
    })
end

local function reportLoaded()
    log("Game detected")
    CronusRejoin:PostAsync("loaded", {
        reason_key = "lua_loaded",
        detail = "Lua helper loaded in Roblox client",
    })
end

local function hasServerEvidence()
    local placeId = tonumber(game.PlaceId) or 0
    local jobId = safeString(game.JobId)
    return placeId > 0 and jobId ~= ""
end

local function reportInGame()
    if not hasServerEvidence() then
        return false
    end
    log("Connected to game")
    CronusRejoin:PostAsync("in_game", {
        reason_key = "lua_in_game_verified",
        detail = "Lua verified Roblox server session",
        evidence_source = "lua_server_job",
    })
    return true
end

local function reportDisconnect(source)
    local code = getErrorCode()
    if code <= 0 then
        return
    end

    local disconnectBase = 0
    pcall(function()
        disconnectBase = Enum.ConnectionError.DisconnectErrors.Value
    end)
    if disconnectBase > 0 and code < disconnectBase then
        return
    end

    local codeText = safeString(code)
    local t = now()
    if CronusRejoin.LastErrorCode == codeText and (t - CronusRejoin.LastDisconnectAt) < 3 then
        return
    end
    CronusRejoin.LastErrorCode = codeText
    CronusRejoin.LastDisconnectAt = t

    if CronusRejoin:IsTeleportTransitionActive() then
        local elapsed = t - CronusRejoin.TeleportStartedAt
        CronusRejoin:PostAsync("teleport_state", {
            reason_key = "lua_teleport_transition",
            error_code = codeText,
            detail = "Disconnect signal ignored during active Roblox teleport",
            teleport_state = safeString(CronusRejoin.TeleportState),
            teleport_place_id = safeString(game.PlaceId),
            teleport_elapsed = safeString(string.format("%.2f", elapsed)),
            evidence_source = "lua_guiservice_teleport_guard",
            detection_source = safeString(source or "event"),
        })
        return
    end

    logWarn("Disconnect detected")
    log("Attempting rejoin...")
    CronusRejoin:PostAsync("disconnect", {
        reason_key = "lua_disconnect_error",
        error_code = codeText,
        message = getErrorMessage(),
        detail = ("Roblox disconnect error code %s"):format(codeText),
        visual_disconnect = "true",
        evidence_source = "lua_guiservice",
        detection_source = safeString(source or "event"),
    })
    CronusRejoin:ClientRecoveryFallback(codeText)

    local shutdownDelay = tonumber(CronusRejoin.ShutdownDelay) or 0
    if shutdownDelay > 0 then
        task.delay(shutdownDelay, function()
            if CronusRejoin.Running then
                logWarn("Trying fallback recovery...")
                pcall(game.Shutdown, game)
            end
        end)
    end
end

table.insert(CronusRejoin.Connections, GuiService.ErrorMessageChanged:Connect(function()
    reportDisconnect("GuiService.ErrorMessageChanged")
end))

pcall(function()
    table.insert(CronusRejoin.Connections, TeleportService.TeleportInitFailed:Connect(function(player, result, message, placeId)
        if player and player ~= LocalPlayer then
            return
        end
        CronusRejoin.TeleportStartedAt = 0
        CronusRejoin.TeleportState = "failed"
        logWarn("Trying fallback recovery...")
        CronusRejoin:PostAsync("teleport_error", {
            reason_key = "lua_teleport_error",
            message = safeString(message),
            detail = ("Teleport failed: %s"):format(safeString(result)),
            place_id = safeString(placeId or game.PlaceId),
            teleport_state = "failed",
            evidence_source = "lua_teleport_init_failed",
        })
    end))
end)

pcall(function()
    table.insert(CronusRejoin.Connections, LocalPlayer.OnTeleport:Connect(function(state)
        local stateText = safeString(state)
        CronusRejoin.TeleportStartedAt = now()
        CronusRejoin.TeleportState = stateText
        log("Teleport detected")
        log("Re-attaching after teleport...")
        CronusRejoin:QueueOnTeleport("teleport_state")
        CronusRejoin:PostAsync("teleport_state", {
            reason_key = "lua_teleport_state",
            detail = stateText,
            teleport_state = stateText,
            teleport_place_id = safeString(game.PlaceId),
            requeue = CronusRejoin.TeleportQueueInstalled and "true" or "false",
            evidence_source = "lua_on_teleport",
        })
    end))
end)

CronusRejoin:QueueOnTeleport("startup")

task.spawn(function()
    if not game:IsLoaded() then
        game.Loaded:Wait()
    end
    reportLoaded()
    for _ = 1, 8 do
        if reportInGame() then
            break
        end
        task.wait(2)
    end
end)

task.spawn(function()
    local lastHeartbeatAt = 0
    while CronusRejoin.Running do
        reportDisconnect("poll")
        local t = now()
        if (t - lastHeartbeatAt) >= 15 and hasServerEvidence() then
            lastHeartbeatAt = t
            CronusRejoin:PostAsync("heartbeat", {
                reason_key = "lua_server_heartbeat",
                detail = "Lua heartbeat in Roblox server session",
                evidence_source = "lua_server_job",
            })
        end
        task.wait(0.5)
    end
end)

G.CronusRejoin = CronusRejoin
log("Rejoin helper loaded")
return CronusRejoin
