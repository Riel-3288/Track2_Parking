import hashlib, hmac, json, requests

def compute_signature(data: dict) -> str:
    keys = sorted(k for k in data.keys() if k != "Signature")
    joined = "|".join(str(data[k]) for k in keys)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()

base_payload = {
    "EventClass": "penalty",
    "Reason": "demo_test",
    "FineAmount": "5.00",
    "EventId": "demo-0001",
    "SequenceId": 9001,
    "ServerDateTime": "2026-09-20 08:00:00",
}
valid_sig = compute_signature(base_payload)

URL = "http://127.0.0.1:5000/webhook"

# missing_signature
requests.post(URL, json=base_payload)

# bad_signature
bad_payload = {**base_payload, "Signature": "0" * 32}
requests.post(URL, json=bad_payload)

# Signature present but not verified (wrong signature)
#    Change FineAmount to 500.00 but keep signature for 5.00
tampered_payload = {**base_payload, "FineAmount": "500.00", "Signature": valid_sig}
requests.post(URL, json=tampered_payload)

print("valid signature would have been:", valid_sig)