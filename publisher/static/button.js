/* olpub — nút Publish chèn vào trang editor Overleaf (qua Nginx sub_filter, kèm nonce CSP).
   Không phụ thuộc DOM của Overleaf: chỉ gắn một phần tử nổi vào <body>, giao diện nằm trong Shadow DOM. */
(function () {
  'use strict'
  var m = location.pathname.match(/^\/project\/([0-9a-f]{24})$/)
  if (!m || window.__olpubLoaded) return
  window.__olpubLoaded = true
  var pid = m[1]
  var API = '/_pub/api/project/' + pid

  var host = document.createElement('div')
  host.id = 'olpub-host'
  host.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483000'
  var root = host.attachShadow({ mode: 'closed' })
  root.innerHTML =
    '<style>' +
    ':host{all:initial}' +
    '*{box-sizing:border-box;font:13px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}' +
    '.pill{border:1px solid #c9d1d9;background:#fff;color:#1f2328;border-radius:999px;padding:6px 12px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.12)}' +
    '.pill.on{background:#138a36;color:#fff;border-color:#138a36}' +
    '.panel{position:absolute;left:0;bottom:44px;width:340px;background:#fff;color:#1f2328;border:1px solid #d0d7de;border-radius:10px;padding:14px;box-shadow:0 8px 28px rgba(0,0,0,.18)}' +
    '.panel[hidden]{display:none}' +
    'h3{margin:0 0 8px;font-weight:600;font-size:14px}' +
    '.row{display:flex;gap:6px;margin:8px 0;align-items:center}' +
    'input[type=text]{flex:1;min-width:0;padding:5px 7px;border:1px solid #d0d7de;border-radius:6px}' +
    'button{padding:5px 10px;border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;cursor:pointer}' +
    'button.primary{background:#138a36;border-color:#138a36;color:#fff}' +
    'button.danger{color:#cf222e}' +
    'button.danger.armed{background:#cf222e;color:#fff;border-color:#cf222e}' +
    'label{display:flex;gap:6px;align-items:center;margin:4px 0;cursor:pointer}' +
    '.muted{color:#656d76;font-size:12px}' +
    '.err{color:#cf222e;font-size:12px;margin:6px 0;white-space:pre-wrap}' +
    '.links a{color:#0969da;margin-right:10px;font-size:12px}' +
    '.sep{border-top:1px solid #eaeef2;margin:10px 0}' +
    '</style>' +
    '<button class="pill" id="pill">🌐 Publish</button>' +
    '<div class="panel" id="panel" hidden></div>'

  var pill = root.getElementById('pill')
  var panel = root.getElementById('panel')
  var state = null
  var timer = null

  function call(method, body) {
    return fetch(API, {
      method: method,
      credentials: 'same-origin',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-OLPub': '1' } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return { error: 'HTTP ' + r.status } }).then(function (j) {
        if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status)
        return j
      })
    })
  }

  function el(tag, attrs, text) {
    var e = document.createElement(tag)
    for (var k in attrs || {}) {
      if (k === 'on') for (var ev in attrs.on) e.addEventListener(ev, attrs.on[ev])
      else if (k in e && k !== 'list') e[k] = attrs[k]
      else e.setAttribute(k, attrs[k])
    }
    if (text != null) e.textContent = text
    return e
  }

  function act(body) {
    panel.querySelectorAll('button').forEach(function (b) { b.disabled = true })
    return call('POST', body).then(render).catch(function (e) { render(state, e.message) })
  }

  function render(s, error) {
    state = s
    pill.textContent = s && s.published ? '🌐 Đã publish' : '🌐 Publish'
    pill.className = 'pill' + (s && s.published ? ' on' : '')
    panel.textContent = ''
    if (!s) { panel.appendChild(el('div', { className: 'muted' }, 'Đang tải…')); return }

    if (!s.published) {
      panel.appendChild(el('h3', null, 'Publish project này'))
      panel.appendChild(el('div', { className: 'muted' },
        'Ai có link đều xem được PDF. Bản publish tự cập nhật mỗi lần bạn Recompile.'))
      var slug = el('input', { type: 'text', placeholder: 'tên link (để trống = ngẫu nhiên)', value: s.slug || '' })
      panel.appendChild(el('div', { className: 'row' }, null)).appendChild(slug)
      var row = panel.appendChild(el('div', { className: 'row' }))
      row.appendChild(el('button', { className: 'primary', on: { click: function () {
        act({ action: 'publish', slug: slug.value.trim(), mode: 'live' })
      } } }, 'Publish'))
    } else {
      panel.appendChild(el('h3', null, s.title || 'Đã publish'))
      var r1 = panel.appendChild(el('div', { className: 'row' }))
      var link = el('input', { type: 'text', value: s.url, readOnly: true })
      r1.appendChild(link)
      r1.appendChild(el('button', { on: { click: function (ev) {
        link.select()
        var b = ev.target
        ;(navigator.clipboard ? navigator.clipboard.writeText(s.url) : Promise.reject())
          .catch(function () { document.execCommand && document.execCommand('copy') })
          .then(function () { b.textContent = 'Đã chép'; setTimeout(function () { b.textContent = 'Chép' }, 1500) })
      } } }, 'Chép'))
      var links = panel.appendChild(el('div', { className: 'links' }))
      links.appendChild(el('a', { href: s.url, target: '_blank', rel: 'noopener' }, 'Mở trang'))
      links.appendChild(el('a', { href: s.pdf_url, target: '_blank', rel: 'noopener' }, '.pdf'))
      links.appendChild(el('a', { href: s.txt_url, target: '_blank', rel: 'noopener' }, '.txt'))
      if (s.tex_url) links.appendChild(el('a', { href: s.tex_url, target: '_blank', rel: 'noopener' }, '.tex'))

      panel.appendChild(el('div', { className: 'muted' }, s.waiting
        ? (s.has_pdf ? 'Đang lấy bản biên dịch mới…' : 'Chờ bản biên dịch — hãy bấm Recompile.')
        : 'Cập nhật lần cuối: ' + s.updated_text))
      panel.appendChild(el('div', { className: 'sep' }))

      function radio(value, text) {
        var l = el('label')
        l.appendChild(el('input', { type: 'radio', name: 'mode', checked: s.mode === value, on: { change: function () {
          act({ action: 'set_mode', mode: value })
        } } }))
        l.appendChild(document.createTextNode(text))
        return l
      }
      panel.appendChild(radio('live', 'Tự cập nhật theo mỗi lần Recompile'))
      var pinned = panel.appendChild(radio('pinned', 'Chốt phiên bản hiện tại'))
      if (s.mode === 'pinned') {
        pinned.appendChild(el('button', { style: 'margin-left:auto', on: { click: function () {
          act({ action: 'refresh' })
        } } }, 'Cập nhật bản chốt'))
      }
      function check(field, action, text) {
        var l = el('label')
        l.appendChild(el('input', { type: 'checkbox', checked: !!s[field], on: { change: function (ev) {
          act({ action: action, enabled: ev.target.checked })
        } } }))
        l.appendChild(document.createTextNode(text))
        return l
      }
      panel.appendChild(check('include_source', 'set_source', 'Kèm mã LaTeX (.tex, đã xóa comment)'))
      panel.appendChild(check('indexable', 'set_indexable', 'Cho phép Google/Bing đưa vào kết quả tìm kiếm'))

      panel.appendChild(el('div', { className: 'sep' }))
      var r2 = panel.appendChild(el('div', { className: 'row' }))
      var slug2 = el('input', { type: 'text', value: s.slug })
      r2.appendChild(slug2)
      r2.appendChild(el('button', { on: { click: function () {
        if (slug2.value.trim() !== s.slug) act({ action: 'set_slug', slug: slug2.value.trim() })
      } } }, 'Đổi tên link'))
      var un = el('button', { className: 'danger', on: { click: function () {
        if (!un.classList.contains('armed')) {
          un.classList.add('armed'); un.textContent = 'Bấm lần nữa để gỡ'
          setTimeout(function () { un.classList.remove('armed'); un.textContent = 'Gỡ publish' }, 4000)
          return
        }
        act({ action: 'unpublish' })
      } } }, 'Gỡ publish')
      panel.appendChild(el('div', { className: 'row' })).appendChild(un)
    }
    if (error || s.last_error) {
      panel.appendChild(el('div', { className: 'err' }, error || ('Lần lấy bản mới bị từ chối: ' + s.last_error)))
    }
  }

  function refresh() {
    return call('GET').then(function (s) { render(s) }).catch(function (e) { render(state, e.message) })
  }

  pill.addEventListener('click', function () {
    panel.hidden = !panel.hidden
    clearInterval(timer)
    if (!panel.hidden) { refresh(); timer = setInterval(refresh, 5000) }
  })

  document.body.appendChild(host)
  refresh()
})()
