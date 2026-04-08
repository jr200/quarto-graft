-- Attach a site-wide build timestamp to every page.
function Meta(meta)
  meta.site_build = os.date("%Y-%m-%d %H:%M %Z")
  return meta
end

-- For graft pages, inject a script that rewrites the "View Source" / "Edit"
-- link so it points to the correct graft branch instead of the trunk branch.
function Pandoc(doc)
  local graft_branch = doc.meta["_graft-branch"]
  local graft_source = doc.meta["_graft-source-path"]
  if not graft_branch or not graft_source then
    return nil
  end

  local branch = pandoc.utils.stringify(graft_branch)
  local source_path = pandoc.utils.stringify(graft_source)

  -- Escape for safe embedding in a JS string literal
  branch = branch:gsub("\\", "\\\\"):gsub('"', '\\"')
  source_path = source_path:gsub("\\", "\\\\"):gsub('"', '\\"')

  local script = '<script>\n'
    .. 'document.addEventListener("DOMContentLoaded", function() {\n'
    .. '  var gb = "' .. branch .. '";\n'
    .. '  var gs = "' .. source_path .. '";\n'
    .. '  document.querySelectorAll("a[href]").forEach(function(a) {\n'
    .. '    var h = a.getAttribute("href");\n'
    .. '    if (h.indexOf("/dist/") === -1) return;\n'
    .. '    var f = h.replace(/^(.*?)\\/(blob|edit)\\/.*$/, "$1/$2/" + gb + "/" + gs);\n'
    .. '    if (f !== h) a.setAttribute("href", f);\n'
    .. '  });\n'
    .. '});\n'
    .. '</script>'

  table.insert(doc.blocks, pandoc.RawBlock("html", script))
  return doc
end
