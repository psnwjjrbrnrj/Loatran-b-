from flask import Flask, request, jsonify, render_template_string
import hashlib, hmac as hmac_lib, io, json, os, socket, tempfile, threading, time
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    pass

try:
    from PIL import Image as _PIL_Image
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# ── CONFIG ──────────────────────────────────────────────
COS_BUCKET = "aovcamp-h5-ugc-1254801811"
COS_REGION = "ap-singapore"
COS_HOST   = f"{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
CDN_BASE   = "https://kg-camp.mobagarena.com"
CDN_UGC    = "https://kg-camp-ugc.mobagarena.com"
API_BASE   = "https://kgvn-api.mobagarena.com"

PI_STICKER_ID = "182"
PI_STICKER_W  = 690.9890109890109
PI_STICKER_H  = 690.9890109890109
PI_STICKER_X  = -194.8712087912088
PI_STICKER_Y  = -85.4572357633227
PI_BG_ID      = "21"
PI_BG_PICURL  = CDN_BASE + "/manage/playerimage_official/iDzT817p.png"
PI_BG_W       = 320
PI_BG_H       = 503.99824175824176

FIXED_HEADERS = {
    "camp-source":        "AOV-CAMP",
    "msdk-gameid":        "1137",
    "camp-authtype":      "msdk",
    "areaid":             "1",
    "msdk-os":            "1",
    "logicworldid":       "1011",
    "aov-language":       "VN",
    "msdk-channelid":     "10",
    "aov-region":         "1137",
    "origin":             "https://kgvn-camp.mobagarena.com",
    "x-requested-with":   "com.garena.game.kgvn",
    "referer":            "https://kgvn-camp.mobagarena.com/",
    "accept":             "*/*",
    "accept-language":    "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 15; SM-A165F Build/AP3A.240905.015.A2; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.177 "
        "Mobile Safari/537.36 MSDK/5.36.000 mQQAppId/1105779914 "
        "mWXAppId/wx7a814e3ceeda8320 mGameId/1137 MSDKdeviceId/disable"
    ),
}

# ── HELPERS ─────────────────────────────────────────────
def _hmac_sha1(key, msg):
    return hmac_lib.new(key, msg.encode(), hashlib.sha1).hexdigest()

def build_cos_auth(sid, skey, method, pathname, clen):
    now = int(time.time())
    end = now + 86400
    kt  = f"{now};{end}"
    sk  = _hmac_sha1(skey.encode(), kt)
    hh  = f"content-length={clen}&host={COS_HOST}&x-cos-forbid-overwrite=true"
    hs  = f"{method.lower()}\n{pathname}\n\n{hh}\n"
    hhttp = hashlib.sha1(hs.encode()).hexdigest()
    s2s = f"sha1\n{kt}\n{hhttp}\n"
    sig = _hmac_sha1(sk.encode(), s2s)
    return (f"q-sign-algorithm=sha1&q-ak={sid}"
            f"&q-sign-time={kt}&q-key-time={kt}"
            f"&q-header-list=content-length;host;x-cos-forbid-overwrite&q-url-param-list="
            f"&q-signature={sig}")

def gen_traceparent():
    return f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01"

def make_session():
    s = requests.Session()
    r = Retry(total=3, backoff_factor=1.5, status_forcelist=[500,502,503,504],
              allowed_methods=["POST","PUT","GET"])
    a = HTTPAdapter(max_retries=r)
    s.mount("https://", a)
    return s

def parse_har(har_bytes):
    har = json.loads(har_bytes.decode("utf-8", errors="ignore"))
    auth_token = user_path = None
    for entry in har["log"]["entries"]:
        req = entry["request"]
        url = req["url"]
        if "kgvn-api.mobagarena.com" in url and not auth_token:
            hdrs = {h["name"].lower(): h["value"] for h in req.get("headers", [])}
            if "msdk-itopencodeparam" in hdrs:
                auth_token = hdrs["msdk-itopencodeparam"]
        if req["method"] == "PUT" and COS_HOST in url and not user_path:
            path  = url.split(COS_HOST)[1].split("?")[0]
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                user_path = "/" + "/".join(parts[:3]) + "/"
        if auth_token and user_path:
            break
    return auth_token, user_path

def api_post(session, endpoint, payload, auth_token):
    hdrs = dict(FIXED_HEADERS)
    hdrs["content-type"]          = "application/json"
    hdrs["msdk-itopencodeparam"]  = auth_token
    hdrs["traceparent"]           = gen_traceparent()
    for attempt in range(3):
        try:
            r = session.post(API_BASE + endpoint, json=payload, headers=hdrs, timeout=25)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 1 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return data
        except Exception as e:
            if attempt == 2:
                return {"code": -1, "msg": str(e)}
            time.sleep(2)
    return {"code": -1, "msg": "max retries"}

def cos_put(session, url, data, headers):
    for attempt in range(3):
        try:
            resp = session.put(url, data=data, headers=headers, timeout=60)
            if resp.status_code == 200:
                return True, ""
            if attempt < 2:
                time.sleep(2)
        except Exception as e:
            return False, str(e)
    return False, f"HTTP {resp.status_code}"

def build_pic_info(pic_info_raw, sticker_url):
    bg = pic_info_raw.get("bg", {})
    return {
        "bg": {
            "id":     bg.get("id",     PI_BG_ID),
            "picUrl": bg.get("picUrl", PI_BG_PICURL),
            "source": 1,
            "width":  bg.get("width",  PI_BG_W),
            "height": bg.get("height", PI_BG_H),
            "posX":   bg.get("posX",   0),
            "posY":   bg.get("posY",   0),
        },
        "stickerList": [{
            "id":     PI_STICKER_ID,
            "picUrl": sticker_url,
            "width":  PI_STICKER_W,
            "height": PI_STICKER_H,
            "posX":   PI_STICKER_X,
            "posY":   PI_STICKER_Y,
            "rotate": 0, "source": 1, "type": 1,
        }],
    }

def run_tool(har_bytes, img_bytes, img_ext, is_share, log):
    def L(msg): log.append(msg)

    # Parse HAR
    try:
        auth_token, user_path = parse_har(har_bytes)
    except Exception as e:
        L(f"❌ Lỗi đọc HAR: {e}"); return False

    if not auth_token:
        L("❌ Không tìm thấy token trong HAR (msdk-itopencodeparam)"); return False
    if not user_path:
        L("❌ Không tìm thấy user_path COS trong HAR"); return False

    L(f"✅ Token: {auth_token[:30]}...")
    L(f"✅ Path COS: {user_path}")

    session = make_session()

    # Get current picInfo
    L("⏳ Đang lấy cấu hình picInfo hiện tại...")
    r = api_post(session, "/api/game/poster/playerimage/getpostereditinfo", {}, auth_token)
    pic_info_raw = r.get("data", {}).get("picInfo", {}) if r.get("code") == 0 else {}
    if pic_info_raw:
        L("✅ Lấy picInfo thành công!")
    else:
        L("⚠️ Dùng cấu hình mặc định.")

    # Prepare image bytes
    png_bytes  = img_bytes
    anim_bytes = None
    anim_ext   = None

    if img_ext == "gif":
        if PILLOW_OK:
            try:
                gif = _PIL_Image.open(io.BytesIO(img_bytes))
                buf = io.BytesIO()
                gif.convert("RGBA").save(buf, format="PNG")
                png_bytes  = buf.getvalue()
                anim_bytes = img_bytes
                anim_ext   = "gif"
                L(f"✅ Đã xử lý GIF ({len(img_bytes):,} bytes)")
            except Exception as e:
                L(f"⚠️ Lỗi xử lý GIF: {e}")
        else:
            L("⚠️ Pillow chưa cài, GIF sẽ dùng như ảnh tĩnh")

    # createposter
    L("⏳ Đang khởi tạo poster...")
    r = api_post(session, "/api/game/poster/playerimage/createposter", {}, auth_token)
    if r.get("code") != 0:
        L(f"❌ Tạo poster thất bại: {r.get('msg', '')} (code={r.get('code')})"); return False
    pid = r["data"]["posterId"]
    L(f"✅ PosterID: {pid}")
    time.sleep(0.4)

    ck   = f"{user_path}0/1/{pid}.png"
    ck_l = f"{user_path}0/1/{pid}_large.png"

    # Get COS credentials
    def get_creds(file_name):
        rc = api_post(session, "/api/game/poster/getcoscredential",
                      {"scene": "PlayerimagePoster", "fileName": file_name}, auth_token)
        return rc.get("data") if rc.get("code") == 0 else None

    L("⏳ Đang lấy quyền hạn COS...")
    creds1 = get_creds(f"0/1/{pid}_large.png")
    if not creds1:
        L("❌ Không lấy được COS credentials"); return False
    creds2 = get_creds(f"0/1/{pid}.png") or creds1
    time.sleep(0.2)

    def mkhdr(key, buf, creds_in, ctype="image/png"):
        return {
            "Authorization":          build_cos_auth(
                creds_in["tmpSecretId"], creds_in["tmpSecretKey"], "PUT", key, len(buf)),
            "Content-Type":           ctype,
            "Content-Length":         str(len(buf)),
            "Host":                   COS_HOST,
            "x-cos-security-token":   creds_in["token"],
            "x-cos-forbid-overwrite": "true",
            "Origin":                 "https://kgvn-camp.mobagarena.com",
            "Referer":                "https://kgvn-camp.mobagarena.com/",
        }

    # Upload _large
    L(f"⏳ Đang upload ảnh _large ({len(png_bytes):,} bytes)...")
    ok1, e1 = cos_put(session, f"https://{COS_HOST}{ck_l}", png_bytes, mkhdr(ck_l, png_bytes, creds1))
    L(f"{'✅ Upload _large thành công!' if ok1 else f'⚠️ _large thất bại: {e1}'}")
    time.sleep(0.3)

    # Upload main
    L(f"⏳ Đang upload ảnh chính...")
    ok2, e2 = cos_put(session, f"https://{COS_HOST}{ck}", png_bytes, mkhdr(ck, png_bytes, creds2, "image/jpeg"))
    if not ok2:
        L(f"❌ Upload ảnh chính thất bại: {e2}"); return False
    L("✅ Upload ảnh chính thành công!")

    sticker_url = CDN_BASE + ck

    # Upload GIF animation if exists
    if anim_bytes and anim_ext:
        ck_a = f"{user_path}0/1/{pid}.{anim_ext}"
        L(f"⏳ Đang upload hiệu ứng động ({len(anim_bytes):,} bytes)...")
        ok_a, ea = cos_put(session, f"https://{COS_HOST}{ck_a}", anim_bytes,
                           mkhdr(ck_a, anim_bytes, creds1))
        if ok_a:
            sticker_url = CDN_BASE + ck_a
            L("✅ Upload GIF thành công!")
    time.sleep(0.4)

    # savepostereditinfo
    pi = build_pic_info(pic_info_raw, sticker_url)
    L("⏳ Đang lưu thông tin poster (savepostereditinfo)...")
    rs = api_post(session, "/api/game/poster/playerimage/savepostereditinfo",
                  {"picInfo": pi}, auth_token)
    L(f"{'✅' if rs.get('code') == 0 else '⚠️'} savepostereditinfo: code={rs.get('code')}")

    # saveposter
    L("⏳ Đang lưu poster (saveposter)...")
    rp = api_post(session, "/api/game/poster/playerimage/saveposter",
                  {"posterId": str(pid), "isApply": True, "isShare": is_share,
                   "picUrl": CDN_UGC + user_path, "picInfo": pi},
                  auth_token)

    if rp.get("code") == 0:
        L(f"🎉 HOÀN TẤT! PosterID={pid}")
        L(f"🖼️ URL: {sticker_url}")
        return True
    else:
        L(f"❌ saveposter thất bại: {rp.get('msg', '')} (code={rp.get('code')})"); return False


# ── ROUTES ──────────────────────────────────────────────
HTML = '''<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Load Tran Tool</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=Inter:wght@400;500&display=swap');

  :root {
    --bg:      #0a0c12;
    --surface: #111520;
    --border:  #1e2540;
    --accent:  #7c3aed;
    --glow:    #a855f7;
    --green:   #22c55e;
    --red:     #ef4444;
    --yellow:  #eab308;
    --text:    #e2e8f0;
    --muted:   #64748b;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 16px 60px;
  }

  /* animated bg grid */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: -1;
    background-image:
      linear-gradient(rgba(124,58,237,.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(124,58,237,.04) 1px, transparent 1px);
    background-size: 40px 40px;
  }

  header {
    text-align: center;
    margin-bottom: 32px;
  }
  .logo {
    font-family: 'Rajdhani', sans-serif;
    font-size: 2.4rem;
    font-weight: 700;
    letter-spacing: .08em;
    background: linear-gradient(135deg, #a855f7, #6366f1, #22d3ee);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: none;
  }
  .logo-sub {
    font-size: .8rem;
    color: var(--muted);
    letter-spacing: .15em;
    text-transform: uppercase;
    margin-top: 4px;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    width: 100%;
    max-width: 520px;
    margin-bottom: 16px;
  }
  .card-title {
    font-family: 'Rajdhani', sans-serif;
    font-size: 1rem;
    font-weight: 600;
    color: var(--glow);
    letter-spacing: .1em;
    text-transform: uppercase;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-title::before {
    content: '';
    display: block;
    width: 3px; height: 16px;
    background: var(--glow);
    border-radius: 2px;
  }

  .upload-zone {
    border: 2px dashed var(--border);
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s;
    position: relative;
  }
  .upload-zone:hover, .upload-zone.drag { border-color: var(--glow); background: rgba(168,85,247,.05); }
  .upload-zone input[type=file] { display: none; }
  .upload-icon { font-size: 2rem; margin-bottom: 8px; }
  .upload-label { font-size: .85rem; color: var(--muted); }
  .upload-label span { color: var(--glow); }
  .file-chosen { font-size: .8rem; color: var(--green); margin-top: 8px; font-weight: 500; }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }
  .toggle-row:last-child { border-bottom: none; }
  .toggle-label { font-size: .9rem; color: var(--text); }
  .toggle-desc  { font-size: .75rem; color: var(--muted); margin-top: 2px; }

  /* toggle switch */
  .switch { position: relative; display: inline-block; width: 44px; height: 24px; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; inset: 0;
    background: var(--border);
    border-radius: 24px;
    cursor: pointer;
    transition: background .2s;
  }
  .slider::before {
    content: '';
    position: absolute;
    width: 18px; height: 18px;
    left: 3px; top: 3px;
    background: #fff;
    border-radius: 50%;
    transition: transform .2s;
  }
  input:checked + .slider { background: var(--accent); }
  input:checked + .slider::before { transform: translateX(20px); }

  .btn {
    width: 100%;
    padding: 14px;
    border: none;
    border-radius: 8px;
    font-family: 'Rajdhani', sans-serif;
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: .08em;
    cursor: pointer;
    transition: opacity .2s, transform .1s;
    background: linear-gradient(135deg, var(--accent), #4f46e5);
    color: #fff;
    text-transform: uppercase;
  }
  .btn:hover:not(:disabled) { opacity: .9; transform: translateY(-1px); }
  .btn:active:not(:disabled) { transform: translateY(0); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }

  /* log panel */
  #log-panel {
    display: none;
    background: #080b10;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    font-family: 'Courier New', monospace;
    font-size: .78rem;
    line-height: 1.7;
    max-height: 320px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .log-line { padding: 1px 0; }
  .log-ok   { color: var(--green); }
  .log-err  { color: var(--red); }
  .log-warn { color: var(--yellow); }
  .log-info { color: #94a3b8; }

  .result-banner {
    display: none;
    border-radius: 8px;
    padding: 16px 20px;
    font-family: 'Rajdhani', sans-serif;
    font-size: 1.1rem;
    font-weight: 600;
    letter-spacing: .05em;
    text-align: center;
  }
  .result-banner.ok  { background: rgba(34,197,94,.12); border: 1px solid rgba(34,197,94,.3); color: var(--green); }
  .result-banner.err { background: rgba(239,68,68,.12);  border: 1px solid rgba(239,68,68,.3);  color: var(--red); }

  .spinner {
    display: none;
    width: 20px; height: 20px;
    border: 2px solid rgba(255,255,255,.2);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin .7s linear infinite;
    margin: 0 auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <div class="logo">⚡ LOAD TRAN TOOL</div>
  <div class="logo-sub">AOV Playerimage Mod · by Hoàng Phúc</div>
</header>

<!-- Upload HAR -->
<div class="card">
  <div class="card-title">File HAR</div>
  <div class="upload-zone" id="zone-har" onclick="document.getElementById('inp-har').click()">
    <div class="upload-icon">📂</div>
    <div class="upload-label">Nhấn để chọn <span>file .har</span> từ game</div>
    <div class="file-chosen" id="har-name"></div>
    <input type="file" id="inp-har" accept=".har">
  </div>
</div>

<!-- Upload Image -->
<div class="card">
  <div class="card-title">Ảnh / GIF</div>
  <div class="upload-zone" id="zone-img" onclick="document.getElementById('inp-img').click()">
    <div class="upload-icon">🖼️</div>
    <div class="upload-label">Nhấn để chọn <span>ảnh hoặc GIF</span></div>
    <div class="file-chosen" id="img-name"></div>
    <input type="file" id="inp-img" accept=".jpg,.jpeg,.png,.webp,.gif">
  </div>
</div>

<!-- Options -->
<div class="card">
  <div class="card-title">Tuỳ chọn</div>
  <div class="toggle-row">
    <div>
      <div class="toggle-label">Đăng lên Quảng Trường</div>
      <div class="toggle-desc">Bật = công khai · Tắt = chỉ mình thấy</div>
    </div>
    <label class="switch">
      <input type="checkbox" id="tog-share">
      <span class="slider"></span>
    </label>
  </div>
</div>

<!-- Run button -->
<div style="width:100%;max-width:520px;margin-bottom:16px;">
  <button class="btn" id="btn-run" onclick="runTool()">
    <span id="btn-text">🚀 BẮT ĐẦU MOD</span>
    <div class="spinner" id="spinner"></div>
  </button>
</div>

<!-- Result -->
<div class="result-banner" id="result-banner"></div>

<!-- Log -->
<div class="card" style="max-width:520px;padding:0;overflow:hidden;" id="log-card">
  <div style="padding:12px 16px 8px;border-bottom:1px solid var(--border);">
    <div class="card-title" style="margin-bottom:0">Log</div>
  </div>
  <div id="log-panel"></div>
</div>

<script>
const harInput  = document.getElementById('inp-har');
const imgInput  = document.getElementById('inp-img');
const harName   = document.getElementById('har-name');
const imgName   = document.getElementById('img-name');
const btnRun    = document.getElementById('btn-run');
const btnText   = document.getElementById('btn-text');
const spinner   = document.getElementById('spinner');
const logPanel  = document.getElementById('log-panel');
const logCard   = document.getElementById('log-card');
const resultBanner = document.getElementById('result-banner');

harInput.addEventListener('change', () => {
  harName.textContent = harInput.files[0]?.name || '';
});
imgInput.addEventListener('change', () => {
  imgName.textContent = imgInput.files[0]?.name || '';
});

// Drag & drop
['zone-har','zone-img'].forEach(id => {
  const zone = document.getElementById(id);
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag');
    const file = e.dataTransfer.files[0];
    if (!file) return;
    if (id === 'zone-har') { harInput.files = e.dataTransfer.files; harName.textContent = file.name; }
    else                   { imgInput.files = e.dataTransfer.files; imgName.textContent = file.name; }
  });
});

function appendLog(line) {
  const div = document.createElement('div');
  div.className = 'log-line ' +
    (line.startsWith('✅')||line.startsWith('🎉') ? 'log-ok' :
     line.startsWith('❌') ? 'log-err' :
     line.startsWith('⚠️') ? 'log-warn' : 'log-info');
  div.textContent = line;
  logPanel.appendChild(div);
  logPanel.scrollTop = logPanel.scrollHeight;
}

async function runTool() {
  if (!harInput.files[0]) { alert('Vui lòng chọn file HAR!'); return; }
  if (!imgInput.files[0]) { alert('Vui lòng chọn ảnh!'); return; }

  // Reset UI
  logPanel.innerHTML = '';
  logPanel.style.display = 'block';
  logCard.style.display = 'block';
  resultBanner.style.display = 'none';
  btnRun.disabled = true;
  btnText.style.display = 'none';
  spinner.style.display = 'block';

  const fd = new FormData();
  fd.append('har', harInput.files[0]);
  fd.append('img', imgInput.files[0]);
  fd.append('is_share', document.getElementById('tog-share').checked ? '1' : '0');

  try {
    const res  = await fetch('/run', { method: 'POST', body: fd });
    const data = await res.json();
    (data.log || []).forEach(appendLog);
    resultBanner.style.display = 'block';
    if (data.success) {
      resultBanner.className = 'result-banner ok';
      resultBanner.textContent = '🎉 MOD THÀNH CÔNG! Vào game để xem poster mới.';
    } else {
      resultBanner.className = 'result-banner err';
      resultBanner.textContent = '❌ Thất bại. Xem log bên dưới để biết lý do.';
    }
  } catch(e) {
    appendLog('❌ Lỗi kết nối server: ' + e.message);
    resultBanner.style.display = 'block';
    resultBanner.className = 'result-banner err';
    resultBanner.textContent = '❌ Lỗi kết nối tới server.';
  } finally {
    btnRun.disabled = false;
    btnText.style.display = 'block';
    spinner.style.display = 'none';
  }
}
</script>
</body>
</html>'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/run', methods=['POST'])
def run_endpoint():
    har_file = request.files.get('har')
    img_file = request.files.get('img')
    is_share = request.form.get('is_share', '0') == '1'

    if not har_file or not img_file:
        return jsonify({"success": False, "log": ["❌ Thiếu file HAR hoặc ảnh!"]})

    har_bytes = har_file.read()
    img_bytes = img_file.read()
    img_ext   = Path(img_file.filename).suffix.lower().lstrip('.')

    log = []
    success = run_tool(har_bytes, img_bytes, img_ext, is_share, log)
    return jsonify({"success": success, "log": log})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
