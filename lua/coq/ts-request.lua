(function(...)
  COQ.treesitter_start = function(buf, syntax)
    coq.validate {buf = {buf, "number"}, syntax = {syntax, "string"}}
    if vim.treesitter and vim.treesitter.start and vim.treesitter.language then
      local lang = vim.treesitter.language.get_lang(syntax)
      if lang then
        vim.treesitter.start(buf, lang)
      end
    end
  end

  local kind = function(node)
    if node:named() then
      return node:type()
    else
      return ""
    end
  end

  local payload = function(buf, node, type)
    if not node:missing() and not node:has_error() then
      local parent = node:parent()
      local grandparent = parent and parent:parent() or nil
      local lo, _, hi, _ = node:range()
      return {
        text = vim.treesitter.get_node_text(node, buf),
        range = {lo, hi},
        kind = type,
        parent = parent and
          {
            text = vim.treesitter.get_node_text(parent, buf),
            kind = kind(parent)
          } or
          nil,
        grandparent = grandparent and
          {
            text = vim.treesitter.get_node_text(grandparent, buf),
            kind = kind(grandparent)
          } or
          nil
      }
    end
  end

  local ts_query =
    vim.fn.has("nvim-0.8") and
    (vim.treesitter.query.get or vim.treesitter.query.get_query) or
    vim.treesitter.get_query

  local iter_nodes = function(buf, lo, hi)
    return coroutine.wrap(
      function()
        local go, parser = pcall(vim.treesitter.get_parser)
        if go and parser then
          local query = ts_query(parser:lang(), "highlights")
          if query then
            for _, tree in pairs(parser:parse()) do
              for capture, node in query:iter_captures(tree:root(), buf, lo, hi) do
                local pl = payload(buf, node, query.captures[capture])
                if pl and pl.kind ~= "comment" then
                  coroutine.yield(pl)
                end
              end
            end
          end
        end
      end
    )
  end

  COQ.ts_req = function(session)
    vim.schedule(
      function()
        local t1 = vim.loop.now()
        local win = vim.api.nvim_get_current_win()
        local buf = vim.api.nvim_win_get_buf(win)
        local height = vim.api.nvim_win_get_height(win)
        local filetype = vim.api.nvim_buf_get_option(buf, "filetype")
        local filename = vim.api.nvim_buf_get_name(buf)

        local lines = vim.api.nvim_buf_line_count(buf)
        local row, col = unpack(vim.api.nvim_win_get_cursor(win))
        row = row - 1
        local lo, hi =
          math.max(0, row - height),
          math.min(lines, row + height + 1)

        local acc = {}
        for payload in iter_nodes(buf, lo, hi) do
          table.insert(acc, payload)
        end
        local t2 = vim.loop.now()
        COQ.Ts_notify(
          session,
          buf,
          lo,
          hi,
          filetype,
          filename,
          acc,
          (t2 - t1) / 1000
        )
      end
    )
  end
end)(...)
