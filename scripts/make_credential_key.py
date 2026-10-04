"""Creates the secret that encrypts members' API keys.  Run:  python scripts/make_credential_key.py
Put the printed line in your .env AND in Railway's Variables. Keep a copy somewhere safe: if it is lost, members must
enter their API keys again. Never share it and never upload it to GitHub."""
from cryptography.fernet import Fernet

print("\nAdd this line to .env and to Railway Variables:\n")
print("CREDENTIAL_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("\nKeep it secret. If you lose it, saved member keys can't be decrypted (members just set them again).\n")
