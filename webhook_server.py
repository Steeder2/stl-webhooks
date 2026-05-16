import os, json, sys, time, logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import smtplib
from email.mime.text import MIMEText

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('stl_webhooks')

# Config from env (with defaults for testing)
RETELL_KEY = os.getenv('RETELL_API_KEY', 'test')
RETELL_BASE = os.getenv('RETELL_BASE_URL', 'https://api.retellai.com/v2')
FROM_NUMBER = os.getenv('RETELL_FROM_NUMBER', '+18887991869')
MARY_LOU_ID = os.getenv('MARY_LOU_AGENT_ID', 'agent_498a980793e0bd27f182574b57')
NOTIFIER_ID = os.getenv('NOTIFIER_AGENT_ID', 'agent_a86395fd3154312c27e86c89c1')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('GMAIL_USER', 'steedpipeline@gmail.com')
SMTP_PASS = os.getenv('GMAIL_APP_PASSWORD', '')
KIRK_EMAIL = os.getenv('KIRK_NOTIFY_EMAIL', 'kirk@getstl.tech')
TELUS_CELL = os.getenv('KIRK_CELL_NUMBER', '7807172819')
SMS_GATEWAY = os.getenv('SMS_GATEWAY', '@msg.telus.com')
PORT = int(os.getenv('PORT', '5000'))

# Helpers
def send_sms(to_cell, body):
    to_addr = f"{to_cell}{SMS_GATEWAY}"
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = to_addr
    msg['Subject'] = ''
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
        s.quit()
        logger.info(f"SMS sent to {to_cell}")
        return True
    except Exception as e:
        logger.error(f"SMS failed: {e}")
        return False

def send_email(to, subj, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = to
    msg['Subject'] = subj
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
        s.quit()
        logger.info(f"Email sent to {to}")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False

def retell_call(to_number, agent_id, metadata=None, scheduled_after=None):
    headers = {"Authorization": f"Bearer {RETELL_KEY}", "Content-Type": "application/json"}
    payload = {"from_number": FROM_NUMBER, "to_number": to_number, "override_agent_id": agent_id}
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
        return {"success": True, "call_id": data.get('call_id')}
    except Exception as e:
        logger.error(f"Retell call failed: {e}")
        return {"success": False, "error": str(e)}

# Endpoints
@app.route('/')
def root():
    return jsonify({"status": "ok", "service": "stl-webhooks"})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route('/book-marylou', methods=['POST'])
def book_marylou():
    data = request.get_json(force=True)
    for f in ["caller_name","company","phone_number","scheduled_time"]:
        if f not in data:
            return jsonify({"error": f"Missing {f}"}), 400
    # parse time
    try:
        dt = datetime.fromisoformat(data["scheduled_time"])
        epoch = int(dt.timestamp())
    except:
        try:
            dt = datetime.strptime(data["scheduled_time"].replace('Z','+00:00'), "%Y-%m-%dT%H:%M:%S%z")
            epoch = int(dt.timestamp())
        except:
            return jsonify({"error": "Bad timestamp"}), 400
    res = retell_call(data["phone_number"], MARY_LOU_ID, {"caller_name": data["caller_name"], "company": data["company"]}, scheduled_after=epoch)
    if res["success"]:
        send_email(KIRK_EMAIL, f"BOOKED: {data['caller_name']} {data['company']}", f"Call scheduled for {data['scheduled_time']}\nRetell ID: {res['call_id']}")
        return jsonify({"ok": True, "call_id": res["call_id"]})
    else:
        send_email(KIRK_EMAIL, "BOOKING FAILED", f"Error: {res.get('error')}\nPayload: {json.dumps(data)}")
        return jsonify({"error": res.get('error')}), 500

@app.route('/notify-kirk', methods=['POST'])
def notify_kirk():
    data = request.get_json(force=True)
    urgency = data.get("urgency","").lower()
    summary = data.get("summary","")
    if not urgency or not summary:
        return jsonify({"error": "Missing urgency or summary"}), 400
    sms_ok = send_sms(TELUS_CELL, summary)
    call_ok = True
    if urgency == "urgent":
        res = retell_call(f"+1{TELUS_CELL}", NOTIFIER_ID, {"summary": summary})
        call_ok = res["success"]
    if not sms_ok or not call_ok:
        send_email(KIRK_EMAIL, f"NOTIFICATION FAILED ({urgency})", f"SMS OK: {sms_ok}, Call OK: {call_ok}")
    return jsonify({"ok": True, "sms_ok": sms_ok, "call_triggered": urgency=="urgent" and call_ok})

@app.route('/activity-sms', methods=['POST'])
def activity_sms():
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
    # simple keyword detection
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
    sms_text = f"{emoji} {agent} {dur_str} — {caller}{company_str}\n→ {summary}\n→ Next: {next_step}"
    if len(sms_text) > 155:
        sms_text = sms_text[:152] + "..."
    sms_ok = send_sms(TELUS_CELL, sms_text)
    if not sms_ok:
        send_email(KIRK_EMAIL, f"Activity feed failed for {data.get('call_id','?')}", "Check Retell dashboard")
        return jsonify({"error": "SMS send failed"}), 500
    return jsonify({"ok": True, "sms": sms_text})

if __name__ == '__main__':
    logger.info(f"Starting on 0.0.0.0:{PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
