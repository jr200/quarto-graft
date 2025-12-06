-- Attach a site-wide build timestamp to every page.
function Meta(meta)
  meta.site_build = os.date("%Y-%m-%d %H:%M %Z")
  return meta
end
