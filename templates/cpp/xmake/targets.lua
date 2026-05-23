target("targets")
    set_kind("phony")
    set_default(false)
    on_run(function (target)
        import("core.project.project")
        local names = {}
        for name, _ in pairs(project.targets()) do
            if name ~= "targets" then
                table.insert(names, name)
            end
        end
        table.sort(names)
        for _, name in ipairs(names) do
            print(name)
        end
    end)
