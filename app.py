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

# ── ROUTES ──────────────────────────────────────────────
HTML = '''<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOV Load Tran · Pro Tool</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg-deep:    #05070f;
  --bg-base:    #090d1a;
  --bg-card:    #0d1221;
  --bg-input:   #111827;
  --border:     rgba(99,102,241,.15);
  --border-h:   rgba(99,102,241,.45);
  --indigo:     #6366f1;
  --violet:     #8b5cf6;
  --cyan:       #22d3ee;
  --green:      #10b981;
  --amber:      #f59e0b;
  --red:        #f43f5e;
  --text-1:     #f1f5f9;
  --text-2:     #94a3b8;
  --text-3:     #475569;
  --radius:     14px;
  --radius-sm:  8px;
}

html { scroll-behavior: smooth; }

body {
  background: var(--bg-deep);
  color: var(--text-1);
  font-family: 'Be Vietnam Pro', sans-serif;
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── NOISE + GRID BACKGROUND ── */
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 0;
  background:
    radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99,102,241,.12) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 80%, rgba(139,92,246,.08) 0%, transparent 50%),
    linear-gradient(rgba(99,102,241,.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(99,102,241,.03) 1px, transparent 1px);
  background-size: auto, auto, 48px 48px, 48px 48px;
  pointer-events: none;
}

/* ── WRAPPER ── */
.wrapper {
  position: relative; z-index: 1;
  max-width: 560px;
  margin: 0 auto;
  padding: 48px 20px 80px;
}

/* ── HEADER ── */
.header {
  text-align: center;
  margin-bottom: 48px;
}
.header-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(99,102,241,.1);
  border: 1px solid rgba(99,102,241,.25);
  border-radius: 99px;
  padding: 5px 14px;
  font-size: .72rem;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: #a5b4fc;
  margin-bottom: 20px;
}
.header-badge .dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 8px #10b981;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { opacity: 1; }
  50%      { opacity: .3; }
}

.header h1 {
  font-size: 2.6rem;
  font-weight: 800;
  line-height: 1.1;
  letter-spacing: -.02em;
  background: linear-gradient(135deg, #e0e7ff 0%, #a5b4fc 40%, #8b5cf6 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 12px;
}
.header p {
  color: var(--text-2);
  font-size: .9rem;
  font-weight: 400;
  line-height: 1.6;
}

/* ── STEPS INDICATOR ── */
.steps {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0;
  margin-bottom: 36px;
}
.step {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  position: relative;
}
.step-num {
  width: 32px; height: 32px;
  border-radius: 50%;
  border: 1.5px solid var(--border);
  background: var(--bg-card);
  display: flex; align-items: center; justify-content: center;
  font-size: .75rem; font-weight: 700;
  color: var(--text-3);
  transition: all .3s;
}
.step-label {
  font-size: .65rem;
  font-weight: 500;
  color: var(--text-3);
  white-space: nowrap;
  transition: color .3s;
}
.step.active .step-num {
  border-color: var(--indigo);
  background: rgba(99,102,241,.15);
  color: #a5b4fc;
  box-shadow: 0 0 16px rgba(99,102,241,.3);
}
.step.active .step-label { color: #a5b4fc; }
.step.done .step-num {
  border-color: var(--green);
  background: rgba(16,185,129,.1);
  color: var(--green);
}
.step.done .step-label { color: var(--green); }
.step-line {
  width: 56px; height: 1px;
  background: var(--border);
  margin: 0 -1px;
  position: relative;
  top: -14px;
}

/* ── CARD ── */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  margin-bottom: 12px;
  transition: border-color .25s;
  position: relative;
  overflow: hidden;
}
.card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(99,102,241,.4), transparent);
}
.card:hover { border-color: var(--border-h); }

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: .82rem;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--text-2);
}
.card-icon {
  width: 28px; height: 28px;
  border-radius: 7px;
  background: rgba(99,102,241,.12);
  border: 1px solid rgba(99,102,241,.2);
  display: flex; align-items: center; justify-content: center;
  font-size: .9rem;
}
.card-badge {
  font-size: .65rem;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 99px;
  background: rgba(99,102,241,.1);
  border: 1px solid rgba(99,102,241,.2);
  color: #a5b4fc;
  letter-spacing: .06em;
}

/* ── UPLOAD ZONE ── */
.upload-zone {
  border: 1.5px dashed rgba(99,102,241,.25);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  padding: 28px 20px;
  text-align: center;
  cursor: pointer;
  transition: all .25s;
  position: relative;
}
.upload-zone:hover {
  border-color: rgba(99,102,241,.6);
  background: rgba(99,102,241,.05);
}
.upload-zone.drag {
  border-color: var(--indigo);
  background: rgba(99,102,241,.08);
  transform: scale(1.01);
}
.upload-zone.has-file {
  border-color: rgba(16,185,129,.4);
  background: rgba(16,185,129,.04);
}
.upload-zone input[type=file] { display: none; }

.upload-icon-wrap {
  width: 48px; height: 48px;
  margin: 0 auto 12px;
  border-radius: 12px;
  background: rgba(99,102,241,.1);
  border: 1px solid rgba(99,102,241,.2);
  display: flex; align-items: center; justify-content: center;
  font-size: 1.4rem;
  transition: all .25s;
}
.upload-zone:hover .upload-icon-wrap,
.upload-zone.drag .upload-icon-wrap {
  background: rgba(99,102,241,.2);
  transform: scale(1.05);
}
.upload-zone.has-file .upload-icon-wrap {
  background: rgba(16,185,129,.1);
  border-color: rgba(16,185,129,.3);
}

.upload-main-text {
  font-size: .9rem;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 4px;
}
.upload-main-text span { color: var(--indigo); }
.upload-sub-text {
  font-size: .75rem;
  color: var(--text-3);
}

.file-info {
  display: none;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 8px 12px;
  background: rgba(16,185,129,.06);
  border: 1px solid rgba(16,185,129,.2);
  border-radius: 6px;
  font-size: .78rem;
  color: var(--green);
  font-weight: 500;
}
.file-info.show { display: flex; }
.file-info-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'JetBrains Mono', monospace;
  font-size: .72rem;
}
.file-info-size { color: var(--text-3); white-space: nowrap; }

/* ── PREVIEW ── */
.img-preview {
  display: none;
  width: 100%;
  max-height: 160px;
  object-fit: cover;
  border-radius: 6px;
  margin-top: 12px;
  border: 1px solid var(--border);
}
.img-preview.show { display: block; }

/* ── OPTIONS ── */
.option-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 0;
  border-bottom: 1px solid rgba(255,255,255,.04);
}
.option-row:last-child { border-bottom: none; padding-bottom: 0; }
.option-row:first-child { padding-top: 0; }

.option-info { flex: 1; }
.option-label {
  font-size: .88rem;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 2px;
}
.option-desc {
  font-size: .73rem;
  color: var(--text-3);
}

/* toggle */
.switch { position: relative; display: inline-block; width: 42px; height: 22px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; inset: 0;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 22px;
  cursor: pointer;
  transition: background .25s, border-color .25s;
}
.slider::before {
  content: '';
  position: absolute;
  width: 16px; height: 16px;
  left: 2px; top: 2px;
  background: var(--text-3);
  border-radius: 50%;
  transition: transform .25s, background .25s;
}
input:checked + .slider { background: rgba(99,102,241,.2); border-color: var(--indigo); }
input:checked + .slider::before { transform: translateX(20px); background: var(--indigo); }

/* ── RUN BUTTON ── */
.btn-wrap { margin: 20px 0 12px; }
.btn-run {
  width: 100%;
  padding: 16px 24px;
  border: none; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--indigo) 0%, var(--violet) 100%);
  color: #fff;
  font-family: 'Be Vietnam Pro', sans-serif;
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: .04em;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 10px;
  transition: opacity .2s, transform .15s, box-shadow .25s;
  box-shadow: 0 4px 24px rgba(99,102,241,.3);
  position: relative;
  overflow: hidden;
}
.btn-run::before {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(255,255,255,.12) 0%, transparent 60%);
}
.btn-run:hover:not(:disabled) {
  opacity: .93;
  transform: translateY(-2px);
  box-shadow: 0 8px 32px rgba(99,102,241,.45);
}
.btn-run:active:not(:disabled) { transform: translateY(0); }
.btn-run:disabled { opacity: .35; cursor: not-allowed; transform: none; }

.spinner {
  width: 18px; height: 18px;
  border: 2px solid rgba(255,255,255,.25);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin .6s linear infinite;
  display: none;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── PROGRESS BAR ── */
.progress-wrap {
  display: none;
  margin-bottom: 12px;
}
.progress-label {
  display: flex;
  justify-content: space-between;
  font-size: .72rem;
  color: var(--text-3);
  margin-bottom: 6px;
}
.progress-track {
  height: 3px;
  background: var(--bg-input);
  border-radius: 99px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  width: 0%;
  background: linear-gradient(90deg, var(--indigo), var(--cyan));
  border-radius: 99px;
  transition: width .4s ease;
  box-shadow: 0 0 8px rgba(34,211,238,.4);
}

/* ── LOG PANEL ── */
.log-wrap {
  display: none;
  background: #060810;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 12px;
}
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: rgba(255,255,255,.02);
}
.log-header-title {
  font-size: .72rem;
  font-weight: 700;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--text-3);
  display: flex; align-items: center; gap: 6px;
}
.log-live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
  animation: pulse 1.2s infinite;
}
.log-body {
  padding: 14px 16px;
  font-family: 'JetBrains Mono', monospace;
  font-size: .73rem;
  line-height: 1.8;
  max-height: 280px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(99,102,241,.3) transparent;
}
.log-line { display: flex; gap: 8px; }
.log-time { color: var(--text-3); flex-shrink: 0; }
.log-msg.ok   { color: #34d399; }
.log-msg.err  { color: #fb7185; }
.log-msg.warn { color: #fbbf24; }
.log-msg.info { color: #94a3b8; }

/* ── RESULT BANNER ── */
.result-banner {
  display: none;
  border-radius: var(--radius-sm);
  padding: 18px 20px;
  margin-bottom: 12px;
  text-align: center;
}
.result-banner.ok {
  background: rgba(16,185,129,.08);
  border: 1px solid rgba(16,185,129,.25);
}
.result-banner.err {
  background: rgba(244,63,94,.08);
  border: 1px solid rgba(244,63,94,.25);
}
.result-icon { font-size: 2rem; margin-bottom: 8px; }
.result-title {
  font-size: 1rem; font-weight: 700;
  color: var(--text-1);
  margin-bottom: 4px;
}
.result-sub { font-size: .8rem; color: var(--text-2); }

/* ── FOOTER ── */
.footer {
  text-align: center;
  margin-top: 40px;
  font-size: .72rem;
  color: var(--text-3);
}
.footer span { color: var(--text-2); }
</style>
</head>
<body>
<div class="wrapper">

  <!-- HEADER -->
  <header class="header">
    <div class="header-badge">
      <span class="dot"></span>
      AOV Pro Tools v1.0
    </div>
    <h1>Load Tran<br>Mod Tool</h1>
    <p>Upload file HAR từ game và ảnh của bạn.<br>Tool sẽ tự động mod poster load trận.</p>
  </header>

  <!-- STEPS -->
  <div class="steps">
    <div class="step active" id="step-1">
      <div class="step-num">1</div>
      <div class="step-label">HAR</div>
    </div>
    <div class="step-line"></div>
    <div class="step" id="step-2">
      <div class="step-num">2</div>
      <div class="step-label">Ảnh</div>
    </div>
    <div class="step-line"></div>
    <div class="step" id="step-3">
      <div class="step-num">3</div>
      <div class="step-label">Tuỳ chọn</div>
    </div>
    <div class="step-line"></div>
    <div class="step" id="step-4">
      <div class="step-num">4</div>
      <div class="step-label">Chạy</div>
    </div>
  </div>

  <!-- CARD: HAR -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <div class="card-icon">📂</div>
        File HAR
      </div>
      <div class="card-badge">Bắt buộc</div>
    </div>
    <div class="upload-zone" id="zone-har" onclick="document.getElementById('inp-har').click()">
      <div class="upload-icon-wrap" id="har-icon">📂</div>
      <div class="upload-main-text">Nhấn để chọn hoặc <span>kéo thả</span></div>
      <div class="upload-sub-text">File .har từ HTTP Canary / Reqable · Tối đa 50MB</div>
      <input type="file" id="inp-har" accept=".har">
    </div>
    <div class="file-info" id="har-info">
      <span>✅</span>
      <span class="file-info-name" id="har-fname"></span>
      <span class="file-info-size" id="har-fsize"></span>
    </div>
  </div>

  <!-- CARD: IMAGE -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <div class="card-icon">🖼️</div>
        Ảnh / GIF
      </div>
      <div class="card-badge">JPG · PNG · WEBP · GIF</div>
    </div>
    <div class="upload-zone" id="zone-img" onclick="document.getElementById('inp-img').click()">
      <div class="upload-icon-wrap" id="img-icon">🖼️</div>
      <div class="upload-main-text">Nhấn để chọn hoặc <span>kéo thả</span></div>
      <div class="upload-sub-text">Hỗ trợ ảnh tĩnh và GIF động · Tối đa 50MB</div>
      <input type="file" id="inp-img" accept=".jpg,.jpeg,.png,.webp,.gif">
    </div>
    <div class="file-info" id="img-info">
      <span>✅</span>
      <span class="file-info-name" id="img-fname"></span>
      <span class="file-info-size" id="img-fsize"></span>
    </div>
    <img class="img-preview" id="img-preview" alt="Preview">
  </div>

  <!-- CARD: OPTIONS -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <div class="card-icon">⚙️</div>
        Tuỳ chọn
      </div>
    </div>
    <div class="option-row">
      <div class="option-info">
        <div class="option-label">Đăng lên Quảng Trường</div>
        <div class="option-desc">Bật → công khai cho mọi người thấy · Tắt → chỉ mình thấy</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="tog-share">
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <!-- PROGRESS -->
  <div class="progress-wrap" id="progress-wrap">
    <div class="progress-label">
      <span id="progress-label-text">Đang xử lý...</span>
      <span id="progress-pct">0%</span>
    </div>
    <div class="progress-track">
      <div class="progress-fill" id="progress-fill"></div>
    </div>
  </div>

  <!-- RUN BUTTON -->
  <div class="btn-wrap">
    <button class="btn-run" id="btn-run" onclick="runTool()">
      <span id="btn-text">🚀  Bắt đầu Mod</span>
      <div class="spinner" id="spinner"></div>
    </button>
  </div>

  <!-- RESULT -->
  <div class="result-banner" id="result-banner">
    <div class="result-icon" id="result-icon"></div>
    <div class="result-title" id="result-title"></div>
    <div class="result-sub" id="result-sub"></div>
  </div>

  <!-- LOG -->
  <div class="log-wrap" id="log-wrap">
    <div class="log-header">
      <div class="log-header-title">
        <div class="log-live-dot" id="log-dot"></div>
        Console Output
      </div>
      <span style="font-size:.68rem;color:var(--text-3)" id="log-count">0 dòng</span>
    </div>
    <div class="log-body" id="log-body"></div>
  </div>

  <footer class="footer">
    Made with ❤️ by <span>Hoàng Phúc</span> · AOV Mod Tools
  </footer>
</div>

<script>
// ── FILE INPUTS ──────────────────────────────────────────
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(1) + ' MB';
}

function setupZone(zoneId, inputId, infoId, fnameId, fsizeId, iconId, previewId) {
  const zone   = document.getElementById(zoneId);
  const input  = document.getElementById(inputId);
  const info   = document.getElementById(infoId);
  const fname  = document.getElementById(fnameId);
  const fsize  = document.getElementById(fsizeId);
  const icon   = document.getElementById(iconId);
  const preview = previewId ? document.getElementById(previewId) : null;

  function onFile(file) {
    if (!file) return;
    fname.textContent = file.name;
    fsize.textContent = fmtSize(file.size);
    info.classList.add('show');
    zone.classList.add('has-file');
    icon.textContent = '✅';
    if (preview && file.type.startsWith('image/')) {
      const url = URL.createObjectURL(file);
      preview.src = url;
      preview.classList.add('show');
    }
    updateSteps();
  }

  input.addEventListener('change', () => onFile(input.files[0]));
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag');
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    onFile(file);
  });
}

setupZone('zone-har','inp-har','har-info','har-fname','har-fsize','har-icon', null);
setupZone('zone-img','inp-img','img-info','img-fname','img-fsize','img-icon','img-preview');

// ── STEPS ────────────────────────────────────────────────
function updateSteps() {
  const harOk = !!document.getElementById('inp-har').files[0];
  const imgOk = !!document.getElementById('inp-img').files[0];
  setStep(1, harOk ? 'done' : 'active');
  setStep(2, harOk && imgOk ? 'done' : harOk ? 'active' : '');
  setStep(3, harOk && imgOk ? 'active' : '');
  setStep(4, '');
}
function setStep(n, state) {
  const el = document.getElementById('step-' + n);
  el.className = 'step' + (state ? ' ' + state : '');
}

// ── LOG ──────────────────────────────────────────────────
let logLineCount = 0;
function appendLog(line) {
  const wrap = document.getElementById('log-wrap');
  const body = document.getElementById('log-body');
  wrap.style.display = 'block';

  const now = new Date();
  const ts  = now.toTimeString().slice(0,8);

  const cls = line.startsWith('✅')||line.startsWith('🎉') ? 'ok'
            : line.startsWith('❌') ? 'err'
            : line.startsWith('⚠️') ? 'warn' : 'info';

  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = `<span class="log-time">${ts}</span><span class="log-msg ${cls}">${line}</span>`;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
  logLineCount++;
  document.getElementById('log-count').textContent = logLineCount + ' dòng';
}

// ── PROGRESS SIMULATION ──────────────────────────────────
let progressInterval = null;
const STEPS_PROGRESS = [
  [10, 'Đang đọc file HAR...'],
  [25, 'Kết nối API game...'],
  [40, 'Lấy quyền hạn COS...'],
  [60, 'Đang upload ảnh...'],
  [80, 'Đang lưu poster...'],
  [95, 'Hoàn tất...'],
];
let progIdx = 0;

function startProgress() {
  const wrap = document.getElementById('progress-wrap');
  wrap.style.display = 'block';
  progIdx = 0;
  setProgress(5, 'Đang khởi động...');
  progressInterval = setInterval(() => {
    if (progIdx < STEPS_PROGRESS.length) {
      const [pct, label] = STEPS_PROGRESS[progIdx++];
      setProgress(pct, label);
    }
  }, 1800);
}
function setProgress(pct, label) {
  document.getElementById('progress-fill').style.width = pct + '%';
  document.getElementById('progress-pct').textContent = pct + '%';
  document.getElementById('progress-label-text').textContent = label;
}
function endProgress(ok) {
  clearInterval(progressInterval);
  setProgress(100, ok ? 'Hoàn tất!' : 'Thất bại');
  setTimeout(() => {
    document.getElementById('progress-wrap').style.display = 'none';
  }, 1500);
}

// ── RUN ──────────────────────────────────────────────────
async function runTool() {
  const harFile = document.getElementById('inp-har').files[0];
  const imgFile = document.getElementById('inp-img').files[0];
  if (!harFile) { shakeCard('zone-har'); alert('Vui lòng chọn file HAR!'); return; }
  if (!imgFile) { shakeCard('zone-img'); alert('Vui lòng chọn ảnh!'); return; }

  // Reset
  document.getElementById('log-body').innerHTML = '';
  document.getElementById('log-wrap').style.display = 'none';
  document.getElementById('result-banner').style.display = 'none';
  logLineCount = 0;

  const btn     = document.getElementById('btn-run');
  const btnText = document.getElementById('btn-text');
  const spinner = document.getElementById('spinner');
  const logDot  = document.getElementById('log-dot');

  btn.disabled = true;
  btnText.style.display = 'none';
  spinner.style.display = 'block';
  logDot.style.background = '#f59e0b';
  logDot.style.boxShadow  = '0 0 6px #f59e0b';

  setStep(4, 'active');
  startProgress();

  const fd = new FormData();
  fd.append('har', harFile);
  fd.append('img', imgFile);
  fd.append('is_share', document.getElementById('tog-share').checked ? '1' : '0');

  try {
    const res  = await fetch('/run', { method: 'POST', body: fd });
    const data = await res.json();
    (data.log || []).forEach(appendLog);

    endProgress(data.success);

    const banner = document.getElementById('result-banner');
    banner.style.display = 'block';

    if (data.success) {
      document.getElementById('result-icon').textContent  = '🎉';
      document.getElementById('result-title').textContent = 'Mod thành công!';
      document.getElementById('result-sub').textContent   = 'Vào game và kiểm tra poster load trận của bạn.';
      banner.className = 'result-banner ok';
      setStep(4, 'done');
      logDot.style.background = '#10b981';
      logDot.style.boxShadow  = '0 0 6px #10b981';
    } else {
      document.getElementById('result-icon').textContent  = '❌';
      document.getElementById('result-title').textContent = 'Thất bại';
      document.getElementById('result-sub').textContent   = 'Xem Console Output bên dưới để biết lý do.';
      banner.className = 'result-banner err';
      logDot.style.background = '#f43f5e';
      logDot.style.boxShadow  = '0 0 6px #f43f5e';
    }
  } catch(e) {
    appendLog('❌ Lỗi kết nối server: ' + e.message);
    endProgress(false);
    const banner = document.getElementById('result-banner');
    banner.style.display = 'block';
    banner.className = 'result-banner err';
    document.getElementById('result-icon').textContent  = '❌';
    document.getElementById('result-title').textContent = 'Lỗi kết nối';
    document.getElementById('result-sub').textContent   = e.message;
  } finally {
    btn.disabled = false;
    btnText.style.display = 'block';
    spinner.style.display = 'none';
  }
}

function shakeCard(id) {
  const el = document.getElementById(id);
  el.style.animation = 'none';
  el.offsetHeight;
  el.style.animation = 'shake .4s ease';
}
</style>
<style>
@keyframes shake {
  0%,100% { transform: translateX(0); }
  20%,60% { transform: translateX(-6px); }
  40%,80% { transform: translateX(6px); }
}
</style>
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
