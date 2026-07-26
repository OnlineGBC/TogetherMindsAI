# Local secrets (SOPS + Google Cloud KMS)

Local secrets live **encrypted** in `sops.env`. There is **no plaintext `.env`** on
the laptop — the values are decrypted into memory only when you run the app. This
keeps secrets safe at rest (HIPAA-friendly hardening). Production is unaffected:
Cloud Run reads secrets from Google Secret Manager, not from any `.env`.

## One-time setup on a new machine
1. Install SOPS: `winget install --id SecretsOPerationS.SOPS -e`
2. Sign in so SOPS can use the KMS key:
   `gcloud auth application-default login`  (as an account with access to the key)

## Everyday use
- **Run the app** (secrets injected into memory, never written to disk):
  ```
  sops exec-env sops.env 'python TogetherMindsAI.py'
  ```
- **Edit / add a secret** (opens decrypted in your editor, re-encrypts on save):
  ```
  sops sops.env
  ```
- **View a value** if you must:
  ```
  sops -d sops.env
  ```

## Notes
- Tests don't need this — pytest uses its own dummy env vars.
- Running `python TogetherMindsAI.py` directly (without `sops exec-env`) will fail
  fast on a missing `SECRET_KEY` — that's the reminder to use the wrapper.
- `sops.env` is gitignored (kept local). It's encrypted, so it *can* be committed
  if you want a versioned backup; the key resource is in `.sops.yaml`.
- The KMS key: `projects/togethermindsai-python/locations/us-central1/keyRings/sops/cryptoKeys/env`
