"""Stripe signature verification on exact raw bytes; no unsigned ingress."""
import hashlib
import hmac
import json
import re
import time
from .engine import Problem

def verify(body, header, secret, now=None):
    if not secret or not secret.startswith('whsec_'):
        raise Problem('Configure a Stripe test webhook signing secret locally')
    if not isinstance(header, str) or len(header) > 2000:
        raise Problem('Missing or invalid Stripe signature')
    fields = {}
    for item in header.split(','):
        (key, sep, value) = item.partition('=')
        if not sep:
            raise Problem('Malformed Stripe signature')
        fields.setdefault(key, []).append(value)
    timestamps = fields.get('t', [])
    if len(timestamps) != 1 or not re.fullmatch('[0-9]{1,12}', timestamps[0]):
        raise Problem('Invalid Stripe timestamp')
    now = time.time() if now is None else now
    if abs(now - int(timestamps[0])) > 300:
        raise Problem('Stripe event timestamp outside acceptance window')
    digest = hmac.new(secret.encode(), timestamps[0].encode() + b'.' + body, hashlib.sha256).hexdigest()
    if not any((re.fullmatch('[a-f0-9]{64}', sig) and hmac.compare_digest(digest, sig) for sig in fields.get('v1', []))):
        raise Problem('Stripe signature mismatch')
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError):
        raise Problem('Invalid event JSON') from None
    if not isinstance(event, dict) or event.get('livemode') is not False:
        raise Problem('Only test-mode Stripe events are accepted')
    return event
