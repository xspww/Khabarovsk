local G = getgenv and getgenv() or _G

if G.ArgusRejoin and G.ArgusRejoin.Stop then
    pcall(function()
        G.ArgusRejoin:Stop()
    end)
end

local ArgusRejoin = {
    Host = __ARGUS_HOST__,
    Port = __ARGUS_PORT__,
    Token = __ARGUS_TOKEN__,
    Account = __ARGUS_ACCOUNT__,
    Version = "1.7.0",
    ShutdownDelay = __ARGUS_SHUTDOWN_DELAY__,
    Running = true,
    Connections = {},
    LastSent = {},
    LastErrorCode = "",
    LastDisconnectAt = 0,
    LastPostOk = {},
    FallbackScheduled = {},
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

local function log(...)
    local parts = {}
    for _, value in ipairs({ ... }) do
        table.insert(parts, tostring(value))
    end
    local line = "[ArgusRejoin] " .. table.concat(parts, " ")
    if rconsoleprint then
        pcall(rconsoleprint, line .. "\n")
    end
    if print then
        pcall(print, line)
    end
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

function ArgusRejoin:Endpoint()
    local host = safeString(self and self.Host or ArgusRejoin.Host)
    if host == "" then
        host = "127.0.0.1"
    end

    local port = safeString(self and self.Port or ArgusRejoin.Port)
    if port == "" then
        port = "7777"
    end

    return "http://" .. host .. ":" .. port .. "/api/lua/rejoin-event"
end

function ArgusRejoin:EndpointWithToken()
    return self:Endpoint() .. "?argus_token=" .. urlEncode(self.Token)
end

function ArgusRejoin:QueryEndpoint(payload)
    local url = self:EndpointWithToken()
    local keys = {
        "event",
        "account",
        "username",
        "configured_account",
        "user_id",
        "pid",
        "place_id",
        "job_id",
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

function ArgusRejoin:Payload(eventName, extra)
    extra = extra or {}
    local payload = {
        event = safeString(eventName),
        account = safeString(LocalPlayer.Name),
        username = safeString(LocalPlayer.Name),
        configured_account = safeString(self.Account),
        user_id = safeString(LocalPlayer.UserId),
        pid = getProcessId(),
        place_id = safeString(game.PlaceId),
        job_id = safeString(game.JobId),
        error_code = safeString(extra.error_code or ""),
        message = safeString(extra.message or ""),
        reason_key = safeString(extra.reason_key or ""),
        detail = safeString(extra.detail or ""),
        executor = identifyexecutor and safeString(identifyexecutor()) or "",
        helper_version = safeString(self.Version),
        token = safeString(self.Token),
        argus_token = safeString(self.Token),
        api_token = safeString(self.Token),
        _argus_token = safeString(self.Token),
        ts = safeString(os.time()),
    }

    for key, value in pairs(extra) do
        if payload[key] == nil then
            payload[key] = safeString(value)
        end
    end

    return payload
end

function ArgusRejoin:Post(eventName, extra)
    if not self.Running then
        return false
    end

    if not Request and not game.HttpGet then
        log("http request unavailable for", eventName)
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
        log("json encode failed", eventName, body)
        return false
    end

    local endpoint = self:EndpointWithToken()
    local publicEndpoint = self:Endpoint()
    local requestHeaders = {
        ["Content-Type"] = "application/json",
        ["X-Argus-Token"] = self.Token,
        ["X-RoboGuard-Token"] = self.Token,
        ["x-argus-token"] = self.Token,
        ["User-Agent"] = "ArgusLuaRejoin/1.7",
    }

    if not Request then
        return self:GetFallback(eventName, payload, "no_request")
    end

    log("post begin", eventName, publicEndpoint)
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
        log("post failed", eventName, response)
        return self:GetFallback(eventName, payload, "request_error")
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
    log("post", eventName, success and "ok" or "failed", "status=" .. tostring(status), "accepted=" .. tostring(accepted))
    if not success then
        return self:GetFallback(eventName, payload, status)
    end
    self.LastPostOk[eventName] = success and accepted
    return success, accepted
end

function ArgusRejoin:GetFallback(eventName, payload, previousStatus)
    local url = self:QueryEndpoint(payload)
    local requestHeaders = {
        ["User-Agent"] = "ArgusLuaRejoin/1.7",
    }
    log("get fallback begin", eventName, "after_status=" .. safeString(previousStatus))

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
        log("get fallback failed", eventName, response)
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

    log("get fallback", eventName, success and "ok" or "failed", "status=" .. tostring(status), "accepted=" .. tostring(accepted))
    self.LastPostOk[eventName] = success and accepted
    return success, accepted
end

function ArgusRejoin:PostAsync(eventName, extra)
    log("post async", eventName)
    task.spawn(function()
        local ok, err = pcall(function()
            self:Post(eventName, extra)
        end)
        if not ok then
            log("post task error", eventName, err)
        end
    end)
    return true
end

function ArgusRejoin:ClientRecoveryFallback(codeText)
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
            log("client fallback skipped", "Argus accepted disconnect")
            return
        end
        log("client fallback start", "error_code=" .. key)
        pcall(function()
            TeleportService:Teleport(game.PlaceId, LocalPlayer)
        end)
        task.delay(1.0, function()
            if not ArgusRejoin.Running then
                return
            end
            pcall(function()
                LocalPlayer:Kick("Argus recovery fallback")
            end)
            pcall(game.Shutdown, game)
            log("client fallback close requested", "error_code=" .. key)
        end)
    end)
end

function ArgusRejoin:Stop()
    self.Running = false
    for _, connection in ipairs(self.Connections) do
        pcall(function()
            connection:Disconnect()
        end)
    end
    table.clear(self.Connections)
end

function ArgusRejoin:Rejoin()
    self:PostAsync("rejoin_requested", {
        reason_key = "lua_manual_rejoin",
        detail = "Manual Lua rejoin requested",
    })
end

local function reportLoaded()
    log(
        "loaded",
        safeString(LocalPlayer.Name),
        "place=" .. safeString(game.PlaceId),
        "job=" .. safeString(game.JobId),
        "version=" .. safeString(ArgusRejoin.Version)
    )
    ArgusRejoin:PostAsync("loaded", {
        reason_key = "lua_loaded",
        detail = "Lua helper loaded in Roblox client",
    })
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
    if ArgusRejoin.LastErrorCode == codeText and (t - ArgusRejoin.LastDisconnectAt) < 3 then
        return
    end
    ArgusRejoin.LastErrorCode = codeText
    ArgusRejoin.LastDisconnectAt = t

    log("disconnect detected", "source=" .. safeString(source or "event"), "error_code=" .. codeText)
    ArgusRejoin:PostAsync("disconnect", {
        reason_key = "lua_disconnect_error",
        error_code = codeText,
        message = getErrorMessage(),
        detail = ("Roblox disconnect error code %s"):format(codeText),
        visual_disconnect = "true",
        evidence_source = "lua_guiservice",
        detection_source = safeString(source or "event"),
    })
    ArgusRejoin:ClientRecoveryFallback(codeText)

    local shutdownDelay = tonumber(ArgusRejoin.ShutdownDelay) or 0
    if shutdownDelay > 0 then
        task.delay(shutdownDelay, function()
            if ArgusRejoin.Running then
                log("shutdown fallback after disconnect", "error_code=" .. codeText)
                pcall(game.Shutdown, game)
            end
        end)
    end
end

table.insert(ArgusRejoin.Connections, GuiService.ErrorMessageChanged:Connect(function()
    reportDisconnect("GuiService.ErrorMessageChanged")
end))

pcall(function()
    table.insert(ArgusRejoin.Connections, TeleportService.TeleportInitFailed:Connect(function(player, result, message, placeId)
        if player and player ~= LocalPlayer then
            return
        end
        log("teleport failed", safeString(result), safeString(message))
        ArgusRejoin:PostAsync("teleport_error", {
            reason_key = "lua_teleport_error",
            message = safeString(message),
            detail = ("Teleport failed: %s"):format(safeString(result)),
            place_id = safeString(placeId or game.PlaceId),
        })
    end))
end)

pcall(function()
    table.insert(ArgusRejoin.Connections, LocalPlayer.OnTeleport:Connect(function(state)
        ArgusRejoin:PostAsync("teleport_state", {
            reason_key = "lua_teleport_state",
            detail = safeString(state),
        })
    end))
end)

task.spawn(function()
    if not game:IsLoaded() then
        game.Loaded:Wait()
    end
    reportLoaded()
end)

task.spawn(function()
    while ArgusRejoin.Running do
        reportDisconnect("poll")
        task.wait(0.5)
    end
end)

G.ArgusRejoin = ArgusRejoin
log("ready", "version=" .. safeString(ArgusRejoin.Version), "manual command: ArgusRejoin:Rejoin()")
return ArgusRejoin
