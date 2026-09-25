local function env_value(key, fallback)
    local ok, value = pcall(function()
        if getgenv then
            local env = getgenv()
            if env and env[key] ~= nil then return env[key] end
        end
        if _G and _G[key] ~= nil then return _G[key] end
        return fallback
    end)
    return ok and value ~= nil and value or fallback
end

local host = tostring(env_value("CRONUS_HOST", "127.0.0.1"))
local port = tonumber(env_value("CRONUS_PORT", 7777)) or 7777
local configured_account = tostring(env_value("CRONUS_ACCOUNT", ""))
local request = syn and syn.request or http and http.request or http_request or request
local load_source = loadstring or load

local function log_line(message, warning)
    local line = "[Cronus] " .. tostring(message or "")
    if rconsoleprint then pcall(rconsoleprint, line .. "\n") end
    if warning and warn then
        pcall(warn, line)
    elseif print then
        pcall(print, line)
    end
end

local function fail(message)
    log_line(message or "Rejoin helper failed to load", true)
    return nil
end

local function url_encode(value)
    value = tostring(value or ""):gsub("\n", "\r\n")
    return value:gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
end

local function local_player()
    local ok, players = pcall(function()
        return game:GetService("Players")
    end)
    if not ok or not players then return nil end
    local started = os.clock()
    while not players.LocalPlayer and os.clock() - started < 15 do
        task.wait()
    end
    return players.LocalPlayer
end

local function account_name(player)
    if configured_account ~= "" then return configured_account end
    return player and tostring(player.Name or "") or ""
end

local function user_id(player)
    return player and tostring(player.UserId or "") or ""
end

local function process_id()
    local candidates = {
        rawget(_G, "getprocessid"),
        rawget(_G, "get_process_id"),
        rawget(_G, "getpid"),
        rawget(_G, "get_pid"),
    }
    for _, getter in ipairs(candidates) do
        if type(getter) == "function" then
            local ok, value = pcall(getter)
            local pid = tonumber(value)
            if ok and pid and pid > 0 then return tostring(math.floor(pid)) end
        end
    end
    return ""
end

local player = local_player()
local account = account_name(player)
local uid = user_id(player)
local pid = process_id()
if account == "" and uid == "" and pid == "" then
    return fail("Rejoin helper failed to load: LocalPlayer identity unavailable")
end

local helper_url = ("http://%s:%s/api/lua/rejoin-helper?bootstrap=1&account=%s&username=%s&user_id=%s&pid=%s"):
    format(host, tostring(port), url_encode(account), url_encode(account), url_encode(uid), url_encode(pid))

local source
if request then
    log_line("Loading rejoin helper...")
    local response = request({
        Method = "GET",
        Url = helper_url,
        Headers = { ["User-Agent"] = "CronusRejoinLoader/1.0" },
    })
    source = response and (response.Body or response.body or response.Data or response.data)
elseif game.HttpGet then
    log_line("Loading rejoin helper...")
    source = game:HttpGet(helper_url)
end

if type(source) ~= "string" or #source <= 0 then
    return fail("Rejoin helper failed to load")
end
if type(load_source) ~= "function" then
    return fail("Rejoin helper failed to load")
end
if source:sub(1, 1) == "{" then
    return fail("Rejoin helper rejected: " .. source:sub(1, 180))
end
if not source:find("CronusRejoin", 1, true) then
    return fail("Rejoin helper failed to load")
end

log_line("Helper source bytes: " .. #source)
local fn, err = load_source(source)
if not fn then
    return fail("Rejoin helper failed to load: " .. tostring(err))
end
log_line("Rejoin helper loaded")
local ok, result = pcall(fn)
if not ok then
    log_line("Rejoin helper crashed: " .. tostring(result), true)
    return nil
end
log_line("Rejoin helper returned: " .. type(result))
return result
