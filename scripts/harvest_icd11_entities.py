"""
scripts/harvest_icd11_entities.py
---------------------------------
One-time (re-runnable) dev tool — NOT part of the app runtime.

Resolves each ICD-11 code in clinical_data/icd_corpus.json to its WHO Foundation
entity ID via the official ICD-API, then writes a true per-code deep link into the
entry's `icd11_url`:

    https://icd.who.int/browse/latest-release/mms/en#<entityId>

Auth: OAuth2 client-credentials using WHO_ICD_ClientId / WHO_ICD_ClientSecret from
.env (never committed). Run again whenever WHO ships a new annual release.

Usage:
    python scripts/harvest_icd11_entities.py            # show resolved table only
    python scripts/harvest_icd11_entities.py --write    # also update the corpus
"""

import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORPUS_PATH = os.path.join(ROOT, "clinical_data", "icd_corpus.json")

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
ID_BASE = "https://id.who.int"
BROWSE_TMPL = "https://icd.who.int/browse/latest-release/mms/en#{entity_id}"

load_dotenv(os.path.join(ROOT, ".env"))


def get_token() -> str:
    cid = os.environ.get("WHO_ICD_ClientId")
    secret = os.environ.get("WHO_ICD_ClientSecret")
    if not cid or not secret:
        sys.exit("Missing WHO_ICD_ClientId / WHO_ICD_ClientSecret in .env")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "scope": "icdapi_access",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(url: str, token: str) -> dict:
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "en",
            "API-Version": "v2",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def latest_release(token: str) -> str:
    """Return the latest MMS release id, e.g. '2024-01'."""
    data = api_get(f"{ID_BASE}/icd/release/11/mms", token)
    latest = data.get("latestRelease", "")           # .../icd/release/11/2024-01/mms
    m = re.search(r"/release/11/([^/]+)/mms", latest)
    return m.group(1) if m else "2024-01"


def _id_tail(uri):
    """Trailing numeric id of a WHO entity / linearization URI."""
    m = re.search(r"/(\d+)(?:[/?#].*)?$", uri or "")
    return m.group(1) if m else ""


def first_code(icd11):
    """Pick the lookup code from the corpus value (handles ranges like '6A70-6A71')."""
    return re.split(r"[-\s]", icd11.strip())[0]


def resolve_entity_id(icd11, release, token):
    """Resolve an ICD-11 MMS code to a WHO Foundation entity id.

    codeinfo gives the linearization stem; the stem entity carries a
    foundationReference we prefer (the browser keys off the foundation id).
    Falls back to the stem's own id. Returns "" if nothing resolves.
    """
    code = first_code(icd11)
    try:
        info = api_get(f"{ID_BASE}/icd/release/11/{release}/mms/codeinfo/{code}?flexible=true", token)
    except requests.HTTPError:
        return ""
    stem_uri = info.get("stemId") or info.get("@id") or ""
    if not stem_uri:
        return ""
    try:
        entity = api_get(stem_uri, token)
        foundation = entity.get("foundationReference") or entity.get("source") or ""
        return _id_tail(foundation) or _id_tail(stem_uri)
    except requests.HTTPError:
        return _id_tail(stem_uri)


def main():
    write = "--write" in sys.argv
    token = get_token()
    release = latest_release(token)
    print(f"Latest MMS release: {release}\n")

    with open(CORPUS_PATH, "r", encoding="utf-8") as fh:
        corpus = json.load(fh)

    resolved, missing = {}, []
    print(f"{'id':<12}{'code':<12}{'entityId':<14}url")
    print("-" * 80)
    for e in corpus["entries"]:
        icd11 = e.get("icd11", "")
        if not icd11:
            continue
        eid = resolve_entity_id(icd11, release, token)
        if eid:
            url = BROWSE_TMPL.format(entity_id=eid)
            resolved[e["id"]] = url
            print(f"{e['id']:<12}{icd11:<12}{eid:<14}{url}")
        else:
            missing.append(e["id"])
            print(f"{e['id']:<12}{icd11:<12}{'(unresolved)':<14}")

    if missing:
        print(f"\nUnresolved ({len(missing)}): {', '.join(missing)}")

    if write and resolved:
        for e in corpus["entries"]:
            if e["id"] in resolved:
                e["icd11_url"] = resolved[e["id"]]
        with open(CORPUS_PATH, "w", encoding="utf-8") as fh:
            json.dump(corpus, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"\nWrote {len(resolved)} icd11_url deep links into {CORPUS_PATH}")
    elif not write:
        print("\n(dry run — pass --write to update the corpus)")


if __name__ == "__main__":
    main()
