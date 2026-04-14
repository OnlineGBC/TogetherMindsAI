"""
migrate_encrypt_messages.py
---------------------------
One-off migration: encrypts all existing plaintext ChatMessage.text rows
using the FIELD_ENCRYPTION_KEY defined in .env (or the environment).

Run once after deploying the EncryptedType change to models.py:
    python migrate_encrypt_messages.py

WARNING: Never rotate FIELD_ENCRYPTION_KEY without running a re-encryption
migration first — rotating the key without re-encrypting makes all existing
messages permanently unreadable.

Safe to re-run: already-encrypted rows are skipped (Fernet ciphertext starts
with 'gAAAAA', plaintext rows do not).
"""

import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from cryptography.fernet import Fernet, InvalidToken

FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")
if not FIELD_ENCRYPTION_KEY:
    print("ERROR: FIELD_ENCRYPTION_KEY is not set. Check your .env file.")
    sys.exit(1)

fernet = Fernet(FIELD_ENCRYPTION_KEY.encode())

# Import app and models after env is loaded
import config
from models import db, ChatMessage, init_encryption

# Temporarily skip IS_TESTING guard so we can use the app context
os.environ["TESTING"] = "1"
import TogetherMindsAI as app_module
app = app_module.app

init_encryption(FIELD_ENCRYPTION_KEY)

encrypted = 0
skipped = 0
errors = 0

with app.app_context():
    messages = ChatMessage.query.all()
    print(f"Found {len(messages)} messages to process...")

    for msg in messages:
        raw = msg.text
        if not raw:
            skipped += 1
            continue

        # Check if already encrypted: Fernet tokens are base64url and start with 'gAAAAA'
        if isinstance(raw, str) and raw.startswith("gAAAAA"):
            skipped += 1
            continue

        try:
            # Write back — EncryptedType will encrypt on assignment
            msg.text = raw
            encrypted += 1
        except Exception as exc:
            print(f"  ERROR on message id={msg.id}: {exc}")
            errors += 1

    if encrypted > 0:
        db.session.commit()
        print(f"Done. Encrypted: {encrypted}  |  Already encrypted (skipped): {skipped}  |  Errors: {errors}")
    else:
        print(f"Nothing to encrypt. Already encrypted (skipped): {skipped}  |  Errors: {errors}")
