#!/usr/bin/env python3
"""Download NFTs held by public Solana and Ethereum wallet addresses.

Reads only public chain data and public metadata/media URLs (IPFS, Arweave,
HTTP). Never needs a private key, seed phrase, or wallet connection, and
never signs or sends a transaction.
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import requests

DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
DEFAULT_ETH_RPC = "https://ethereum-rpc.publicnode.com"
DEFAULT_ME_API = "https://api-mainnet.magiceden.dev"
HELIUS_RPC = "https://mainnet.helius-rpc.com/"
USER_AGENT = "nft-puller/1.0 (read-only; +https://github.com/TherapyToaster/nft-puller)"
MAX_FILE_BYTES = 512 * 1024 * 1024

IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://nftstorage.link/ipfs/",
    "https://w3s.link/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
]

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

EVM_CHAINS = {
    "ethereum": "eth.blockscout.com",
    "base": "base.blockscout.com",
    "polygon": "polygon.blockscout.com",
    "optimism": "optimism.blockscout.com",
    "arbitrum": "arbitrum.blockscout.com",
    "gnosis": "gnosis.blockscout.com",
    "zora": "explorer.zora.energy",
    "scroll": "scroll.blockscout.com",
}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT

# Name/collection: claim-style pitches and embedded URLs.
SPAM_NAME = re.compile(
    r"(claim|visit\b|reward|voucher|redeem|airdrop|free mint|\bgiveaway\b|"
    r"https?://|www\.|\.(?:com|io|xyz|app|site|net|org|live|fun|gift)\b)",
    re.I,
)
# Description must combine a pitch word with a URL to be flagged.
SPAM_DESC_WORD = re.compile(r"\b(claim|visit|redeem|voucher)\b", re.I)
SPAM_DESC_URL = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|io|xyz|app|site|net|org|live|fun|gift)\b)",
    re.I,
)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def split_addrs(value: str) -> List[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def looks_like_eth(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""))


def looks_like_sol(addr: str) -> bool:
    # Base58, typical Solana public-key length. Not a checksum check.
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr or ""))


def safe_name(value: Any, length: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "")).strip(" .")
    return (text[:length] or "unnamed")


def ipfs_candidates(url: str) -> List[str]:
    if not url:
        return []
    url = url.strip()
    if url.startswith("data:"):
        return [url]
    if url.startswith("ipfs://"):
        path = url[7:]
        if path.startswith("ipfs/"):
            path = path[5:]
        return [gateway + path for gateway in IPFS_GATEWAYS]
    if url.startswith("ar://"):
        return ["https://arweave.net/" + url[5:]]
    match = re.search(r"/ipfs/(.+)$", url)
    if match and url.startswith("http"):
        extras = [gateway + match.group(1) for gateway in IPFS_GATEWAYS if not url.startswith(gateway)]
        return [url] + extras
    match = re.match(
        r"https?://(bafy[a-z0-9]+|Qm[A-Za-z0-9]+)\.ipfs\.[^/]+/?(.*)$",
        url,
    )
    if match:
        cid, rest = match.group(1), match.group(2)
        suffix = ("/" + rest) if rest else ""
        extras = [gateway + cid + suffix for gateway in IPFS_GATEWAYS]
        return [url] + extras
    return [url]


def fetch_bytes(url: str, max_bytes: int = MAX_FILE_BYTES) -> Tuple[bytes, str, str]:
    """Return (body, content_type, url_used) or raise with attempted errors."""
    errors: List[str] = []
    for candidate in ipfs_candidates(url):
        if candidate.startswith("data:"):
            header, _, data = candidate.partition(",")
            content_type = header[5:].split(";")[0] or "text/plain"
            if ";base64" in header:
                import base64

                body = base64.b64decode(data)
            else:
                body = unquote(data).encode()
            return body, content_type, "data-uri"
        for _attempt in range(2):
            try:
                response = SESSION.get(
                    candidate,
                    timeout=(15, 90),
                    stream=True,
                    allow_redirects=True,
                )
                if response.status_code != 200:
                    errors.append(f"{candidate[:70]} HTTP {response.status_code}")
                    response.close()
                    if response.status_code in (400, 403, 404, 410):
                        break
                    time.sleep(1.5)
                    continue
                buf = bytearray()
                for chunk in response.iter_content(1 << 16):
                    buf += chunk
                    if len(buf) > max_bytes:
                        response.close()
                        raise RuntimeError("file exceeds size cap")
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
                return bytes(buf), content_type, candidate
            except Exception as exc:
                errors.append(f"{candidate[:70]} {type(exc).__name__}: {str(exc)[:80]}")
                time.sleep(1.5)
    raise RuntimeError(" | ".join(errors[-4:]) or "no url")


def file_extension(body: bytes, content_type: str, url: str) -> str:
    if body.startswith(b"\x89PNG"):
        return ".png"
    if body.startswith(b"GIF8"):
        return ".gif"
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith(b"RIFF") and body[8:12] == b"WAVE":
        return ".wav"
    if body.startswith(b"RIFF") and body[8:12] == b"AVI ":
        return ".avi"
    if body.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if body.startswith(b"glTF"):
        return ".glb"
    if body.startswith(b"ID3"):
        return ".mp3"
    if body.startswith(b"OggS"):
        return ".ogg"
    if body.startswith(b"%PDF"):
        return ".pdf"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        brand = body[8:12]
        if brand.startswith(b"qt"):
            return ".mov"
        if brand in (b"avif", b"avis"):
            return ".avif"
        return ".mp4"
    head = body.lstrip()[:200]
    if head.startswith((b"<?xml", b"<svg ", b"<svg")) or b"<svg" in body[:500]:
        return ".svg"
    mapping = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/avif": ".avif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "model/gltf-binary": ".glb",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "text/html": ".html",
        "application/json": ".json",
    }
    if content_type in mapping:
        return mapping[content_type]
    match = re.search(r"\.([A-Za-z0-9]{2,5})(?:\?|$)", url or "")
    if match and match.group(1).lower() not in ("json", "io", "xyz", "com", "link", "net"):
        return "." + match.group(1).lower()
    return mimetypes.guess_extension(content_type or "") or ".bin"


def spam_reason(name: str, collection: str, description: str) -> str:
    hit = SPAM_NAME.search(f"{name} {collection}")
    if hit:
        return f"suspicious term in name/collection: '{hit.group(0)}'"
    if SPAM_DESC_WORD.search(description or "") and SPAM_DESC_URL.search(description or ""):
        return "claim/visit-style description with URL"
    return ""


def rpc_post(url: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    response = SESSION.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if "error" in data and data["error"]:
        raise RuntimeError(str(data["error"]))
    return data


def helius_url() -> Optional[str]:
    key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not key:
        return None
    return f"{HELIUS_RPC}?api-key={key}"


def solana_rpc() -> str:
    return (os.environ.get("SOLANA_RPC_URL") or DEFAULT_SOLANA_RPC).strip()


def eth_rpc() -> str:
    return (os.environ.get("ETH_RPC_URL") or DEFAULT_ETH_RPC).strip()


def list_solana_das(owner: str, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    endpoint = helius_url()
    if not endpoint:
        return [], "no HELIUS_API_KEY; skipped DAS (compressed NFTs may be missed)"
    items: List[Dict[str, Any]] = []
    page = 1
    page_size = 1000 if limit is None else min(1000, max(1, limit))
    while True:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAssetsByOwner",
            "params": {
                "ownerAddress": owner,
                "page": page,
                "limit": page_size,
                "displayOptions": {
                    "showCollectionMetadata": True,
                    "showUnverifiedCollections": True,
                },
            },
        }
        data = rpc_post(endpoint, payload)
        batch = ((data.get("result") or {}).get("items")) or []
        items.extend(batch)
        total = (data.get("result") or {}).get("total")
        if limit is not None and len(items) >= limit:
            items = items[:limit]
            break
        if not batch:
            break
        if total is not None and len(items) >= int(total):
            break
        if len(batch) < page_size:
            break
        page += 1
        time.sleep(0.2)
    return items, None


def list_solana_magic_eden(owner: str, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    items: List[Dict[str, Any]] = []
    offset = 0
    page_size = 500 if limit is None else min(500, max(1, limit))
    base = os.environ.get("MAGIC_EDEN_API") or DEFAULT_ME_API
    try:
        while True:
            url = f"{base}/v2/wallets/{owner}/tokens"
            response = SESSION.get(url, params={"offset": offset, "limit": page_size}, timeout=60)
            if response.status_code == 429:
                time.sleep(3)
                response = SESSION.get(url, params={"offset": offset, "limit": page_size}, timeout=60)
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if limit is not None and len(items) >= limit:
                items = items[:limit]
                break
            if len(batch) < page_size:
                break
            offset += page_size
            time.sleep(0.4)
        return items, None
    except Exception as exc:
        return items, f"Magic Eden list failed: {exc}"


def list_solana_token_accounts(owner: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            data = rpc_post(
                solana_rpc(),
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        owner,
                        {"programId": program},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            for account in (data.get("result") or {}).get("value") or []:
                info = account["account"]["data"]["parsed"]["info"]
                amount = info["tokenAmount"]
                out.append(
                    {
                        "program": program,
                        "mint": info["mint"],
                        "amount": amount["amount"],
                        "decimals": amount["decimals"],
                        "state": info.get("state"),
                    }
                )
        except Exception as exc:
            errors.append(f"{program}: {exc}")
    return out, ("; ".join(errors) if errors else None)


def das_item_to_record(asset: Dict[str, Any], wallet: str) -> Dict[str, Any]:
    grouping = [g for g in asset.get("grouping") or [] if g.get("group_key") == "collection"]
    collection_meta = (grouping[0].get("collection_metadata") or {}) if grouping else {}
    metadata = (asset.get("content") or {}).get("metadata") or {}
    links = (asset.get("content") or {}).get("links") or {}
    notes: List[str] = []
    if asset.get("interface") == "MplCoreCollection":
        notes.append("MplCoreCollection account owned by wallet (collection itself, not an item)")
    owner = ((asset.get("ownership") or {}).get("owner")) or ""
    if owner and owner != wallet:
        notes.append(
            "on-chain owner is not this wallet; an indexer still listed it "
            "here (often an active marketplace listing)"
        )
    if (asset.get("compression") or {}).get("compressed"):
        notes.append("compressed NFT")
    if grouping and grouping[0].get("verified") is False:
        notes.append("unverified collection")
    return {
        "wallet": wallet,
        "chain": "solana",
        "collection": collection_meta.get("name") or "",
        "coll_addr": grouping[0]["group_value"] if grouping else "",
        "name": metadata.get("name") or "",
        "tid": asset.get("id") or "",
        "metadata_url": (asset.get("content") or {}).get("json_uri") or "",
        "fallback_meta": metadata,
        "fallback_image": links.get("image") or "",
        "fallback_anim": links.get("animation_url") or "",
        "record": asset,
        "note": "; ".join(notes),
        "interface": asset.get("interface") or "",
    }


def me_item_to_record(item: Dict[str, Any], wallet: str) -> Dict[str, Any]:
    collection = item.get("collectionName") or item.get("collection") or ""
    if isinstance(collection, dict):
        collection = collection.get("name") or collection.get("symbol") or ""
    coll_addr = item.get("collectionAddress") or ""
    raw_collection = item.get("collection")
    if not coll_addr and isinstance(raw_collection, str) and len(raw_collection) > 20:
        coll_addr = raw_collection
    notes = []
    if item.get("isCompressed"):
        notes.append("compressed NFT (Magic Eden)")
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    return {
        "wallet": wallet,
        "chain": "solana",
        "collection": collection or "",
        "coll_addr": coll_addr,
        "name": item.get("name") or "",
        "tid": item.get("mintAddress") or item.get("tokenAddress") or item.get("mint") or "",
        "metadata_url": item.get("tokenExternalURL") or "",
        "fallback_meta": {
            "name": item.get("name"),
            "attributes": item.get("attributes") or [],
            "properties": properties,
        },
        "fallback_image": item.get("image") or "",
        "fallback_anim": properties.get("animation_url") or "",
        "record": item,
        "note": "; ".join(notes) or "source: Magic Eden wallet API",
        "interface": "compressed" if item.get("isCompressed") else "magiceden",
    }


def list_solana(wallet: str, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    das_items, das_warn = list_solana_das(wallet, limit=limit)
    if das_warn:
        warnings.append(das_warn)
    for asset in das_items:
        record = das_item_to_record(asset, wallet)
        if record["tid"]:
            by_id[record["tid"]] = record
        if limit is not None and len(by_id) >= limit:
            return list(by_id.values())[:limit], warnings

    me_items, me_warn = list_solana_magic_eden(wallet, limit=limit)
    if me_warn:
        warnings.append(me_warn)
    missing_for_das: List[str] = []
    for item in me_items:
        mint = item.get("mintAddress") or item.get("tokenAddress") or item.get("mint") or ""
        if not mint:
            continue
        if mint in by_id:
            continue
        missing_for_das.append(mint)
        by_id[mint] = me_item_to_record(item, wallet)

    # If we have Helius, try to upgrade Magic Eden-only mints to full DAS records.
    endpoint = helius_url()
    if endpoint and missing_for_das:
        for start in range(0, len(missing_for_das), 100):
            batch = missing_for_das[start : start + 100]
            try:
                data = rpc_post(
                    endpoint,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAssetBatch",
                        "params": {"ids": batch},
                    },
                )
                for asset in data.get("result") or []:
                    if not asset or not asset.get("id"):
                        continue
                    by_id[asset["id"]] = das_item_to_record(asset, wallet)
            except Exception as exc:
                warnings.append(f"getAssetBatch failed: {exc}")
                break
            time.sleep(0.2)

    token_accounts, token_warn = list_solana_token_accounts(wallet)
    if token_warn:
        warnings.append(token_warn)
    nft_like = [
        row
        for row in token_accounts
        if row.get("decimals") == 0 and str(row.get("amount")) not in ("0", "")
    ]
    for row in nft_like:
        mint = row["mint"]
        if mint in by_id:
            continue
        by_id[mint] = {
            "wallet": wallet,
            "chain": "solana",
            "collection": "",
            "coll_addr": "",
            "name": "",
            "tid": mint,
            "metadata_url": "",
            "fallback_meta": {},
            "fallback_image": "",
            "fallback_anim": "",
            "record": row,
            "note": "token account only; metadata indexer did not list this mint",
            "interface": "spl-token",
        }
        if limit is not None and len(by_id) >= limit:
            break

    records = list(by_id.values())
    if limit is not None:
        records = records[:limit]
    return records, warnings


def decode_eth_string(result_hex: str) -> Optional[str]:
    if not result_hex or result_hex in ("0x", "0x0") or len(result_hex) < 130:
        return None
    body = bytes.fromhex(result_hex[2:])
    length = int.from_bytes(body[32:64], "big")
    return body[64 : 64 + length].decode(errors="replace")


def eth_call_token_uri(contract: str, token_id: str, token_type: str) -> Optional[str]:
    try:
        token_int = int(token_id)
    except (TypeError, ValueError):
        return None
    arg = hex(token_int)[2:].rjust(64, "0")
    if (token_type or "").upper() in ("ERC-1155", "ERC1155"):
        data = "0x0e89341c" + arg
    else:
        data = "0xc87b56dd" + arg
    try:
        response = SESSION.post(
            eth_rpc(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": contract, "data": data}, "latest"],
            },
            timeout=30,
        )
        response.raise_for_status()
        uri = decode_eth_string((response.json() or {}).get("result") or "")
        if uri and (token_type or "").upper() in ("ERC-1155", "ERC1155"):
            uri = uri.replace("{id}", arg)
        return uri
    except Exception:
        return None


def list_evm(wallet: str, chain: str, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    host = EVM_CHAINS[chain]
    items: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {}
    error = None
    for _ in range(80):
        try:
            response = SESSION.get(
                f"https://{host}/api/v2/addresses/{wallet}/nft",
                params=params,
                timeout=40,
            )
            if response.status_code == 404:
                error = "404 (no address record on this chain)"
                break
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            error = str(exc)[:200]
            break
        items.extend(data.get("items") or [])
        if limit is not None and len(items) >= limit:
            items = items[:limit]
            break
        nxt = data.get("next_page_params")
        if not nxt:
            break
        params = nxt
        time.sleep(0.5)

    records: List[Dict[str, Any]] = []
    for item in items:
        token = item.get("token") or {}
        token_id = str(item.get("id") or "")
        addr = token.get("address_hash") or token.get("address") or ""
        token_type = item.get("token_type") or token.get("type") or ""
        metadata = item.get("metadata") or {}
        records.append(
            {
                "wallet": wallet,
                "chain": chain,
                "collection": token.get("name") or "",
                "coll_addr": addr,
                "name": (metadata.get("name") if isinstance(metadata, dict) else None) or "",
                "tid": token_id,
                "metadata_url": item.get("_tokenURI") or "",
                "fallback_meta": metadata if isinstance(metadata, dict) else {},
                "fallback_image": (
                    (metadata.get("image") if isinstance(metadata, dict) else None)
                    or item.get("image_url")
                    or ""
                ),
                "fallback_anim": (
                    (metadata.get("animation_url") if isinstance(metadata, dict) else None)
                    or item.get("animation_url")
                    or ""
                ),
                "record": item,
                "note": f"{token_type} balance {item.get('value')}; Blockscout reputation={token.get('reputation')}",
                "interface": token_type,
            }
        )
    return records, error


def unique_dir(root: Path, used: set, chain: str, collection: str, name: str, tid: str, is_spam: bool) -> Path:
    base = root / chain
    if is_spam:
        base = base / "_spam" / safe_name(collection)
    else:
        base = base / safe_name(collection)
    leaf = safe_name(name or tid, 60)
    path = base / leaf
    key = str(path)
    if key in used:
        path = base / f"{leaf}__{safe_name(tid, 48)}"
    used.add(str(path))
    return path



def process_item(item: Dict[str, Any], out_root: Path, used: set, lock: Any) -> Dict[str, Any]:
    errors: List[str] = []
    if item.get("chain") in EVM_CHAINS and item.get("coll_addr") and item.get("tid") and not item.get("metadata_url"):
        uri = eth_call_token_uri(item["coll_addr"], str(item["tid"]), item.get("interface") or "")
        if uri:
            item["metadata_url"] = uri
    meta = None
    meta_status = "ok"
    if item.get("metadata_url"):
        try:
            body, _ct, _url = fetch_bytes(item["metadata_url"], max_bytes=20 * 1024 * 1024)
            meta = json.loads(body.decode("utf-8-sig"))
        except Exception as exc:
            errors.append(f"metadata fetch failed: {str(exc)[:300]}")
            meta_status = "fail"
    if meta is None:
        meta = item.get("fallback_meta") or {}
        if meta and item.get("metadata_url"):
            errors.append("used indexer-cached metadata instead")
        elif not item.get("metadata_url"):
            errors.append(
                "no metadata URI on-chain; used indexer metadata"
                if meta
                else "no metadata URI and no indexer metadata"
            )
    if not isinstance(meta, dict):
        meta = {"_raw": meta}

    name = meta.get("name") or item.get("name") or ""
    item["name"] = name
    if not item.get("collection"):
        collection = meta.get("collection")
        if isinstance(collection, dict):
            collection = collection.get("name")
        if not collection and name:
            collection = re.sub(r"\s*#\s*\d+\s*$", "", name).strip()
        if not collection:
            collection = (
                "_collection_accounts"
                if item.get("interface") == "MplCoreCollection"
                else "_no_collection"
            )
        item["collection"] = collection

    reason = spam_reason(name, item.get("collection") or "", meta.get("description") or "")
    item["is_spam"] = bool(reason)
    item["spam_reason"] = reason

    image = meta.get("image") or meta.get("image_url") or item.get("fallback_image") or ""
    animation = meta.get("animation_url") or item.get("fallback_anim") or ""
    item["media_url"] = image
    item["anim_url"] = animation

    with lock:
        dest = unique_dir(
            out_root,
            used,
            item["chain"],
            item["collection"],
            name,
            item.get("tid") or "",
            item["is_spam"],
        )
        dest.mkdir(parents=True, exist_ok=True)
    item["dir"] = str(dest)
    (dest / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    record = item.get("record") or {}
    (dest / "chain_record.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    files: List[str] = []
    image_ok = True
    animation_ok = True
    for kind, url in (("image", image), ("animation", animation)):
        if not url:
            if kind == "image":
                image_ok = False
                errors.append("no image URL in metadata")
            continue
        try:
            body, content_type, used_url = fetch_bytes(url)
            if (
                content_type == "text/html"
                and kind == "image"
                and b"<svg" not in body[:2000]
            ):
                raise RuntimeError(f"got HTML page instead of media from {used_url[:80]}")
            filename = f"{kind}{file_extension(body, content_type, url)}"
            (dest / filename).write_bytes(body)
            files.append(f"{filename} ({len(body)} B)")
        except Exception as exc:
            errors.append(f"{kind} download failed: {str(exc)[:300]}")
            if kind == "image":
                image_ok = False
            else:
                animation_ok = False

    item["files"] = files
    if image_ok and animation_ok and meta_status == "ok":
        status = "ok"
    elif image_ok or files:
        status = "partial"
    else:
        status = "failed"
    item["status"] = status
    item["error"] = "; ".join(errors)
    print(
        f"{status:7} {item['chain']:10} {str(item['collection'])[:24]:24} "
        f"{str(name)[:28]:28} | {item['error'][:120]}",
        flush=True,
    )
    return item


def write_manifest(rows: List[Dict[str, Any]], out_root: Path) -> Path:
    path = out_root / "manifest.csv"
    columns = [
        "wallet",
        "chain",
        "collection",
        "name",
        "token_id_or_mint",
        "contract_or_collection_address",
        "metadata_url",
        "media_url",
        "animation_url",
        "is_spam",
        "spam_reason",
        "download_status",
        "error",
        "note",
        "local_path",
        "files",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in sorted(
            rows,
            key=lambda item: (
                item.get("chain") or "",
                bool(item.get("is_spam")),
                (item.get("collection") or "").lower(),
                item.get("name") or "",
            ),
        ):
            rel = os.path.relpath(row.get("dir") or "", out_root)
            writer.writerow(
                [
                    row.get("wallet") or "",
                    row.get("chain") or "",
                    row.get("collection") or "",
                    row.get("name") or "",
                    row.get("tid") or "",
                    row.get("coll_addr") or "",
                    row.get("metadata_url") or "",
                    row.get("media_url") or "",
                    row.get("anim_url") or "",
                    str(bool(row.get("is_spam"))).lower(),
                    row.get("spam_reason") or "",
                    row.get("status") or "",
                    row.get("error") or "",
                    row.get("note") or "",
                    rel,
                    "; ".join(row.get("files") or []),
                ]
            )
    return path


def make_zip(out_root: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for file_path in sorted(out_root.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, arcname=str(file_path.relative_to(out_root)))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download NFTs held by public Solana and Ethereum wallets into a "
            "folder and zip. Read-only; no private key required."
        )
    )
    parser.add_argument(
        "--sol",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="Solana wallet address. Repeat for multiple wallets.",
    )
    parser.add_argument(
        "--eth",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="Ethereum (or EVM) wallet address. Repeat for multiple wallets.",
    )
    parser.add_argument("--out", default="output", help="Output folder (default: output)")
    parser.add_argument("--zip", dest="zip_path", default="output.zip", help="Zip path (default: output.zip)")
    parser.add_argument("--no-zip", action="store_true", help="Skip writing a zip archive")
    parser.add_argument("--limit", type=int, default=None, help="Download at most N items")
    parser.add_argument("--workers", type=int, default=8, help="Parallel downloads (default: 8)")
    parser.add_argument(
        "--evm-chain",
        action="append",
        default=[],
        metavar="NAME",
        help="EVM chain to scan in addition to ethereum, or 'all'. Repeatable.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file (default: .env). Missing files are ignored.",
    )
    return parser.parse_args(argv)


def resolve_chains(flags: List[str]) -> List[str]:
    if not flags:
        return ["ethereum"]
    chains: List[str] = []
    for flag in flags:
        name = flag.strip().lower()
        if name == "all":
            for chain in EVM_CHAINS:
                if chain not in chains:
                    chains.append(chain)
            continue
        if name not in EVM_CHAINS:
            raise SystemExit(
                f"Unknown EVM chain '{flag}'. Choose from: {', '.join(EVM_CHAINS)} or all."
            )
        if name not in chains:
            chains.append(name)
    return chains


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_dotenv(Path(args.env_file))

    sol_wallets = list(args.sol) + split_addrs(os.environ.get("SOLANA_WALLETS") or "")
    eth_wallets = list(args.eth) + split_addrs(os.environ.get("ETHEREUM_WALLETS") or "")
    sol_wallets = list(dict.fromkeys(sol_wallets))
    eth_wallets = list(dict.fromkeys(eth_wallets))

    if not sol_wallets and not eth_wallets:
        print(
            "Pass at least one wallet with --sol and/or --eth, "
            "or set SOLANA_WALLETS / ETHEREUM_WALLETS.",
            file=sys.stderr,
        )
        return 2

    for addr in sol_wallets:
        if not looks_like_sol(addr):
            print(f"Does not look like a Solana address: {addr}", file=sys.stderr)
            return 2
    for addr in eth_wallets:
        if not looks_like_eth(addr):
            print(f"Does not look like an Ethereum address: {addr}", file=sys.stderr)
            return 2

    try:
        evm_chains = resolve_chains(args.evm_chain)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    if not eth_wallets:
        evm_chains = []

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    items: List[Dict[str, Any]] = []

    def remaining() -> Optional[int]:
        if args.limit is None:
            return None
        return max(0, args.limit - len(items))

    for wallet in sol_wallets:
        if remaining() == 0:
            break
        print(f"Listing Solana NFTs for {wallet} ...", flush=True)
        found, warnings = list_solana(wallet, limit=remaining())
        for warning in warnings:
            print(f"  note: {warning}", flush=True)
        print(f"  found {len(found)} Solana item(s)", flush=True)
        items.extend(found)

    for wallet in eth_wallets:
        for chain in evm_chains:
            if remaining() == 0:
                break
            print(f"Listing {chain} NFTs for {wallet} ...", flush=True)
            found, error = list_evm(wallet, chain, limit=remaining())
            if error:
                print(f"  note: {error}", flush=True)
            print(f"  found {len(found)} {chain} item(s)", flush=True)
            items.extend(found)
        if remaining() == 0:
            break

    total = len(items)
    if args.limit is not None:
        items = items[: max(0, args.limit)]
        print(f"Limiting download to {len(items)} of {total} listed item(s).", flush=True)
    else:
        print(f"Downloading {total} item(s).", flush=True)

    used: set = set()
    lock = threading.Lock()
    results: List[Dict[str, Any]] = []
    workers = max(1, int(args.workers))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process_item, item, out_root, used, lock) for item in items]
        for future in as_completed(futures):
            results.append(future.result())

    for row in results:
        row.pop("record", None)
        row.pop("fallback_meta", None)

    manifest = write_manifest(results, out_root)
    ok = sum(1 for row in results if row.get("status") == "ok")
    partial = sum(1 for row in results if row.get("status") == "partial")
    failed = sum(1 for row in results if row.get("status") == "failed")
    spam = sum(1 for row in results if row.get("is_spam"))
    print(
        f"Done. {len(results)} item(s): {ok} ok, {partial} partial, {failed} failed, {spam} flagged as spam."
    )
    print(f"Manifest: {manifest}")

    if not args.no_zip:
        zip_path = Path(args.zip_path).resolve()
        make_zip(out_root, zip_path)
        print(f"Zip: {zip_path} ({zip_path.stat().st_size} bytes)")
    return 0



if __name__ == "__main__":
    sys.exit(main())
