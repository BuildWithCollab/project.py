includes("xmake/*.lua")

set_project("{{project.name}}")

add_rules("mode.release")
set_defaultmode("release")

set_languages("c++23")

set_warnings("all", "extra")
set_encodings("utf-8")
set_policy("build.c++.modules", true)

option("build_tests")
    set_default(true)
    set_showmenu(true)
    set_description("Build test targets")
option_end()

option("build_examples")
    set_default(true)
    set_showmenu(true)
    set_description("Build example binaries")
option_end()

if get_config("build_tests") then
    add_requires("catch2")
end

-- includes("lib/collab.platform/xmake.lua")

-- if get_config("build_examples") then
--     includes("examples/xmake.lua")
-- end
