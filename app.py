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
            raw_msg = rjson.get("message") or rjson.get("error") or str(rjson)
            # Normalise GCC's vague errors into user-facing messages
            raw_lower = raw_msg.lower()
            if any(k in raw_lower for k in ("slot full", "seat", "capacity", "no seats", "not available")):
                user_msg = "Slot Full — no seats available for this date/time."
            elif "internal server error" in raw_lower or "exception" in raw_lower:
                user_msg = "GCC server error — try a different date or slot."
            else:
                user_msg = raw_msg
            update_slot(session_id, slot_key, status="error", error=user_msg)
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
        # Also persist order_id and amount_paise so /confirm can use them later
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
#
# After Razorpay payment, the frontend sends us:
#   razorpay_payment_id, razorpay_order_id, razorpay_signature
# We then call GCC's /book/api/Confirm with the same payload the real GCC
# website sends, so the booking is finalised in GCC's system and the user
# receives a WhatsApp confirmation.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/confirm/<session_id>/<slot_id>", methods=["POST"])
def confirm_payment(session_id, slot_id):
    """Called by frontend after Razorpay checkout completes successfully."""
    data = request.get_json(force=True)
    payment_id  = data.get("razorpay_payment_id", "")
    order_id    = data.get("razorpay_order_id", "")
    # signature  = data.get("razorpay_signature", "")  # not used by GCC's Confirm API

    # Read the slot to get tempBookId and amount from the existing session
    slot_snap = None
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        slot = session["slots"].get(str(slot_id))
        if not slot:
            return jsonify({"error": "Slot not found"}), 404
        import copy as _copy
        slot_snap = _copy.deepcopy(slot)

    temp_book_id   = slot_snap.get("tempBookId")
    amount_paise   = slot_snap.get("amount_paise", 0)
    # GCC Confirm API expects amount in rupees (integer), not paise
    amount_rupees  = int(amount_paise) // 100 if amount_paise else 0

    # If we don't have a stored order_id, fall back to whatever the frontend sent
    stored_order_id = slot_snap.get("order_id") or order_id

    # ── Call GCC's Confirm API ────────────────────────────────────────────────
    booking_id = None
    gcc_error  = None
    if temp_book_id:
        try:
            confirm_payload = {
                "payment_id": payment_id,
                "order_id":   stored_order_id,
                "status":     "paid",
                "id":         temp_book_id,
                "amount":     amount_rupees,
            }
            gcc_confirm_headers = dict(GCC_HEADERS)
            gcc_confirm_headers["content-type"] = "application/json"

            gcc_resp = http_requests.post(
                f"{GCC_BASE}/book/api/Confirm",
                json=confirm_payload,
                headers=gcc_confirm_headers,
                timeout=30,
            )
            gcc_resp.raise_for_status()

            # GCC returns the booking_id as a plain value (string/int) or "error"
            try:
                gcc_data = gcc_resp.json()
            except Exception:
                gcc_data = gcc_resp.text.strip()

            if gcc_data and gcc_data != "error":
                booking_id = str(gcc_data)
            else:
                gcc_error = "GCC returned an error on confirmation — check GCC account."

        except Exception as e:
            gcc_error = f"GCC Confirm call failed: {e}"
    else:
        gcc_error = "Missing tempBookId — cannot confirm with GCC."

    # ── Update local session state ────────────────────────────────────────────
    with sessions_lock:
        session = sessions.get(session_id)
        if session:
            slot = session["slots"].get(str(slot_id))
            if slot:
                slot["status"]       = "confirmed" if booking_id else "paid"
                slot["payment_id"]   = payment_id
                slot["booking_id"]   = booking_id
                slot["gcc_error"]    = gcc_error

    return jsonify({
        "status":     "ok",
        "booking_id": booking_id,
        "gcc_error":  gcc_error,
    })


# ─── Slot availability (real-time seats from GCC) ─────────────────────────────

@app.route("/slots", methods=["GET"])
def get_slot_availability():
    """
    Fetches available seat counts for a specific location, date, and slot.
    Query params: location (key), date (YYYY-MM-DD), slot_id (int)
    """
    location_key = request.args.get("location", "kolathur")
    date         = request.args.get("date", "")
    slot_id      = request.args.get("slot_id", "")

    if location_key not in LOCATIONS:
        return jsonify({"error": f"Invalid location. Choose: {list(LOCATIONS.keys())}"}), 400
    if not date:
        return jsonify({"error": "date is required (YYYY-MM-DD)"}), 400
    if not slot_id:
        return jsonify({"error": "slot_id is required"}), 400

    loc = LOCATIONS[location_key]

    try:
        avail_resp = http_requests.post(
            f"{GCC_BASE}/book/api/getAvailability",
            data={
                "catId":    loc["catId"],
                "buildId":  loc["buildId"],
                "subId":    loc["subId"],
                "fromDate": date,
                "toDate":   date,
                "slots[]": slot_id,
            },
            headers=GCC_HEADERS,
            timeout=15,
        )
        avail_resp.raise_for_status()
        avail_json = avail_resp.json()

        # GCC typically returns something like:
        # [{"slotId": 1, "availableSeats": 12, "totalSeats": 20, ...}]
        # Normalise into a consistent structure regardless of GCC's exact schema
        seats = None
        if isinstance(avail_json, list) and len(avail_json) > 0:
            first = avail_json[0]
            seats = first.get("availableSeats") or first.get("available") or first.get("seatsAvailable")
        elif isinstance(avail_json, dict):
            seats = avail_json.get("availableSeats") or avail_json.get("available")

        return jsonify({
            "location":        location_key,
            "date":            date,
            "slot_id":         slot_id,
            "available_seats": seats,
            "raw":             avail_json,
        })

    except Exception as e:
        return jsonify({"error": f"Failed to fetch availability: {e}"}), 502


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)
