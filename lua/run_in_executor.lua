local function cronusGlobal(name, fallback)
    local ok, value = pcall(function()
        if getgenv then
            local env = getgenv()
            if env and env[name] ~= nil then
                return env[name]
            end
        end
        if _G and _G[name] ~= nil then
            return _G[name]
        end
        return fallback
    end)
    if ok and value ~= nil then
        return value
    end
    return fallback
end

local CRONUS_HOST = tostring(cronusGlobal("CRONUS_HOST", "127.0.0.1"))
local CRONUS_PORT = tonumber(cronusGlobal("CRONUS_PORT", 7777)) or 7777
local CRONUS_ACCOUNT = tostring(cronusGlobal("CRONUS_ACCOUNT", ""))

local Request =
    (syn and syn.request)
    or (http and http.request)
    or http_request
    or request
local Load = loadstring or load

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

local function failDownload(reason)
    logWarn(reason or "Rejoin helper failed to load")
    return nil
end

local function encode(value)
    value = tostring(value or "")
    value = value:gsub("\n", "\r\n")
    value = value:gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
    return value
end

local function selectedPlayer()
    local ok, players = pcall(function()
        return game:GetService("Players")
    end)
    return ok and players and players.LocalPlayer or nil
end

local function selectedAccount()
    if tostring(CRONUS_ACCOUNT or "") ~= "" then
        return CRONUS_ACCOUNT
    end
    local player = selectedPlayer()
    return player and tostring(player.Name or "") or ""
end

local function selectedUserId()
    local player = selectedPlayer()
    return player and tostring(player.UserId or "") or ""
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

local function queueOnTeleport(sourceCode)
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
            local ok, err = pcall(provider, sourceCode)
            if ok then
                log("Rejoin helper restored")
                return true
            end
            logWarn("Executor does not support auto-run")
        end
    end
    logWarn("Executor does not support auto-run")
    return false
end

local account = selectedAccount()
local url = ("http://%s:%s/api/lua/rejoin-helper?bootstrap=1&account=%s&username=%s&user_id=%s&pid=%s"):format(
    CRONUS_HOST,
    tostring(CRONUS_PORT),
    encode(account),
    encode(account),
    encode(selectedUserId()),
    encode(getProcessId())
)

local source = nil
local statusCode = nil
if Request then
    log("Loading rejoin helper...")
    local response = Request({
        Method = "GET",
        Url = url,
        Headers = {
            ["User-Agent"] = "CronusRejoinLoader/1.0",
        },
    })
    statusCode = response and (response.StatusCode or response.status_code or response.Status or response.status)
    source = response and (response.Body or response.body or response.Data or response.data)
elseif game.HttpGet then
    log("Loading rejoin helper...")
    source = game:HttpGet(url)
end

if type(source) ~= "string" or #source <= 0 then
    return failDownload("Rejoin helper failed to load")
end
if type(Load) ~= "function" then
    return failDownload("Rejoin helper failed to load")
end
if source:sub(1, 1) == "{" then
    return nil
end
if not source:find("CronusRejoin", 1, true) then
    return failDownload("Rejoin helper failed to load")
end

if not source:find("CronusRejoin:QueueOnTeleport", 1, true) then
    queueOnTeleport(source)
end

local fn, err = Load(source)
if not fn then
    return failDownload("Rejoin helper failed to load")
end
log("Rejoin helper loaded")
return fn()
