-- xmake/workspace.lua — workspace-aware dependency resolution
--
-- Lets you depend on a package and, at build time, prefer a local sibling
-- folder copy of it over the registry version. Useful when developing
-- coordinated changes across multiple repos.
--
-- Usage in xmake.lua:
--   includes("xmake/workspace.lua")
--
--   -- Top level (like add_requires)
--   add_workspace_requires("collab-core", "collab-process")
--   add_workspace_requires("collab-core", { configs = { shared = true } })
--
--   -- Inside a target (like add_packages)
--   target("myapp")
--       add_workspace_packages("collab-core")
--       add_workspace_packages("collab-core", { public = true })
--
--   -- Inside a target — depend on a specific xmake target inside a
--   -- sibling folder whose shipped package name hasn't been decided yet.
--   -- Errors loudly if the folder isn't in the workspace; that error IS
--   -- the reminder to pick a shipped package name before pushing.
--   target("myapp")
--       add_workspace_deps("agentic:collab-pipes")
--       add_workspace_deps("agentic:collab-pipes", { public = true })
--
-- Environment variables:
--   XMAKE_WORKSPACE_PATHS    — semicolon (Windows) / colon (Mac/Linux)
--                              separated list of folders to search. For each
--                              package name, the first folder containing
--                              <folder>/<name>/xmake.lua wins.
--                              Example: "C:/Code/orgA;C:/Code/orgB"
--   XMAKE_WORKSPACE_PACKAGES — optional comma-separated allowlist. If set,
--                              only names in this list are eligible for
--                              workspace override; everything else goes
--                              straight to the registry even if a folder
--                              exists on the path.
--
-- Functions:
--   use_workspace_packages("name", ...) — in-code allowlist. Merges with
--                              XMAKE_WORKSPACE_PACKAGES. If the merged
--                              allowlist is non-empty, allowlist mode is
--                              active; if both sources are empty, every
--                              name is eligible.

if _xmake_workspace_loaded then return end
_xmake_workspace_loaded = true

local _sep = (os.host() == "windows") and ";" or ":"

-- If the last arg is a table, pop it as opts. Returns (names, opts).
local function _split_opts(args)
    local opts = {}
    if type(args[#args]) == "table" then
        opts = table.remove(args)
    end
    return args, opts
end

-- Packages marked workspace-eligible via use_workspace_packages()
local _explicit_allowlist = {}

function use_workspace_packages(...)
    for _, name in ipairs({...}) do
        _explicit_allowlist[name] = true
    end
end

-- Returns the merged allowlist set, or nil if no allowlist is active
-- (both sources empty → every name is eligible).
local function _allowlist()
    local set, any = {}, false

    for name, _ in pairs(_explicit_allowlist) do
        set[name] = true
        any = true
    end

    local env = os.getenv("XMAKE_WORKSPACE_PACKAGES")
    if env and #env > 0 then
        for name in env:gmatch("([^,]+)") do
            local trimmed = name:match("^%s*(.-)%s*$")
            if #trimmed > 0 then
                set[trimmed] = true
                any = true
            end
        end
    end

    if any then return set else return nil end
end

local function _eligible(name)
    local set = _allowlist()
    if set == nil then return true end
    return set[name] == true
end

-- Walk XMAKE_WORKSPACE_PATHS for <entry>/<name>/xmake.lua.
-- Returns the folder path on hit, nil on miss.
local function _find_workspace_folder(name)
    local paths_env = os.getenv("XMAKE_WORKSPACE_PATHS")
    if not paths_env or #paths_env == 0 then return nil end

    for entry in paths_env:gmatch("([^" .. _sep .. "]+)") do
        local trimmed = entry:match("^%s*(.-)%s*$")
        if #trimmed > 0 then
            local folder = path.join(trimmed, name)
            if os.isfile(path.join(folder, "xmake.lua")) then
                return folder
            end
        end
    end

    return nil
end

-- Top level — like add_requires, but a workspace folder wins if eligible.
function add_workspace_requires(...)
    local names, opts = _split_opts({...})
    for _, name in ipairs(names) do
        local folder = _eligible(name) and _find_workspace_folder(name) or nil
        if folder then
            includes(path.join(folder, "xmake.lua"))
        else
            add_requires(name, opts)
        end
    end
end

-- Inside a target — like add_packages, but uses add_deps if workspace-resolved.
function add_workspace_packages(...)
    local names, opts = _split_opts({...})
    for _, name in ipairs(names) do
        local folder = _eligible(name) and _find_workspace_folder(name) or nil
        if folder then
            add_deps(name, opts)
        else
            add_packages(name, opts)
        end
    end
end

-- Inside a target — depend on a specific xmake target inside a workspace
-- folder whose shipped package name hasn't been decided yet.
--
-- Syntax: "folder:target" (colon required).
--   - "folder" is the sibling folder name in the workspace
--   - "target" is the literal xmake target inside that folder's xmake.lua
--
-- If "folder" resolves to a workspace member → add_deps("target", opts).
-- Otherwise → raise with an instruction to either fix the workspace setup
-- or replace this call with add_workspace_packages("<shipped-name>") once
-- a shipped package name exists. The raise IS the feature.
function add_workspace_deps(...)
    local args, opts = _split_opts({...})
    for _, spec in ipairs(args) do
        local folder, target = spec:match("^([^:]+):([^:]+)$")
        if not folder then
            os.raise("add_workspace_deps requires \"folder:target\" syntax, got: \""
                .. tostring(spec) .. "\"")
        end
        local folder_path = _find_workspace_folder(folder)
        local eligible = _eligible(folder)
        if folder_path and eligible then
            add_deps(target, opts)
        else
            local reason
            if not folder_path then
                reason = "\"" .. folder .. "\" not found on XMAKE_WORKSPACE_PATHS"
            else
                reason = "\"" .. folder .. "\" exists on XMAKE_WORKSPACE_PATHS "
                      .. "but is excluded by the XMAKE_WORKSPACE_PACKAGES allowlist"
            end
            os.raise("add_workspace_deps(\"" .. spec .. "\") — " .. reason .. ".\n\n"
                .. "Either adjust the workspace setup, or replace this call with\n"
                .. "add_workspace_packages(\"<shipped-package-name>\") once you've\n"
                .. "decided on a shipped package name.")
        end
    end
end
