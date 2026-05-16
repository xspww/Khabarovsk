local ARGUS_HOST = "127.0.0.1"
local ARGUS_PORT = 7777
local ARGUS_ACCOUNT = ""

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
    local line = "[ArgusAccountLoader] " .. table.concat(parts, " ")
    if rconsoleprint then
        pcall(rconsoleprint, line .. "\n")
    end
    if print then
        pcall(print, line)
    end
end

local function encode(value)
    value = tostring(value or "")
    value = value:gsub("\n", "\r\n")
    value = value:gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
    return value
end

local url = ("http://%s:%s/api/lua/account-module?account=%s"):format(
    ARGUS_HOST,
    tostring(ARGUS_PORT),
    encode(ARGUS_ACCOUNT)
)

local source = nil
if Request then
    log("downloading", url)
    local response = Request({
        Method = "GET",
        Url = url,
        Headers = {
            ["User-Agent"] = "ArgusAccountLoader/1.0",
        },
    })
    source = response and (response.Body or response.body or response.Data or response.data)
elseif game.HttpGet then
    log("downloading with game:HttpGet", url)
    source = game:HttpGet(url)
end

assert(type(source) == "string" and #source > 0, "Argus account module download failed")
assert(type(Load) == "function", "executor does not expose loadstring/load")
assert(source:sub(1, 1) ~= "{", "Argus returned JSON instead of Lua. Restart Argus or check the port.")
assert(source:find("ArgusAccount", 1, true), "Downloaded text is not the Argus account module")

local fn, err = Load(source)
assert(fn, err)
log("module compiled")
return fn()
