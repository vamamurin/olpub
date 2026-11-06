/* olpub — nút Publish chèn vào trang editor Overleaf (qua Nginx sub_filter, kèm nonce CSP).
   Nút nằm trong rail trái, ngay trên dấu ? và dấu cài đặt, mượn cỡ/màu của chúng.
   Không tìm được rail thì rơi về nút nổi góc trái dưới. UI nằm trong Shadow DOM.

   File nằm trên VM 1007 (/opt/olpub/static/button.js). Nginx VM 100 ghim mã băm SHA-384
   của nó (SRI, trong /etc/nginx/conf.d/olpub.conf): file bị sửa mà mã băm chưa cập nhật
   thì trình duyệt TỪ CHỐI chạy — nút biến mất, không có code lạ nào chạy được.
   Sửa file này xong: tính lại mã băm và chạy `sudo olpub-sri set <mã băm>` trên VM 100.
   Tùy chọn: window.__olpubNonce = "…" — chỉ cần khi CSP của Overleaf có style-src 'nonce-…'
   API: GET/POST /_pub/api/project/<projectId>
*/
(function () {
  'use strict'

  /* ---------- 0. chỉ chạy trong trang editor của một project ---------- */
  var m = location.pathname.match(/^\/project\/([0-9a-f]{24})$/)
  if (!m || window.__olpubLoaded) return
  window.__olpubLoaded = true
  var API = '/_pub/api/project/' + m[1]
  var PANEL_W = 300, TIMEOUT = 12000, UNIT = 32
  var READ_ORIGIN = 'https://read.tapama.cloud'   // link trong panel chỉ được trỏ về đây
  var NONCE_RE = /^[A-Za-z0-9+\/=_-]{8,128}$/
  var rAF = window.requestAnimationFrame || function (f) { setTimeout(f, 16) }

  /* ---------- 1. host + shadow root ---------- */
  var host = document.createElement('div')
  host.id = 'olpub-host'
  host.style.cssText = 'position:fixed;left:4px;bottom:96px;z-index:1030;' +
                       'width:' + UNIT + 'px;height:' + UNIT + 'px;visibility:hidden'
  var root = host.attachShadow({ mode: 'closed' })

  // Font và độ rộng đưa vào qua biến CSS (CSSOM) — không ghép chuỗi vào HTML/CSS,
  // nên giá trị lạ (vd. tên font chứa "</style>") không thể phá ra thành thẻ HTML.
  try { host.style.setProperty('--ol-font', getComputedStyle(document.body).fontFamily || '') } catch (e) {}
  host.style.setProperty('--panel-w', PANEL_W + 'px')

  var styleEl = document.createElement('style')
  var nonce = window.__olpubNonce
  if (typeof nonce === 'string' && NONCE_RE.test(nonce)) styleEl.nonce = nonce
  styleEl.textContent = `
:host{all:initial}
:host{font-family:var(--ol-font,system-ui,sans-serif);font-size:13px;line-height:1.45;display:block}
:host{--ol-green:#138a07;--fg:#1f2328;--muted:#6a737d;--bg:#fff;--hover:#f2f3f5;
      --bd:#d0d7de;--bd-soft:#eaeef2;--r:4px;--rail-fg:#c3c9d0;--rail-r:6px;
      --rail-hover:rgba(128,128,128,.22)}
:host([data-theme=dark]){--fg:#e8e8e8;--muted:#9aa0a6;--bg:#22252a;--hover:#2b2f35;
      --bd:#3a3f45;--bd-soft:#31353a}
*{box-sizing:border-box;font:inherit;line-height:1.45}
.rail{display:flex;align-items:center;justify-content:center;width:100%;height:100%;margin:0;
      padding:0;border:0;border-radius:var(--rail-r);background:transparent;color:var(--rail-fg);
      cursor:pointer;position:relative;box-shadow:none;transition:background-color .12s,color .12s}
.rail:hover{background:var(--rail-hover)}
.rail:focus-visible{outline:2px solid var(--rail-fg);outline-offset:-2px}
.rail svg{display:block;width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;
      stroke-linecap:round;stroke-linejoin:round}
.panel{position:fixed;left:8px;top:8px;width:var(--panel-w,300px);max-height:calc(100vh - 16px);
      overflow:auto;overscroll-behavior:contain;background:var(--bg);color:var(--fg);
      border:1px solid var(--bd);border-radius:var(--r);padding:12px;
      box-shadow:0 6px 22px rgba(0,0,0,.22);text-align:left;z-index:1}
.panel[hidden]{display:none}
h3{margin:0 0 6px;font-size:13px;font-weight:600}
.row{display:flex;gap:6px;margin:8px 0;align-items:center}
input[type=text]{flex:1;min-width:0;height:28px;padding:0 8px;color:var(--fg);background:var(--bg);
      border:1px solid var(--bd);border-radius:var(--r)}
button{height:28px;padding:0 10px;border:1px solid var(--bd);border-radius:var(--r);
      background:var(--bg);color:var(--fg);cursor:pointer}
button:hover{background:var(--hover)}
button:disabled{opacity:.55;cursor:default}
button.primary{background:var(--ol-green);border-color:var(--ol-green);color:#fff}
button.danger{color:#c4271a;border-color:var(--bd)}
button.danger.armed{background:#c4271a;border-color:#c4271a;color:#fff}
label{display:flex;gap:6px;align-items:center;margin:4px 0;cursor:pointer}
input[type=radio],input[type=checkbox]{accent-color:var(--ol-green);margin:0}
.muted{color:var(--muted);font-size:12px}
.err{color:#c4271a;font-size:12px;margin:6px 0;white-space:pre-wrap}
.links a{color:var(--ol-green);margin-right:10px;font-size:12px;text-decoration:none}
.links a:hover{text-decoration:underline}
.sep{border-top:1px solid var(--bd-soft);margin:10px 0}
`
  root.appendChild(styleEl)

  // Phần markup là chuỗi tĩnh, không chứa dữ liệu nào từ trang hay từ máy chủ
  root.appendChild(document.createRange().createContextualFragment(`<button class="rail" id="pill" type="button" title="Publish" aria-label="Publish">
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/>
    <path d="M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5s-1.2 5.9-3.6 8.5c-2.4-2.6-3.6-5.4-3.6-8.5S9.6 6.1 12 3.5z"/>
  </svg>
</button>
<div class="panel" id="panel" hidden></div>`))

  var pill = root.querySelector('#pill')
  var panel = root.querySelector('#panel')
  if (!pill || !panel) return

  var state = null, timer = null
  var typing = false          // đang gõ trong panel → không render đè
  var lookRef = null          // phần tử rail mà nút bám theo (chính là phần tử chèn trước nó)
  var sized = false           // đã đo được cỡ thật chưa (chưa thì ẩn nút)

  /* ---------- 2. API ---------- */
  function call(method, body) {
    var opt = {
      method: method,
      credentials: 'same-origin',
      redirect: 'manual',             // Authelia hết phiên trả 302 sang auth.* — không đi theo
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-OLPub': '1' } : {},
      body: body ? JSON.stringify(body) : undefined
    }
    var to = null
    if (window.AbortController) {
      var ctrl = new AbortController()
      opt.signal = ctrl.signal
      to = setTimeout(function () { ctrl.abort() }, TIMEOUT)
    }
    return fetch(API, opt).then(function (r) {
      if (r.type === 'opaqueredirect' || (r.status >= 300 && r.status < 400)) {
        throw new Error('Phiên đăng nhập Authelia đã hết — tải lại trang để đăng nhập lại')
      }
      var ct = (r.headers && r.headers.get ? r.headers.get('content-type') : '') || ''
      if (ct.indexOf('json') < 0) {
        throw new Error(r.status === 200
          ? 'Máy chủ trả về HTML thay vì JSON — phiên đăng nhập có thể đã hết, tải lại trang'
          : 'HTTP ' + r.status)
      }
      return r.json().catch(function () { return { error: 'HTTP ' + r.status } })
        .then(function (j) {
          if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status)
          return j
        })
    }).then(function (j) {
      if (to) clearTimeout(to)
      return j
    }, function (e) {
      if (to) clearTimeout(to)
      if (e && e.name === 'AbortError') throw new Error('Hết thời gian chờ ' + (TIMEOUT / 1000) + 's')
      if (e instanceof TypeError) throw new Error('Không kết nối được tới máy chủ publish')
      throw e
    })
  }

  /* ---------- 3. theme cho panel ---------- */
  function detectTheme() {
    var n = document.body, p = null
    while (n && n.nodeType === 1) {
      var q = String(getComputedStyle(n).backgroundColor || '').match(/[\d.]+/g)
      if (q && (q.length < 4 || parseFloat(q[3]) > 0)) { p = q; break }
      n = n.parentElement
    }
    host.dataset.theme = (!p || (0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]) >= 128)
      ? 'light' : 'dark'
  }

  /* ---------- 4. tìm và bám hình dạng của mục rail ---------- */
  function findRef() {
    var all = document.querySelectorAll('a, button, [role="button"]')
    var helps = [], sett = [], best = null
    for (var i = 0; i < all.length; i++) {
      var e = all[i]
      if (e === host || host.contains(e) || !e.offsetParent) continue
      var r = e.getBoundingClientRect()
      var measured = r && (r.width || r.height || r.left || r.top)
      if (measured && !(r.left < 120 && r.width <= 80)) continue     // phải nằm ở rail trái
      var sig = [e.getAttribute('aria-label'), e.getAttribute('title'), e.getAttribute('href'),
                 e.getAttribute('data-testid'), e.getAttribute('data-cy'), e.textContent].join(' ')
      if (/help|trợ giúp/i.test(sig)) helps.push(e)
      else if (/setting|cài đặt|preference/i.test(sig)) sett.push(e)
    }
    // ưu tiên dấu ? (Help); trong mỗi nhóm lấy mục nằm thấp nhất
    var list = helps.length ? helps : sett
    for (var j = 0; j < list.length; j++) {
      if (!best || list[j].getBoundingClientRect().top > best.getBoundingClientRect().top) best = list[j]
    }
    if (!best) return null
    // mục rail bọc trong <li>/<div> thì bám cả wrapper, để cỡ khớp đúng tầng chèn
    var par = best.parentElement
    if (par && /^(LI|DIV)$/.test(par.tagName) && par.parentElement &&
        /^(UL|OL|NAV)$/.test(par.parentElement.tagName)) return par
    return best
  }
  // đo cỡ + màu của ref, gán cho host. Trả về true khi đã đo được cỡ thật.
  function adopt(ref) {
    if (!ref) return sized
    var cs = null
    try { cs = getComputedStyle(ref) } catch (e) { return sized }
    if (!cs) return sized
    var w = Math.round(ref.offsetWidth || ref.getBoundingClientRect().width || 0)
    var h = Math.round(ref.offsetHeight || ref.getBoundingClientRect().height || 0)
    if (w >= 12 && h >= 12) {
      if (host.style.width !== w + 'px') host.style.width = w + 'px'
      if (host.style.height !== h + 'px') host.style.height = h + 'px'
      sized = true
    } else if (!sized) {
      host.style.width = UNIT + 'px'
      host.style.height = UNIT + 'px'
    }
    if (cs.marginTop) host.style.marginTop = cs.marginTop
    if (cs.marginBottom) host.style.marginBottom = cs.marginBottom
    host.style.setProperty('--rail-fg', cs.color || '#c3c9d0')
    host.style.setProperty('--rail-r', cs.borderRadius && cs.borderRadius !== '0px'
      ? cs.borderRadius : '6px')
    return sized
  }
  function corner() {                       // không thấy rail: nút nổi góc trái dưới
    host.style.position = 'fixed'
    host.style.left = '4px'
    host.style.bottom = '96px'
    host.style.zIndex = '1030'
    host.style.marginTop = '0'
    host.style.marginBottom = '0'
    host.style.setProperty('--rail-fg', '#c3c9d0')
    sized = true
  }
  function dock() {
    var ref = (lookRef && lookRef.isConnected) ? lookRef : findRef()
    if (!ref || !ref.parentElement) { lookRef = null; return false }
    lookRef = ref
    var par = ref.parentElement
    if (host.parentElement !== par || host.nextElementSibling !== ref) {
      host.style.position = 'relative'
      host.style.left = 'auto'
      host.style.bottom = 'auto'
      host.style.zIndex = 'auto'
      par.insertBefore(host, ref)
    }
    adopt(ref)                              // đúng chỗ rồi thì chỉ cập nhật cỡ/màu, không di chuyển
    rAF(function () { adopt(ref) })          // layout xong đo lại một lần
    return true
  }
  function modalOpen() {
    var n = document.querySelectorAll('.modal.show, .modal.in, [role="dialog"]')
    for (var i = 0; i < n.length; i++) {
      var s = getComputedStyle(n[i])
      if (s.display !== 'none' && s.visibility !== 'hidden' && n[i].offsetParent) return true
    }
    return false
  }
  function mount() {
    if (!host.isConnected) document.body.appendChild(host)
    if (!dock()) corner()
    host.style.visibility = (!sized || modalOpen()) ? 'hidden' : ''
  }
  function tune() {                         // bám lại cỡ/màu + đặt lại panel nếu đang mở
    mount()
    if (!panel.hidden) placePanel()
  }

  /* ---------- 5. đặt panel ---------- */
  function placePanel() {
    if (!host.isConnected || panel.hidden) return
    var r = host.getBoundingClientRect()
    var vw = innerWidth || 1024, vh = innerHeight || 768, gap = 8, edge = 8
    var left = r.right + gap
    if (left + PANEL_W > vw - edge) left = Math.max(edge, vw - PANEL_W - edge)
    panel.style.maxHeight = 'none'
    var need = panel.scrollHeight || panel.offsetHeight || 320       // chiều cao thật của nội dung
    var h = Math.min(need, vh - edge * 2)
    var below = vh - (r.bottom + gap) - edge
    var above = r.top - gap - edge
    var top
    if (need <= below) top = r.top - edge                            // đủ chỗ phía dưới icon
    else if (above > below) top = r.bottom + gap - h                 // mở ngược lên trên
    else top = Math.max(edge, Math.min(r.top - edge, vh - h - edge))
    top = Math.max(edge, Math.min(top, vh - 96))
    panel.style.left = Math.round(left) + 'px'
    panel.style.top = Math.round(top) + 'px'
    panel.style.bottom = 'auto'
    panel.style.maxHeight = Math.max(160, vh - top - edge) + 'px'    // phần dư cuộn bên trong
  }
  function placeSoon() {
    rAF(function () { placePanel(); rAF(placePanel) })
  }

  /* ---------- 6. helper DOM ---------- */
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
  // Chỉ nhận link https về đúng trang publish; mọi giá trị khác (javascript:, data:, host lạ) bị bỏ.
  function safeHref(u) {
    try {
      var x = new URL(String(u), location.href)
      if (x.protocol === 'https:' && x.origin === READ_ORIGIN) return x.href
    } catch (e) {}
    return null
  }
  function row() { return panel.appendChild(el('div', { className: 'row' })) }
  function note(text) { panel.appendChild(el('div', { className: 'muted' }, text)) }
  function sep() { panel.appendChild(el('div', { className: 'sep' })) }
  function btn(text, cls, fn, attrs) {
    var a = attrs || {}
    a.type = 'button'
    if (cls) a.className = cls
    a.on = { click: fn }
    return el('button', a, text)
  }
  function toggle(type, checked, onchange) {
    return el('input', { type: type, checked: checked, on: { change: onchange } })
  }

  /* ---------- 7. render ---------- */
  // Không có chấm/nhãn trạng thái trên nút: chỉ tooltip đổi, giao diện giữ im lặng.
  function updatePill(s) {
    var on = !!(s && s.published)
    pill.dataset.published = on ? '1' : '0'
    var label = on ? 'Đã publish — bấm để xem link'
                   : 'Publish — đưa project thành link công khai'
    if (pill.title !== label) pill.title = label
    if (pill.getAttribute('aria-label') !== label) pill.setAttribute('aria-label', label)
  }
  function act(body) {
    panel.querySelectorAll('button').forEach(function (b) { b.disabled = true })
    return call('POST', body).then(render).catch(function (e) { render(state, e.message) })
  }

  function render(s, error) {
    state = s
    updatePill(s)
    panel.textContent = ''
    if (!s) {
      note('Đang tải…')
      placeSoon()
      return
    }

    if (!s.published) {
      panel.appendChild(el('h3', null, 'Publish project này'))
      note('Ai có link đều xem được PDF. Bản publish tự cập nhật mỗi lần bạn Recompile.')
      var slug = el('input', { type: 'text', value: s.slug || '',
        placeholder: 'tên link (để trống = ngẫu nhiên)' })
      row().appendChild(slug)
      row().appendChild(btn('Publish', 'primary', function () {
        act({ action: 'publish', slug: slug.value.trim(), mode: 'live' })
      }))
    } else {
      panel.appendChild(el('h3', null, s.title || 'Đã publish'))

      var link = el('input', { type: 'text', value: s.url, readOnly: true })
      var r1 = row()
      r1.appendChild(link)
      r1.appendChild(btn('Chép', null, function (ev) {
        link.select()
        var b = ev.target
        ;(navigator.clipboard ? navigator.clipboard.writeText(s.url) : Promise.reject())
          .then(function () { return true }, function () {
            return !!(document.execCommand && document.execCommand('copy'))
          })
          .then(function (ok) {
            b.textContent = ok ? 'Đã chép' : 'Chép tay (Ctrl+C)'
            setTimeout(function () { b.textContent = 'Chép' }, 1500)
          })
      }))

      var links = panel.appendChild(el('div', { className: 'links' }))
      ;[['Mở trang', s.url], ['.pdf', s.pdf_url], ['.txt', s.txt_url], ['.tex', s.tex_url]]
        .forEach(function (p) {
          var href = p[1] && safeHref(p[1])
          if (!href) return
          links.appendChild(el('a', { href: href, target: '_blank', rel: 'noopener noreferrer' }, p[0]))
        })

      note(s.waiting
        ? (s.has_pdf ? 'Đang lấy bản biên dịch mới…' : 'Chờ bản biên dịch — hãy bấm Recompile.')
        : 'Cập nhật lần cuối: ' + s.updated_text)
      sep()

      ;[['live', 'Tự cập nhật theo mỗi lần Recompile'],
        ['pinned', 'Chốt phiên bản hiện tại']].forEach(function (o) {
        var l = el('label')
        l.appendChild(toggle('radio', s.mode === o[0], function () {
          act({ action: 'set_mode', mode: o[0] })
        }))
        l.firstChild.name = 'mode'
        l.appendChild(document.createTextNode(o[1]))
        if (o[0] === 'pinned' && s.mode === 'pinned') {
          l.appendChild(btn('Cập nhật bản chốt', null, function () {
            act({ action: 'refresh' })
          }, { style: 'margin-left:auto' }))
        }
        panel.appendChild(l)
      })

      ;[['include_source', 'set_source', 'Kèm mã LaTeX (.tex, đã xóa comment)'],
        ['indexable', 'set_indexable', 'Cho phép Google/Bing đưa vào kết quả tìm kiếm']]
        .forEach(function (o) {
          var l = el('label')
          l.appendChild(toggle('checkbox', !!s[o[0]], function (ev) {
            act({ action: o[1], enabled: ev.target.checked })
          }))
          l.appendChild(document.createTextNode(o[2]))
          panel.appendChild(l)
        })

      sep()
      var slug2 = el('input', { type: 'text', value: s.slug })
      var r2 = row()
      r2.appendChild(slug2)
      r2.appendChild(btn('Đổi tên link', null, function () {
        if (slug2.value.trim() !== s.slug) act({ action: 'set_slug', slug: slug2.value.trim() })
      }))

      var un = btn('Gỡ publish', 'danger', function () {
        if (!un.classList.contains('armed')) {           // bấm 2 lần mới gỡ
          un.classList.add('armed')
          un.textContent = 'Bấm lần nữa để gỡ'
          setTimeout(function () {
            un.classList.remove('armed')
            un.textContent = 'Gỡ publish'
          }, 4000)
          return
        }
        act({ action: 'unpublish' })
      })
      row().appendChild(un)
    }

    if (error || s.last_error) {
      panel.appendChild(el('div', { className: 'err' },
        error || ('Lần lấy bản mới bị từ chối: ' + s.last_error)))
    }
    placeSoon()                       // nội dung đổi chiều cao → đặt lại vị trí sau khi layout
  }

  /* ---------- 8. vòng cập nhật ---------- */
  function refresh() {
    if (document.hidden) return Promise.resolve()
    detectTheme()
    mount()
    return call('GET').then(function (s) {
      if (!panel.hidden && typing) { state = s; updatePill(s); return }
      render(s)
    }).catch(function (e) {
      if (!typing) render(state, e.message)
    })
  }
  function stopPoll() {
    clearInterval(timer)
    timer = null
  }
  function openPanel() {
    panel.hidden = false
    placePanel()
    refresh()
    placeSoon()
    stopPoll()
    timer = setInterval(refresh, 5000)
  }
  function closePanel() {
    panel.hidden = true
    stopPoll()
  }

  pill.addEventListener('click', function () {
    if (panel.hidden) openPanel()
    else closePanel()
  })
  root.addEventListener('focusin', function (e) {
    typing = !!(e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName))
  })
  root.addEventListener('focusout', function () { typing = false })
  root.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !panel.hidden) closePanel()
  })
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPoll()
    else if (!panel.hidden) { stopPoll(); timer = setInterval(refresh, 5000) }
  })
  window.addEventListener('resize', placePanel)
  window.addEventListener('scroll', function () { if (!panel.hidden) placePanel() }, true)

  /* ---------- 9. chạy ---------- */
  // theme + layout của Overleaf chốt SAU khi trang load → đo lại vài lần cho khớp màu và cỡ
  ;[0, 60, 250, 700, 1600, 3500].forEach(function (d) { setTimeout(tune, d) })
  setTimeout(function () { sized = true; mount() }, 2500)   // lỡ không đo được gì thì vẫn hiện

  new MutationObserver(function () { if (!host.isConnected) mount() })
    .observe(document.body, { childList: true, subtree: true })
  try {
    new MutationObserver(tune).observe(document.documentElement, {
      attributes: true, attributeFilter: ['class', 'style', 'data-theme']
    })
  } catch (e) {}
  if (window.ResizeObserver) {
    try {
      var ro = new ResizeObserver(tune), watched = null
      setInterval(function () {                    // rail đổi cỡ (theme/zoom/font) → bám lại
        if (lookRef && lookRef !== watched && lookRef.isConnected) {
          ro.disconnect()
          ro.observe(lookRef)
          watched = lookRef
        }
      }, 1000)
    } catch (e) {}
  }

  mount()
  refresh()
})()
