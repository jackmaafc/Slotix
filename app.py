import uuid
import threading
import io
import base64
import time
import requests as http_requests
import qrcode
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Configuration ────────────────────────────────────────────────────────────

LOCATIONS = {
    "kolathur":      {"buildId": 1, "catId": 1,  "subId": 1,  "label": "Kolathur"},
    "periyar_nagar": {"buildId": 2, "catId": 4,  "subId": 8,  "label": "Periyar Nagar"},
    "jawahar_nagar": {"buildId": 4, "catId": 7,  "subId": 13, "label": "Jawahar Nagar"},
    # To add a new centre, add a new entry here with its buildId, catId, subId and label
    # These values can be found by capturing a HAR from the GCC booking website
}

SLOT_LABELS = {
    1: "06:00 AM – 09:30 AM",
    2: "10:00 AM – 01:30 PM",
    3: "02:00 PM – 05:30 PM",
    4: "06:00 PM – 09:30 PM",
    5: "10:00 PM – 11:00 PM",
}

RAZORPAY_KEY = "rzp_live_ySI5Ns54Y7qOcJ"

GCC_BASE = "https://gccservices.in/muthalvarpadaippagam"

GCC_HEADERS = {
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://gccservices.in",
    "referer": "https://gccservices.in/muthalvarpadaippagam/book",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

# ─── Thread-safe session store ────────────────────────────────────────────────

sessions = {}
sessions_lock = threading.Lock()


def update_slot(session_id, slot_id, **kwargs):
    """Thread-safe update of a slot's fields."""
    with sessions_lock:
        slot = sessions[session_id]["slots"][str(slot_id)]
        slot.update(kwargs)


def read_session(session_id):
    """Thread-safe read of a full session."""
    with sessions_lock:
        session = sessions.get(session_id)
        if session is None:
            return None
        # Return a deep-ish copy so the response isn't affected by concurrent writes
        import copy
        return copy.deepcopy(session)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def generate_qr_base64(data_string):
    """Generate a QR code PNG and return it as a base64 string."""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# ─── Booking flow (one thread per slot) ──────────────────────────────────────

def booking_flow(session_id, slot_id, location, date, name, phone, num_seats):
    loc = LOCATIONS[location]

    # ── Step 1: Reserve slot ──────────────────────────────────────────
    update_slot(session_id, slot_id, status="reserving")
    try:
        reserve_data = {
            "catId":      loc["catId"],
            "buildId":    loc["buildId"],
            "subId":      loc["subId"],
            "noOfPeople": num_seats,
            "fromDate":   date,
            "toDate":     date,
            "userName":   name,
            "userMobile": phone,
            "slots[]":    slot_id,
        }
        resp = http_requests.post(
            f"{GCC_BASE}/book/api/saveInTemp",
            data=reserve_data,
            headers=GCC_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        rjson = resp.json()

        if rjson.get("status") != "SUCCESS":
            update_slot(session_id, slot_id,
                        status="error",
                        error=f"Reservation failed: {rjson}")
            return

        temp_book_id = rjson["tempBookId"]
        amount = rjson.get("amount", 0)
        update_slot(session_id, slot_id, amount=amount)

    except Exception as e:
        update_slot(session_id, slot_id,
                    status="error",
                    error=f"Reservation error: {str(e)}")
        return

    # ── Step 2: Create Razorpay order ─────────────────────────────────
    update_slot(session_id, slot_id, status="creating_order")
    try:
        order_resp = http_requests.post(
            f"{GCC_BASE}/book/api/create_order",
            data={"id": temp_book_id},
            headers=GCC_HEADERS,
            timeout=30,
        )
        order_resp.raise_for_status()
        ojson = order_resp.json()
        order_id = ojson["id"]
        amount_paise = ojson["amount"]

    except Exception as e:
        update_slot(session_id, slot_id,
                    status="error",
                    error=f"Order creation error: {str(e)}")
        return

    # ── Step 3: Create UPI payment ────────────────────────────────────
    update_slot(session_id, slot_id, status="creating_payment")
    try:
        payment_data = {
            "description":    "Co-workspace Transaction",
            "notes[address]": "Greater Chennai Corporation.",
            "contact":        f"+91{phone}",
            "currency":       "INR",
            "amount":         amount_paise,
            "order_id":       order_id,
            "method":         "upi",
            "_[flow]":        "intent",
            "upi[flow]":      "intent",
            "_[upiqr]":       "1",
            "_[library]":     "checkoutjs",
            "_[platform]":    "browser",
        }
        pay_resp = http_requests.post(
            f"https://api.razorpay.com/v1/standard_checkout/payments/create/ajax?key_id={RAZORPAY_KEY}",
            data=payment_data,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        pay_resp.raise_for_status()
        pjson = pay_resp.json()

        intent_url = pjson["data"]["intent_url"]
        payment_id = pjson["payment_id"]
        status_url = pjson["request"]["url"]

        qr_b64 = generate_qr_base64(intent_url)

        update_slot(session_id, slot_id,
                    status="awaiting_payment",
                    qr_base64=qr_b64,
                    paymentId=payment_id,
                    statusUrl=status_url)

    except Exception as e:
        update_slot(session_id, slot_id,
                    status="error",
                    error=f"Payment creation error: {str(e)}")
        return

    # ── Step 4: Poll for payment ──────────────────────────────────────
    for _ in range(120):
        time.sleep(5)
        try:
            poll_resp = http_requests.get(status_url, timeout=15)
            poll_json = poll_resp.json()
            poll_status = poll_json.get("status", "").lower()

            if poll_status in ("captured", "authorized"):
                update_slot(session_id, slot_id, status="paid")
                return
            elif poll_status == "failed":
                update_slot(session_id, slot_id,
                            status="failed",
                            error="Payment failed")
                return
        except Exception:
            continue  # keep polling on transient errors

    update_slot(session_id, slot_id,
                status="timeout",
                error="Payment polling timed out after 10 minutes")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/locations", methods=["GET"])
def get_locations():
    return jsonify([
        {"key": k, "label": v["label"]}
        for k, v in LOCATIONS.items()
    ])


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True)

    date     = data.get("date")
    slot_ids = data.get("slot_ids")
    location = data.get("location", "kolathur")
    name     = data.get("name", "Shriram")
    phone    = data.get("phone", "8825743347")
    num_seats = data.get("num_seats", 2)

    if not date:
        return jsonify({"error": "date is required"}), 400
    if not slot_ids or not isinstance(slot_ids, list):
        return jsonify({"error": "slot_ids must be a non-empty list"}), 400
    if location not in LOCATIONS:
        return jsonify({"error": f"Invalid location. Choose from: {list(LOCATIONS.keys())}"}), 400

    session_id = str(uuid.uuid4())

    with sessions_lock:
        sessions[session_id] = {
            "slots": {
                str(sid): {
                    "slot_id":   sid,
                    "slot_label": SLOT_LABELS.get(sid, f"Slot {sid}"),
                    "status":    "pending",
                    "qr_base64": None,
                    "amount":    None,
                    "error":     None,
                }
                for sid in slot_ids
            }
        }

    for sid in slot_ids:
        t = threading.Thread(
            target=booking_flow,
            args=(session_id, sid, location, date, name, phone, num_seats),
            daemon=True,
        )
        t.start()

    return jsonify({"session_id": session_id})


@app.route("/status/<session_id>", methods=["GET"])
def status(session_id):
    session = read_session(session_id)
    if session is None:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(session)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)
