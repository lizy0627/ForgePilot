"""Self-contained keyset boundaries survive deletion; legacy row IDs remain readable."""
import base64
import binascii
from datetime import datetime
import json
from uuid import UUID

from .errors import ForgePilotError


def encode_cursor(created_at, identity):
    value=json.dumps([created_at,identity],separators=(',',':')).encode()
    return 'p1.'+base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode_cursor(value):
    if not value.startswith('p1.'):
        return None
    try:
        raw=value[3:]
        pair=json.loads(base64.b64decode(raw+'='*(-len(raw)%4),altchars=b'-_',validate=True))
        if not isinstance(pair,list) or len(pair)!=2 or not all(isinstance(v,str) for v in pair):
            raise ValueError()
        if datetime.fromisoformat(pair[0]).tzinfo is None or str(UUID(pair[1]))!=pair[1]:
            raise ValueError()
        return pair
    except (ValueError,TypeError,binascii.Error):
        raise ForgePilotError('invalid_page','历史分页游标无效，请刷新列表。') from None
