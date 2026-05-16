#!/usr/bin/env python3
"""
STL Webhook Server — 3 endpoints for Retell automation
Deploy to Render (free) for 24/7 uptime.
All secrets are read from environment variables.
Edit config.yaml for local development only (ignored when env vars are set).
"""
import os
import json
import smtplib
import logging
import time
import threading
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# ---- Load config (env vars OR config.yaml fallback) ----
def load_config():
    env = os.getenv
    config = {
        'retell': {
            'api_key': env('RETELL_API_KEY', ''),
            'base_url': env('RETELL_BASE_URL', 'https://api.retellai.com/v2'),
            'from_number': env('RETELL_FROM_NUMBER', '+18887991869')
        },
        'agents': {
            'mary_lou': env('MARY_LOU_AGENT_ID', ''),
            'notifier': env('NOTIFIER_AGENT_ID', '')
        },
        'smtp': {
            'host': env('SMTP_HOST', 'smtp.gmail.com'),
            'port': int(env('SMTP_PORT', '587')),
            'user': env('GMAIL_USER', ''),
            'password': env('GMAIL_APP_PASSWORD', '')
        },
        'notifications': {
            'kirk_email': env('KIRK_NOTIFY_EMAIL', ''),
            'telus_cell': env('KIRK_CELL_NUMBER', ''),
            'sms_gateway': env('SMS_GATEWAY', '@msg.telus.com')
        },
        'server': {
            'port': int(env('PORT', '5000')),
            'keepalive_minutes': int(env('KEEPALIVE_MINUTES', '5'))
        }
    }
    # Fallback: try loading from config.yaml for local development
    try:
        import yaml
        with open('config.yaml') as f:
            yaml_cfg = yaml.safe_load(f)
        # Only use yaml values if env var is empty
        for section in config:
            if section in yaml_cfg:
                for key in config[section]:
                    if not config[section][key]:
                        config[section][key] = yaml_cfg[section].get(key, config[section][key])
    except:
        pass
    return config

cfg = load_config()

RETELL_API_KEY = cfg['retell']['api_key']
RETELL_BASE = cfg['retell']['base_url']
FROM_NUMBER = cfg['retell']['from_number']
MARY_LOU = cfg['agents']['mary_lou']
NOTIFIER = cfg['agents']['notifier']
SMTP_HOST = cfg['smtp']['host']
SMTP_PORT = cfg['smtp']['port']
SMTP_USER = cfg['smtp']['user']
SMTP_PASS = cfg['smtp']['password']
KIRK_EMAIL = cfg['notifications']['kirk_email']
TELUS_CELL = cfg['notifications']['telus_cell']
SMS_GATEWAY = cfg['notifications']['sms_gateway']
SERVER_PORT = cfg['server']['port']
KEEPALIVE_MIN = cfg['server']['keepalive_minutes']

# ---- APP ----
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('webhook_server')

# ---- HELPERS ----
def send_email_sms(to_cell, body):
    to_addr = f"{to_cell}{SMS_GATEWAY}"
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = to_addr
    msg['Subject'] = ''
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        logger.info(f"SMS sent to {to_cell}")
        return True
    except Exception as e:
        logger.error(f"SMS send failed: {e}")
        return False

def send_email(to, subject, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = to
    msg['Subject'] = subject
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        logger.info(f"Email sent to {to}")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False

def retell_create_call(to_number, agent_id, metadata=None, scheduled_after=None):
    headers = {"Authorization": f"Bearer {RETELL_API_KEY}", "Content-Type": "application/json"}
    body = {"from_number": FROM_NUMBER, "to_number": to_number, "override_agent_id": agent_id}
    if metadata:
        body["metadata"] = metadata
        body["retell_llm_dynamic_variables"] = metadata
    if scheduled_after:
        body["scheduled_timestamp_after"] = scheduled_after
    try:
        r = requests.post(f"{RETELL_BASE}/create-phone-call", json=body, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        logger.info(f"Retell call created: {data.get('call_id', 'unknown')}")
        return {"success": True, "call_id": data.get("call_id"), "data": data}
    except Exception as e:
        logger.error(f"Retell call failed: {e}")
        return {"success": False, "error": str(e)}

# ---- SCENARIO 1: book_marylou_callback ----
@app.route('/book-marylou', methods=['POST'])
def book_marylou():
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error": "Invalid JSON"}), 400
    required = ["caller_name", "company", "phone_number", "scheduled_time"]
    for field in required:
        if field not in data:
            send_email(KIRK_EMAIL, "❌ BOOKING FAILED — Missing fields", json.dumps(data))
            return jsonify({"error": f"Missing field: {field}"}), 400
    caller = data["caller_name"]
    company = data["company"]
    phone = data["phone_number"]
    scheduled = data["scheduled_time"]
    try:
        dt = datetime.fromisoformat(scheduled)
        epoch = int(dt.timestamp())
    except:
        try:
            dt = datetime.strptime(scheduled.replace('Z', '+00:00'), "%Y-%m-%dT%H:%M:%S%z")
            epoch = int(dt.timestamp())
        except:
            send_email(KIRK_EMAIL, "❌ BOOKING FAILED — Bad timestamp", f"scheduled_time={scheduled}")
            return jsonify({"error": "Invalid scheduled_time format"}), 400
    result = retell_create_call(phone, MARY_LOU, metadata={"caller_name": caller, "company": company}, scheduled_after=epoch)
    if result["success"]:
        call_id = result["call_id"]
        send_email(KIRK_EMAIL, f"🟢 BOOKING LOCKED — {caller} from {company} — {scheduled}",
                   f"Booking confirmed.\nCaller: {caller}\nCompany: {company}\nPhone: {phone}\nScheduled: {scheduled}\nRetell Call ID: {call_id}")
        logger.info(f"Mary-Lou callback booked for {caller} at {scheduled}")
        return jsonify({"ok": True, "call_id": call_id, "scheduled": scheduled})
    else:
        send_email(KIRK_EMAIL, f"🔴 BOOKING FAILED — {caller} — Manual fix needed",
                   f"Retell error: {result['error']}\nPayload: {json.dumps(data)}")
        return jsonify({"error": result["error"]}), 500

# ---- SCENARIO 2: notify_kirk ----
@app.route('/notify-kirk', methods=['POST'])
def notify_kirk():
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error": "Invalid JSON"}), 400
    if "urgency" not in data or "summary" not in data:
        send_email(KIRK_EMAIL, "❌ NOTIFICATION FAILED — Missing fields", json.dumps(data))
        return jsonify({"error": "Missing urgency or summary"}), 400
    urgency = data["urgency"].lower()
    summary = data["summary"]
    if urgency not in ["urgent", "normal", "low"]:
        return jsonify({"error": "urgency must be urgent, normal, or low"}), 400
    sms_ok = send_email_sms(TELUS_CELL, summary)
    call_ok = True
    if urgency == "urgent":
        result = retell_create_call(f"+1{TELUS_CELL}", NOTIFIER, metadata={"summary": summary})
        call_ok = result["success"]
    if not sms_ok or not call_ok:
        send_email(KIRK_EMAIL, f"🔴 NOTIFICATION FAILED — {urgency}",
                   f"SMS ok: {sms_ok}, Call ok: {call_ok}\nSummary: {summary}")
    logger.info(f"Notify Kirk: urgency={urgency}, sms={sms_ok}, call={call_ok}")
    return jsonify({"ok": True, "sms_ok": sms_ok, "call_triggered": urgency == "urgent" and call_ok})

# ---- SCENARIO 3: activity_sms ----
@app.route('/activity-sms', methods=['POST'])
def activity_sms():
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error": "Invalid JSON"}), 400
    call_id = data.get("call_id", "unknown")
    agent_name = data.get("agent_name", "Unknown")
    call_duration = data.get("call_duration", 0)
    caller_number = data.get("caller_number", "unknown")
    transcript = data.get("transcript", "")
    metadata = data.get("metadata", {})
    caller_name = metadata.get("caller_name", caller_number)
    company = metadata.get("company", "")
    emoji_map = {"Helen": "📞", "Marcus": "📅", "Donna": "🌙", "Mary-Lou": "🎯", "Notifier": "🔔"}
    emoji = emoji_map.get(agent_name, "📞")
    mins = int(call_duration / 60000)
    secs = int((call_duration % 60000) / 1000)
    dur_str = f"{mins}:{secs:02d}"
    summary = "Call completed"
    next_step = "Check dashboard"
    t = transcript.lower()
    if any(w in t for w in ["book", "schedule", "appointment"]):
        summary = "Booked appointment"; next_step = "Auto-dial scheduled"
    elif any(w in t for w in ["discovery", "assessment"]):
        summary = "Discovery call completed"; next_step = "Kirk drafts report"
    elif any(w in t for w in ["urgent", "emergency"]):
        summary = "Urgent client issue"; next_step = "Immediate callback needed"
    elif any(w in t for w in ["callback", "call back"]):
        summary = "Callback requested"; next_step = "Kirk follows up"
    elif any(w in t for w in ["message", "left a message"]):
        summary = "Message left"; next_step = "Review in Retell"
    company_str = f", {company}" if company else ""
    sms = f"{emoji} {agent_name} {dur_str} — {caller_name}{company_str}\n→ {summary}\n→ Next: {next_step}"
    if len(sms) > 155:
        sms = sms[:152] + "..."
    sms_ok = send_email_sms(TELUS_CELL, sms)
    if not sms_ok:
        send_email(KIRK_EMAIL, f"⚠️ Activity feed failed for call {call_id}",
                   f"Check Retell dashboard.\nPayload: {json.dumps(data)[:500]}")
        return jsonify({"error": "SMS send failed"}), 500
    logger.info(f"Activity SMS sent: {sms[:80]}...")
    return jsonify({"ok": True, "sms": sms})

# ---- Health check ----
@app.route('/', methods=['GET'])
def root():
    return jsonify({"status": "ok", "service": "stl-webhooks", "time": datetime.now().isoformat()})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ---- Keep-Alive ----
startup_time = time.time()
def keepalive_ping():
    while True:
        time.sleep(KEEPALIVE_MIN * 60)
        try:
            r = requests.get(f"http://127.0.0.1:{SERVER_PORT}/health", timeout=10)
            uptime_h = (time.time() - startup_time) / 3600
            logger.info(f"Keepalive ping OK — uptime {uptime_h:.1f}h")
        except Exception as e:
            logger.warning(f"Keepalive ping failed: {e}")

threading.Thread(target=keepalive_ping, daemon=True).start()

# ---- Main ----
if __name__ == '__main__':
    logger.info(f"Starting STL Webhook Server on port {SERVER_PORT}")
    logger.info(f"Keep-alive: every {KEEPALIVE_MIN} min")
    logger.info("Endpoints: /book-marylou, /notify-kirk, /activity-sms, /health")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)
