#!/usr/bin/env python3
"""
STL Webhook Server — No SMS, Retell calls + emails only
"""
import os, json, time, logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import smtplib
from email.mime.text import MIMEText

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('stl_webhooks')

# Config from env
RETELL_KEY = os.getenv('RETELL_API_KEY', '')
RETELL_BASE = os.getenv('RETELL_BASE_URL', 'https://api.retellai.com/v2')
FROM_NUMBER = os.getenv('RETELL_FROM_NUMBER', '+18887991869')            # used for book-marylou
CALL_FROM_NUMBER = os.getenv('RETELL_CALL_FROM_NUMBER', '')              # new DID for urgent calls
CALL_TO_NUMBER = os.getenv('CALL_TO_NUMBER', '+17807172819')             # Kirk's cell
MARY_LOU_ID = os.getenv('MARY_LOU_AGENT_ID', '')
NOTIFIER_ID = os.getenv('NOTIFIER_AGENT_ID', '')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('GMAIL_USER', '')
SMTP_PASS = os.getenv('GMAIL_APP_PASSWORD', '')
KIRK_EMAIL = os.getenv('KIRK_NOTIFY_EMAIL', 'kirk@getstl.tech')
PORT = int(os.getenv('PORT', '5000'))

# Helpers
def send_email(to, subj, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = to
    msg['Subject'] = subj
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        logger.info(f"Email sent to {to}")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False

def retell_call(to_number, agent_id, from_number, metadata=None, scheduled_after=None):
    headers = {"Authorization": f"Bearer {RETELL_KEY}", "Content-Type": "application/json"}
    payload = {"from_number": from_number, "to_number": to_number, "override_agent_id": agent_id}
    if metadata:
        payload["metadata"] = metadata
        payload["retell_llm_dynamic_variables"] = metadata
    if scheduled_after:
        payload["scheduled_timestamp_after"] = scheduled_after
    try:
        r = requests.post(f"{RETELL_BASE}/create-phone-call", json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        logger.info(f"Retell call created: {data.get('call_id')}")
        return {"success": True, "call_id": data.get('call_id'), "data": data}
    except Exception as e:
        logger.error(f"Retell call failed: {e}")
        # capture raw error body if possible
        error_detail = str(e)
        try:
            error_detail = r.text
        except: pass
        return {"success": False, "error": error_detail}

# Routes
@app.route('/')
def root():
    return jsonify({"status": "ok", "service": "stl-webhooks"})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# Scenario 1 — book_marylou_callback (unchanged, uses FROM_NUMBER)
@app.route('/book-marylou', methods=['POST'])
def book_marylou():
    data = request.get_json(force=True)
    for f in ["caller_name","company","phone_number","scheduled_time"]:
        if f not in data:
            return jsonify({"error": f"Missing {f}"}), 400
    try:
        dt = datetime.fromisoformat(data["scheduled_time"])
        epoch = int(dt.timestamp())
    except:
        try:
            dt = datetime.strptime(data["scheduled_time"].replace('Z','+00:00'), "%Y-%m-%dT%H:%M:%S%z")
            epoch = int(dt.timestamp())
        except:
            return jsonify({"error": "Bad timestamp"}), 400
    res = retell_call(data["phone_number"], MARY_LOU_ID, FROM_NUMBER,
                      metadata={"caller_name": data["caller_name"], "company": data["company"]},
                      scheduled_after=epoch)
    if res["success"]:
        send_email(KIRK_EMAIL, f"BOOKED: {data['caller_name']} {data['company']}",
                   f"Call scheduled for {data['scheduled_time']}\nRetell ID: {res['call_id']}")
        return jsonify({"ok": True, "call_id": res["call_id"]})
    else:
        send_email(KIRK_EMAIL, "BOOKING FAILED", f"Error: {res.get('error')}\nPayload: {json.dumps(data)}")
        return jsonify({"error": res.get('error')}), 500

# Scenario 2 — notify_kirk (URGENT => call, NORMAL/LOW => email only)
@app.route('/notify-kirk', methods=['POST'])
def notify_kirk():
    data = request.get_json(force=True)
    urgency = data.get("urgency","").lower()
    summary = data.get("summary","")
    if not urgency or not summary:
        return jsonify({"error": "Missing urgency or summary"}), 400
    if urgency == "urgent":
        # Call via Retell using new DID
        res = retell_call(CALL_TO_NUMBER, NOTIFIER_ID, CALL_FROM_NUMBER,
                          metadata={"summary": summary})
        if res["success"]:
            send_email(KIRK_EMAIL, f"URGENT CALL PLACED", f"Retell call {res['call_id']} placed to {CALL_TO_NUMBER}\nSummary: {summary}")
            return jsonify({"ok": True, "call_triggered": True, "call_id": res["call_id"]})
        else:
            send_email(KIRK_EMAIL, "URGENT CALL FAILED", f"Retell error: {res.get('error')}\nSummary: {summary}")
            return jsonify({"error": res.get('error')}), 500
    else:
        # normal / low → email only
        send_email(KIRK_EMAIL, f"NOTIFICATION: {urgency}", summary)
        return jsonify({"ok": True, "call_triggered": False, "email_sent": True})

# Scenario 3 — activity_log (renamed from activity-sms)
@app.route('/activity-log', methods=['POST'])
@app.route('/activity-sms', methods=['POST'])   # backward compat for existing Retell config
def activity_log():
    data = request.get_json(force=True)
    agent = data.get("agent_name", "Unknown")
    duration = data.get("call_duration", 0)
    mins = int(duration/60000); secs = int((duration%60000)/1000)
    dur_str = f"{mins}:{secs:02d}"
    transcript = data.get("transcript","").lower()
    meta = data.get("metadata", {})
    caller = meta.get("caller_name", data.get("caller_number","unknown"))
    company = meta.get("company","")
    emoji = {"Helen":"📞","Marcus":"📅","Donna":"🌙","Mary-Lou":"🎯","Notifier":"🔔"}.get(agent, "📞")
    if any(k in transcript for k in ["book","schedule","appointment"]):
        summary = "Booked appointment"; next_step = "Auto-dial scheduled"
    elif any(k in transcript for k in ["discovery","assessment"]):
        summary = "Discovery call completed"; next_step = "Kirk drafts report"
    elif any(k in transcript for k in ["urgent","emergency"]):
        summary = "Urgent client issue"; next_step = "Immediate callback"
    elif any(k in transcript for k in ["callback","call back"]):
        summary = "Callback requested"; next_step = "Kirk follows up"
    elif any(k in transcript for k in ["message","left a message"]):
        summary = "Message left"; next_step = "Review in Retell"
    else:
        summary = "Call completed"; next_step = "Check dashboard"
    company_str = f", {company}" if company else ""
    email_body = f"{emoji} {agent} {dur_str} — {caller}{company_str}\n→ {summary}\n→ Next: {next_step}"
    send_email(KIRK_EMAIL, f"Call log: {agent} ({caller})", email_body)
    return jsonify({"ok": True, "email_sent": True, "summary": email_body})

# Keep-Alive
startup_time = time.time()
import threading
def keepalive_ping():
    while True:
        time.sleep(5 * 60)
        try:
            requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
            uptime_h = (time.time() - startup_time) / 3600
            logger.info(f"Keepalive ping OK — uptime {uptime_h:.1f}h")
        except Exception as e:
            logger.warning(f"Keepalive ping failed: {e}")
threading.Thread(target=keepalive_ping, daemon=True).start()

if __name__ == '__main__':
    logger.info(f"Starting STL Webhook Server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
