import uuid
import threading
import copy
import requests as http_requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Configuration ────────────────────────────────────────────────────────────

LOCATIONS = {
    "kolathur":      {"buildId": 1, "catId": 1,  "subId": 1,  "label": "Kolathur"},
    "periyar_nagar": {"buildId": 2, "catId": 4,  "subId": 8,  "label": "Periyar Nagar"},
    "jawahar_nagar": {"buildId": 4, "catId": 7,  "subId": 13, "label": "Jawahar Nagar"},
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
    with sessions_lock:
        sessions[session_id]["slots"][str(slot_id)].update(kwargs)


def read_session(session_id):
    with sessions_lock:
        s = sessions.get(session_id)
        return copy.deepcopy(s) if s else None


# ─── Booking flow (one thread per slot) ──────────────────────────────────────
#
# This automates the GCC form-filling process:
#   Step 1: Reserve the slot (saveInTemp)
#   Step 2: Create the Razorpay order (create_order)
#
# After these two steps, the frontend uses Razorpay checkout.js to handle
# payment — exactly like the GCC website does when you click "Pay Now".
# ─────────────────────────────────────────────────────────────────────────────

def booking_flow(session_id, slot_key, location, date, name, phone, num_seats, real_slot_id):
    loc = LOCATIONS[location]

    # ── Step 1: Reserve slot (automates form submission) ─────────────
    update_slot(session_id, slot_key, status="reserving")
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
            "slots[]":    real_slot_id,
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
            msg = rjson.get("message", str(rjson))
            update_slot(session_id, slot_key, status="error", error=msg)
            return

        temp_book_id = rjson["tempBookId"]
        amount = rjson.get("amount", 0)
        update_slot(session_id, slot_key, amount=amount, tempBookId=temp_book_id)

    except Exception as e:
        update_slot(session_id, slot_key, status="error", error=f"Reservation failed: {e}")
        return

    # ── Step 2: Create Razorpay order (automates "Pay Now" click) ────
    update_slot(session_id, slot_key, status="creating_order")
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

        # Ready for payment — frontend will open Razorpay checkout.js
        update_slot(session_id, slot_key,
                    status="ready_to_pay",
                    order_id=order_id,
                    amount_paise=amount_paise,
                    razorpay_key=RAZORPAY_KEY)

    except Exception as e:
        update_slot(session_id, slot_key, status="error", error=f"Order creation failed: {e}")
        return


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

    dates     = data.get("dates")
    slot_ids  = data.get("slot_ids")
    location  = data.get("location", "kolathur")
    name      = data.get("name", "Shriram")
    phone     = data.get("phone", "8825743347")
    num_seats = data.get("num_seats", 2)

    if not dates or not isinstance(dates, list) or len(dates) == 0:
        return jsonify({"error": "dates must be a non-empty list"}), 400
    if not slot_ids or not isinstance(slot_ids, list) or len(slot_ids) == 0:
        return jsonify({"error": "slot_ids must be a non-empty list"}), 400
    if location not in LOCATIONS:
        return jsonify({"error": f"Invalid location. Choose from: {list(LOCATIONS.keys())}"}), 400

    session_id = str(uuid.uuid4())

    with sessions_lock:
        sessions[session_id] = {"slots": {}}
        for d in dates:
            for sid in slot_ids:
                slot_key = f"{d}_{sid}"
                sessions[session_id]["slots"][slot_key] = {
                    "slot_id":       slot_key,
                    "slot_label":    f"{d} — {SLOT_LABELS.get(sid, f'Slot {sid}')}",
                    "status":        "pending",
                    "amount":        None,
                    "amount_paise":  None,
                    "order_id":      None,
                    "razorpay_key":  None,
                    "error":         None,
                }

    for d in dates:
        for sid in slot_ids:
            slot_key = f"{d}_{sid}"
            t = threading.Thread(
                target=booking_flow,
                args=(session_id, slot_key, location, d, name, phone, num_seats, sid),
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


# ─── Confirm payment (called by frontend after Razorpay success) ─────────────

@app.route("/confirm/<session_id>/<slot_id>", methods=["POST"])
def confirm_payment(session_id, slot_id):
    """Called by frontend after Razorpay checkout completes successfully."""
    data = request.get_json(force=True)
    payment_id = data.get("razorpay_payment_id", "")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        slot = session["slots"].get(str(slot_id))
        if not slot:
            return jsonify({"error": "Slot not found"}), 404
        slot["status"] = "paid"
        slot["payment_id"] = payment_id

    return jsonify({"status": "ok"})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)
