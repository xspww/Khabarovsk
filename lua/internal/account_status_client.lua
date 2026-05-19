-- CronusAccount.lua
-- Safe drop-in client for Cronus Launcher local Lua API.
-- It sends account/runtime signals only. It never exposes Roblox cookies or CSRF tokens.

local G = getgenv and getgenv() or _G

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")

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

local Account = {}
Account.__index = Account

Account.Config = {
    Host = __CRONUS_HOST__,
    Port = __CRONUS_PORT__,
    Token = __CRONUS_TOKEN__,
    Account = __CRONUS_ACCOUNT__,
    SessionId = __CRONUS_SESSION_ID__,
    LaunchNonce = __CRONUS_LAUNCH_NONCE__,
    Version = "account-1.0.0",
}

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
    local privateServerId = ""
    local privateServerOwnerId = ""
    pcall(function()
        privateServerId = safeString(game.PrivateServerId)
    end)
    pcall(function()
        privateServerOwnerId = safeString(game.PrivateServerOwnerId)
    end)

    local ownerNumber = tonumber(privateServerOwnerId) or 0
    local isPrivate = privateServerId ~= "" or ownerNumber > 0
    return {
        private_server_id = privateServerId,
        private_server_owner_id = privateServerOwnerId,
        is_vip_server = isPrivate and "true" or "false",
        server_type = isPrivate and "VIP" or "PUBLIC",
    }
end

local function decodeJson(body)
    local text = safeString(body)
    if text == "" then
        return { ok = true }
    end
    local ok, decoded = pcall(function()
        return HttpService:JSONDecode(text)
    end)
    if ok and type(decoded) == "table" then
        return decoded
    end
    return { ok = true, body = text }
end

function Account.SetKey(key)
    Account.Config.Token = safeString(key)
end

function Account.SetSession(sessionId, launchNonce)
    Account.Config.SessionId = safeString(sessionId)
    Account.Config.LaunchNonce = safeString(launchNonce)
end

function Account.SetEndpoint(host, port)
    Account.Config.Host = safeString(host)
    Account.Config.Port = tonumber(port) or Account.Config.Port
end

function Account.new(username, options)
    options = options or {}
    local self = setmetatable({}, Account)
    self.Username = safeString(username or options.username or Account.Config.Account)
    self.Host = safeString(options.host or Account.Config.Host)
    self.Port = tonumber(options.port or Account.Config.Port) or 7777
    self.Token = safeString(options.token or Account.Config.Token)
    self.SessionId = safeString(options.session_id or Account.Config.SessionId)
    self.LaunchNonce = safeString(options.launch_nonce or Account.Config.LaunchNonce)
    self.EventCounter = 0
    self.Timeout = tonumber(options.timeout or 5) or 5
    return self
end

function Account:_requireToken()
    if safeString(self.Token) == "" then
        return nil, "CronusAccount token missing; load this module from /api/lua/account-module"
    end
    return true, nil
end

function Account:Endpoint()
    local host = safeString(self.Host)
    if host == "" then
        host = "127.0.0.1"
    end
    local port = tostring(tonumber(self.Port) or 7777)
    return "http://" .. host .. ":" .. port .. "/api/lua/rejoin-event"
end

function Account:EndpointWithToken()
    return self:Endpoint() .. "?cronus_token=" .. urlEncode(self.Token)
end

function Account:NextEventId(eventName)
    self.EventCounter = (tonumber(self.EventCounter) or 0) + 1
    return table.concat({
        safeString(self.SessionId),
        safeString(self.LaunchNonce),
        safeString(eventName),
        safeString(os.time()),
        safeString(self.EventCounter),
        safeString(math.random(100000, 999999)),
    }, ":")
end

function Account:Payload(eventName, fields)
    fields = fields or {}
    local playerName = safeString(LocalPlayer and LocalPlayer.Name or "")
    local configured = safeString(self.Username)
    local serverInfo = getServerInfo()
    local payload = {
        event = safeString(eventName),
        account = playerName ~= "" and playerName or configured,
        username = playerName ~= "" and playerName or configured,
        configured_account = configured,
        account_hint = configured,
        session_id = safeString(self.SessionId),
        launch_nonce = safeString(self.LaunchNonce),
        event_id = self:NextEventId(eventName),
        user_id = safeString(LocalPlayer and LocalPlayer.UserId or ""),
        pid = getProcessId(),
        place_id = safeString(game.PlaceId),
        job_id = safeString(game.JobId),
        universe_id = safeString(game.GameId),
        private_server_id = serverInfo.private_server_id,
        private_server_owner_id = serverInfo.private_server_owner_id,
        is_vip_server = serverInfo.is_vip_server,
        server_type = serverInfo.server_type,
        executor = identifyexecutor and safeString(identifyexecutor()) or "",
        helper_version = safeString(Account.Config.Version),
        token = safeString(self.Token),
        cronus_token = safeString(self.Token),
        api_token = safeString(self.Token),
        _cronus_token = safeString(self.Token),
        ts = safeString(os.time()),
    }

    for key, value in pairs(fields) do
        payload[key] = safeString(value)
    end

    return payload
end

function Account:QueryEndpoint(payload)
    local url = self:EndpointWithToken()
    local keys = {
        "event",
        "account",
        "username",
        "configured_account",
        "account_hint",
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
        "error_code",
        "message",
        "reason_key",
        "detail",
        "description",
        "executor",
        "helper_version",
        "visual_disconnect",
        "evidence_source",
        "detection_source",
        "ts",
    }

    for _, key in ipairs(keys) do
        local text = safeString(payload and payload[key])
        if text ~= "" then
            url = url .. "&" .. urlEncode(key) .. "=" .. urlEncode(text)
        end
    end

    return url
end

function Account:_request(method, url, body)
    local tokenOk, tokenErr = self:_requireToken()
    if not tokenOk then
        return nil, tokenErr
    end

    local headers = {
        ["Content-Type"] = "application/json",
        ["X-Cronus-Token"] = self.Token,
        ["User-Agent"] = "CronusAccountLua/1.0",
    }

    if Request then
        local req = {
            Method = method,
            Url = url,
            Headers = headers,
            headers = headers,
            Timeout = self.Timeout,
        }
        if body then
            req.Body = body
            req.body = body
            req.Data = body
            req.data = body
        end

        local ok, response = pcall(Request, req)
        if not ok then
            return nil, "request threw: " .. safeString(response)
        end
        if type(response) ~= "table" then
            return nil, "request returned non-table"
        end

        local status = tonumber(response.StatusCode or response.Status) or 0
        local responseBody = safeString(response.Body or response.body or response.Data or response.data)
        if status < 200 or status >= 300 then
            return nil, string.format("HTTP %d %s: %s", status, safeString(response.StatusMessage or ""), responseBody)
        end
        return decodeJson(responseBody), nil
    end

    if method == "GET" and game.HttpGet then
        local ok, response = pcall(function()
            return game:HttpGet(url)
        end)
        if ok then
            return decodeJson(response), nil
        end
        return nil, "game:HttpGet threw: " .. safeString(response)
    end

    return nil, "executor HTTP request unavailable"
end

function Account:Send(eventName, fields)
    local payload = self:Payload(eventName, fields)
    local encodeOk, body = pcall(function()
        return HttpService:JSONEncode(payload)
    end)
    if not encodeOk then
        return nil, "json encode failed: " .. safeString(body)
    end

    local decoded, err = self:_request("POST", self:EndpointWithToken(), body)
    if decoded then
        return decoded, nil
    end

    local fallback, fallbackErr = self:_request("GET", self:QueryEndpoint(payload), nil)
    if fallback then
        return fallback, nil
    end
    return nil, fallbackErr or err
end

function Account:Loaded(detail)
    return self:Send("loaded", {
        reason_key = "lua_account_loaded",
        detail = detail or "CronusAccount module loaded",
    })
end

function Account:InGame(detail)
    return self:Send("in_game", {
        reason_key = "lua_account_in_game",
        detail = detail or "Lua reported in-game",
    })
end

function Account:Heartbeat(fields)
    fields = fields or {}
    fields.reason_key = fields.reason_key or "lua_account_heartbeat"
    return self:Send("heartbeat", fields)
end

function Account:Disconnected(errorCode, message)
    return self:Send("disconnect", {
        reason_key = "lua_account_disconnect",
        error_code = safeString(errorCode),
        message = safeString(message),
        detail = "Lua reported disconnect",
        visual_disconnect = "true",
        evidence_source = "cronus_account_module",
    })
end

function Account:TeleportError(message, placeId)
    return self:Send("teleport_error", {
        reason_key = "lua_account_teleport_error",
        message = safeString(message),
        place_id = safeString(placeId or game.PlaceId),
        teleport_state = "failed",
        evidence_source = "cronus_account_module",
        detail = "Lua reported teleport error",
    })
end

function Account:TeleportState(state)
    local stateText = safeString(state)
    return self:Send("teleport_state", {
        reason_key = "lua_account_teleport_state",
        detail = stateText,
        teleport_state = stateText,
        teleport_place_id = safeString(game.PlaceId),
        evidence_source = "cronus_account_module",
    })
end

function Account:RequestRejoin(reason)
    return self:Send("rejoin_requested", {
        reason_key = "lua_account_manual_rejoin",
        detail = safeString(reason or "Lua requested rejoin"),
    })
end

function Account:SetDescription(text)
    local description = safeString(text)
    return self:Send("description", {
        reason_key = "lua_account_description",
        description = description,
        detail = description,
    })
end

function Account:MarkFinished(description)
    local text = safeString(description)
    return self:Send("finished", {
        reason_key = "lua_account_finished",
        description = text,
        detail = text ~= "" and text or "Lua marked account finished",
    })
end

G.CronusAccount = Account
task.spawn(function()
    local ok, err = pcall(function()
        local client = Account.new(Account.Config.Account)
        client:Loaded("CronusAccount module loaded")
    end)
    if not ok and print then
        pcall(print, "[CronusAccount] loaded event failed: " .. safeString(err))
    end
end)
return Account
