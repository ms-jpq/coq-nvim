(function(...)
  local make_filter = function(include_clients, client_names)
    vim.validate {
      include_clients = {include_clients, "boolean"},
      client_names = {client_names, "table"}
    }

    if #client_names <= 0 then
      return function()
        return true
      end
    else
      local acc = {}
      for _, client_name in ipairs(client_names) do
        vim.validate {client_name = {client_name, "string"}}
        acc[client_name] = true
      end

      return function(client_name)
        vim.validate {client_name = {client_name, "string"}}
        local includes = acc[client_name]
        local yes = (function()
          if include_clients then
            return includes
          else
            return not includes
          end
        end)()
        return yes
      end
    end
  end

  local cid = -1
  local acc = {}

  COQ.lsp_pull = function(client, uid, lo, hi)
    vim.validate {
      uid = {uid, "number"},
      lo = {lo, "number"},
      hi = {hi, "number"}
    }

    if uid > cid then
      return {}
    end

    local items = acc[client] or {}
    local a = {}
    for i = lo, hi do
      local item = items[i]
      if item then
        a[i - lo + 1] = item
      else
        break
      end
    end

    return a
  end

  local lsp_notify = function(payload)
    vim.validate {payload = {payload, "table"}}
    local client = payload.client
    local multipart = tonumber(payload.multipart)
    local name = payload.name
    local reply = payload.reply
    local uid = payload.uid
    vim.validate {
      multipart = {multipart, "number", true},
      name = {name, "string"},
      uid = {uid, "number"}
    }

    if multipart then
      if uid > cid then
        acc = {}
      end
      if type(reply) == "table" then
        if type(reply.items) == "table" then
          acc[client] = reply.items
          reply.items = {}
        else
          acc[client] = reply
          payload.reply = {}
        end
      end
    end
    cid = uid

    COQ.Lsp_notify(payload)
  end

  local req =
    (function()
    local current_sessions = {}
    local cancels = {}
    return function(name, multipart, session_id, clients, callback)
      vim.validate {clients = {clients, "table"}}
      local n_clients, client_map = unpack(clients)
      vim.validate {
        name = {name, "string"},
        session_id = {session_id, "number"},
        n_clients = {n_clients, "number"},
        client_map = {client_map, "table"},
        callback = {callback, "function"}
      }
      current_sessions[name] = session_id

      pcall(
        cancels[name] or function()
          end
      )

      local new_payload = function()
        return {
          client = vim.NIL,
          done = true,
          method = vim.NIL,
          multipart = multipart,
          name = name,
          reply = vim.NIL,
          offset_encoding = vim.NIL,
          uid = session_id
        }
      end

      local on_resp_old = function(err, method, resp, client_id)
        vim.validate {
          method = {method, "string", true}
        }

        local payload = new_payload()

        n_clients = n_clients - 1
        payload.method = method or vim.NIL
        local client = client_map[client_id]
        payload.client = client and client.name or vim.NIL
        payload.offset_encoding = client and client.offset_encoding or vim.NIL
        payload.done = n_clients == 0

        local current_session = current_sessions[name] or -2
        if current_session ~= session_id then
          payload.reply = vim.NIL
        else
          payload.reply = resp or vim.NIL
        end

        lsp_notify(payload)
      end

      local on_resp_new = function(err, resp, ctx)
        on_resp_old(err, ctx.method, resp, ctx.client_id)
      end

      local on_resp = function(...)
        if type(({...})[2]) ~= "string" then
          on_resp_new(...)
        else
          on_resp_old(...)
        end
      end

      if n_clients == 0 then
        lsp_notify(new_payload())
      else
        cancels[name] = callback(on_resp)
      end
    end
  end)()

  local get_clients = (function()
    if vim.lsp.get_clients then
      return function(bufnr)
        local clients = {}
        for _, client in pairs(vim.lsp.get_clients({bufnr = bufnr})) do
          clients[client.id] = client
        end
        return clients
      end
    else
      return vim.lsp.buf_get_clients
    end
  end)()
  local _ = nil

  (function()
    local lsp_clients = function(
      include_clients,
      client_names,
      buf,
      lsp_method,
      lsp_capability)
      local filter = make_filter(include_clients, client_names)

      vim.validate {
        buf = {buf, "number"},
        lsp_method = {lsp_method, "string"},
        lsp_capability = {lsp_capability, "string", true},
        filter = {filter, "function"}
      }

      local n_clients = 0
      local clients = {}

      for id, client in pairs(get_clients(buf)) do
        if
          filter(client.name) and
            client.supports_method(lsp_capability or lsp_method)
         then
          n_clients = n_clients + 1
          clients[id] = client
        end
      end

      return n_clients, clients
    end

    local lsp_request_all = function(
      clients,
      buf,
      lsp_method,
      make_params,
      handler)
      vim.validate {
        buf = {buf, "number"},
        make_params = {make_params, "function"},
        clients = {clients, "table"},
        lsp_method = {lsp_method, "string"},
        handler = {handler, "function"}
      }

      local cancels = {}
      local cancel_all = function()
        for _, cancel in ipairs(cancels) do
          cancel()
        end
      end

      for _, client in pairs(clients) do
        vim.validate {client = {client, "table"}}

        local request_params = make_params(client)

        local go, cancel_handle =
          client.request(lsp_method, request_params, handler, buf)
        if not go then
          handler(
            "<>FAILED<>",
            nil,
            {client_id = client.id, method = lsp_method}
          )
        else
          table.insert(
            cancels,
            function()
              client.cancel_request(cancel_handle)
            end
          )
        end
      end

      return cancel_all
    end

    local lsp_comp_base = function(
      lsp_method,
      lsp_capability,
      name,
      multipart,
      session_id,
      client_names,
      pos)
      vim.validate {
        lsp_method = {lsp_method, "string"},
        lsp_capability = {lsp_capability, "string"},
        client_names = {client_names, "table"},
        name = {name, "string"},
        pos = {pos, "table"},
        session_id = {session_id, "number"}
      }
      local row, col8, col16, col32 = unpack(pos)
      vim.validate {
        row = {row, "number"},
        col8 = {col8, "number"},
        col16 = {col16, "number"},
        col32 = {col32, "number"}
      }

      local buf = vim.api.nvim_get_current_buf()
      local n_clients, clients =
        lsp_clients(false, client_names, buf, lsp_method, lsp_capability)

      local make_params = function(client)
        local col = (function()
          if client.offset_encoding == "utf-16" then
            return col16
          elseif client.offset_encoding == "utf-8" then
            return col8
          else
            -- see -- coq/server/edit.py
            -- return col32
            return col8
          end
        end)()

        local position = {line = row, character = col}
        local text_doc = vim.lsp.util.make_text_document_params()
        return {
          position = position,
          textDocument = text_doc,
          context = {
            triggerKind = vim.lsp.protocol.CompletionTriggerKind.Invoked
          }
        }
      end

      req(
        name,
        multipart,
        session_id,
        {n_clients, clients},
        function(on_resp)
          return lsp_request_all(clients, buf, lsp_method, make_params, on_resp)
        end
      )
    end

    COQ.lsp_comp = function(name, multipart, session_id, client_names, pos)
      lsp_comp_base(
        "textDocument/completion",
        "completionProvider",
        name,
        multipart,
        session_id,
        client_names,
        pos
      )
    end

    COQ.lsp_inline_comp = function(
      name,
      multipart,
      session_id,
      client_names,
      pos)
      lsp_comp_base(
        "textDocument/inlineCompletion",
        "inlineCompletionProvider",
        name,
        multipart,
        session_id,
        client_names,
        pos
      )
    end

    COQ.lsp_resolve = function(name, multipart, session_id, client_names, item)
      vim.validate {
        name = {name, "string"},
        session_id = {session_id, "number"},
        client_names = {client_names, "table"},
        item = {item, "table"}
      }

      local buf = vim.api.nvim_get_current_buf()
      local lsp_method = "completionItem/resolve"
      local n_clients, clients =
        lsp_clients(true, client_names, buf, lsp_method)

      local make_params = function()
        return item
      end

      req(
        name,
        multipart,
        session_id,
        {n_clients, clients},
        function(on_resp)
          return lsp_request_all(clients, buf, lsp_method, make_params, on_resp)
        end
      )
    end

    COQ.lsp_command = function(name, multipart, session_id, client_names, cmd)
      vim.validate {cmd = {cmd, "table"}}
      vim.validate {
        name = {name, "string"},
        session_id = {session_id, "number"},
        client_names = {client_names, "table"},
        command = {cmd.command, "string"}
      }

      local buf = vim.api.nvim_get_current_buf()
      local lsp_method = "workspace/executeCommand"
      local n_clients, clients =
        lsp_clients(true, client_names, buf, lsp_method)

      local make_params = function()
        return cmd
      end

      req(
        name,
        multipart,
        session_id,
        {n_clients, clients},
        function(on_resp)
          return lsp_request_all(clients, buf, lsp_method, make_params, on_resp)
        end
      )
    end
  end)()

  local _ = nil

  (function()
    local freeze = function(name, is_list, original)
      vim.validate {
        name = {name, "string"},
        is_list = {is_list, "boolean"},
        original = {original, "table"}
      }

      local proxy =
        setmetatable(
        is_list and original or {},
        {
          __index = function(_, key)
            if original[key] == nil then
              error("NotImplementedError :: " .. name .. "->" .. key)
            else
              return original[key]
            end
          end,
          __newindex = function(_, key, val)
            error(
              "TypeError :: " ..
                vim.inspect {key, val} .. "->frozen<" .. name .. ">"
            )
          end
        }
      )
      return proxy
    end

    local lua_clients = function(key, include_clients, client_names)
      local filter = make_filter(include_clients, client_names)
      vim.validate {
        key = {key, "string"},
        filter = {filter, "function"}
      }

      local sources = COQsources or {}
      local names, fns = {}, {}

      if type(sources) == "table" then
        for id, source in pairs(sources) do
          if
            type(source) == "table" and type(source.name) == "string" and
              filter(source.name) and
              type(source[key]) == "function"
           then
            local offset_encoding = source.offset_encoding or "utf-8"
            vim.validate {
              offset_encoding = {offset_encoding, "string"}
            }
            names[id] = {
              name = source.name,
              offset_encoding = offset_encoding
            }
            table.insert(fns, {id, source[key]})
          end
        end
      end

      return names, fns
    end

    local lua_cancel = function()
      local acc = {}
      local cancel = function()
        for _, cont in ipairs(acc) do
          local go, err = pcall(cont)
          if not go then
            vim.api.nvim_err_writeln(err)
          end
        end
      end
      return acc, cancel
    end

    local lua_req = function(
      name,
      multipart,
      session_id,
      key,
      include_clients,
      client_names,
      method,
      args)
      vim.validate {
        args = {args, "table"},
        key = {key, "string"},
        method = {method, "string"},
        name = {name, "string"},
        session_id = {session_id, "number"}
      }

      local names, client_fns = lua_clients(key, include_clients, client_names)
      local cancels, cancel = lua_cancel()

      req(
        name,
        multipart,
        session_id,
        {#client_fns, names},
        function(on_resp)
          for _, spec in ipairs(client_fns) do
            local id, fn = unpack(spec)
            local go, maybe_cancel =
              pcall(
              fn,
              args,
              function(resp)
                on_resp(nil, method, resp, id)
              end
            )
            if go then
              if type(maybe_cancel) == "function" then
                table.insert(cancels, maybe_cancel)
              end
            else
              vim.api.nvim_err_writeln(maybe_cancel)
            end
          end
          return cancel
        end
      )
    end

    COQ.lsp_third_party = function(
      name,
      multipart,
      session_id,
      client_names,
      pos,
      line)
      local args =
        freeze(
        "coq_3p.args",
        false,
        {
          uid = session_id,
          pos = freeze("coq_3p.args.pos", true, pos),
          line = line
        }
      )

      lua_req(
        name,
        multipart,
        session_id,
        "fn",
        false,
        client_names,
        "< lua :: comp >",
        args
      )
    end

    COQ.lsp_inline_third_party = function(
      name,
      multipart,
      session_id,
      client_names,
      pos,
      line)
      local args =
        freeze(
        "coq_3p.args",
        false,
        {
          uid = session_id,
          pos = freeze("coq_3p.args.pos", true, pos),
          line = line
        }
      )

      lua_req(
        name,
        multipart,
        session_id,
        "ln",
        false,
        client_names,
        "< lua :: inline comp >",
        args
      )
    end

    COQ.lsp_third_party_resolve = function(
      name,
      multipart,
      session_id,
      client_names,
      item)
      local args =
        freeze(
        "coq_3p.args",
        false,
        {
          uid = session_id,
          item = item
        }
      )

      lua_req(
        name,
        multipart,
        session_id,
        "resolve",
        true,
        client_names,
        "< lua :: resolve >",
        args
      )
    end

    COQ.lsp_third_party_cmd = function(
      name,
      multipart,
      session_id,
      client_names,
      cmd)
      local args =
        freeze(
        "coq_3p.args",
        false,
        {
          uid = session_id,
          command = cmd.command,
          arguments = cmd.arguments
        }
      )

      lua_req(
        name,
        multipart,
        session_id,
        "exec",
        true,
        client_names,
        "< lua :: cmd >",
        args
      )
    end
  end)()
end)(...)
