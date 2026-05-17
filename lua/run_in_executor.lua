local CRONUS_HOST = "127.0.0.1"
local CRONUS_PORT = 7777
local CRONUS_ACCOUNT = ""

local Request =
    (syn and syn.request)
    or (http and http.request)
    or http_request
    or request
local Load = loadstring or load

local function log(...)
    local parts = {}
    for _, value in ipairs({ ... }) do
        table.insert(parts, tostring(value))
    end
    local line = "[CronusRejoinLoader] " .. table.concat(parts, " ")
    if rconsoleprint then
        pcall(rconsoleprint, line .. "\n")
    end
    if print then
        pcall(print, line)
    end
end

local function preview(value)
    value = tostring(value or "")
    value = value:gsub("[\r\n]+", " ")
    if #value > 180 then
        return value:sub(1, 180) .. "..."
    end
    return value
end

local function failDownload(reason, statusCode, body)
    local suffix = ""
    if statusCode then
        suffix = suffix .. " status=" .. tostring(statusCode)
    end
    local bodyPreview = preview(body)
    if bodyPreview ~= "" then
        suffix = suffix .. " body=" .. bodyPreview
    end
    error(reason .. suffix, 2)
end

local function encode(value)
    value = tostring(value or "")
    value = value:gsub("\n", "\r\n")
    value = value:gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
    return value
end

local function queueOnTeleport(sourceCode)
    local providers = {
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
                log("helper queued for teleport")
                return true
            end
            log("queue_on_teleport failed", err)
        end
    end
    log("queue_on_teleport unavailable")
    return false
end

local url = ("http://%s:%s/api/lua/rejoin-helper?account=%s"):format(
    CRONUS_HOST,
    tostring(CRONUS_PORT),
    encode(CRONUS_ACCOUNT)
)

local source = nil
local statusCode = nil
if Request then
    log("downloading", url)
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
    log("downloading with game:HttpGet", url)
    source = game:HttpGet(url)
end

if type(source) ~= "string" or #source <= 0 then
    failDownload("Cronus Lua helper download failed", statusCode, source)
end
assert(type(Load) == "function", "executor does not expose loadstring/load")
if source:sub(1, 1) == "{" then
    failDownload("Cronus returned JSON instead of Lua. Restart Cronus Launcher or check the port.", statusCode, source)
end
if not source:find("ArgusRejoin", 1, true) then
    failDownload("Downloaded text is not the Cronus rejoin monitor", statusCode, source)
end

if not source:find("ArgusRejoin:QueueOnTeleport", 1, true) then
    queueOnTeleport(source)
end

local fn, err = Load(source)
assert(fn, err)
log("helper compiled")
return fn()
