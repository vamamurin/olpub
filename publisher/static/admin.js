/* olpub — trang quản lý dự phòng tại overleaf.tapama.cloud/_pub/
   Dùng khi nút trong editor không hiện (ví dụ sau một bản cập nhật Overleaf). */
(function () {
  'use strict'
  var app = document.getElementById('app')

  function call(method, url, body) {
    return fetch(url, {
      method: method, credentials: 'same-origin',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-OLPub': '1' } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j })
    })
  }
  function el(tag, attrs, text) {
    var e = document.createElement(tag)
    for (var k in attrs || {}) {
      if (k === 'on') for (var ev in attrs.on) e.addEventListener(ev, attrs.on[ev])
      else e[k] = attrs[k]
    }
    if (text != null) e.textContent = text
    return e
  }

  function load(msg) {
    call('GET', '/_pub/api/list').then(function (d) {
      app.textContent = ''
      app.appendChild(el('h1', null, 'Publish từ Overleaf'))
      app.appendChild(el('p', { className: 'muted' }, 'Đăng nhập với: ' + d.user +
        '. Cách thường dùng: mở project trong Overleaf và bấm nút 🌐 ở góc trái dưới.'))
      if (msg) app.appendChild(el('p', { className: 'err' }, msg))

      var f = app.appendChild(el('div', { className: 'row' }))
      var inp = f.appendChild(el('input', { type: 'text', placeholder: 'Dán link project Overleaf (…/project/<id>)' }))
      f.appendChild(el('button', { className: 'primary', on: { click: function () {
        var m = inp.value.match(/([0-9a-f]{24})/)
        if (!m) return load('Không tìm thấy project ID (24 ký tự hex) trong link.')
        call('POST', '/_pub/api/project/' + m[1], { action: 'publish', mode: 'live' })
          .then(function () { load() }).catch(function (e) { load(e.message) })
      } } }, 'Publish'))

      var t = app.appendChild(el('table'))
      var hr = t.appendChild(el('tr'))
      ;['Tài liệu', 'Link', 'Chế độ', 'Cập nhật', ''].forEach(function (h) { hr.appendChild(el('th', null, h)) })
      d.items.forEach(function (s) {
        var tr = t.appendChild(el('tr', { className: s.published ? '' : 'off' }))
        var c1 = tr.appendChild(el('td'))
        c1.appendChild(el('a', { href: '/project/' + s.project_id }, s.title || s.project_id))
        if (s.last_error) c1.appendChild(el('div', { className: 'err' }, s.last_error))
        var c2 = tr.appendChild(el('td'))
        if (s.published) c2.appendChild(el('a', { href: s.url, target: '_blank', rel: 'noopener' }, s.url))
        else c2.textContent = '(đã gỡ) ' + s.slug
        tr.appendChild(el('td', null, s.published ? (s.mode === 'live' ? 'Tự cập nhật' : 'Đã chốt') : '—'))
        tr.appendChild(el('td', null, s.published ? (s.waiting ? 'Đang chờ bản biên dịch' : s.updated_text) : '—'))
        var c5 = tr.appendChild(el('td'))
        c5.appendChild(el('button', { on: { click: function () {
          call('POST', '/_pub/api/project/' + s.project_id, { action: s.published ? 'unpublish' : 'publish', mode: s.mode })
            .then(function () { load() }).catch(function (e) { load(e.message) })
        } } }, s.published ? 'Gỡ' : 'Publish lại'))
      })
    }).catch(function (e) { app.textContent = 'Lỗi: ' + e.message })
  }
  load()
})()
