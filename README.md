# nft-puller

Download every NFT held by one or more **public** Solana and Ethereum wallet
addresses, then package the media and metadata into a zip. Files are organized
by chain and collection, with obvious airdropped spam kept in a separate
`_spam` folder.

**This tool only reads public data.** It never needs a private key, seed
phrase, or wallet connection, and it never signs or sends a transaction.

## What it does

1. Lists NFTs held by the wallet addresses you pass in:
   - **Solana:** Magic Eden's public wallet API, optional [Helius](https://www.helius.dev)
     DAS (`getAssetsByOwner`, recommended for compressed NFTs), and
     `getTokenAccountsByOwner` on a public RPC as a supplement.
   - **Ethereum:** the public [Blockscout](https://www.blockscout.com) NFT API,
     plus an `eth_call` for each token's on-chain metadata URI. Other EVM
     chains (Base, Polygon, and similar) can be included with `--evm-chain`.
2. Fetches each NFT's metadata JSON and original image or animation, trying
   several IPFS gateways plus Arweave and HTTP when needed.
3. Writes a `manifest.csv` with chain, collection, name, token id or mint,
   source URLs, spam flag, and download status. Failed items (dead links,
   unreachable gateways, missing metadata) are listed instead of hidden.
4. Builds a zip of the output folder.

## Install

Python 3.8 or newer:

```bash
git clone https://github.com/TherapyToaster/nft-puller.git
cd nft-puller
python3 -m pip install -r requirements.txt
```

For Solana compressed NFTs, copy `.env.example` to `.env` and add a free
Helius API key. Ethereum and uncompressed Solana listings work without one.

```bash
cp .env.example .env
# edit .env and set HELIUS_API_KEY=...
```

## Usage

Wallet addresses come from CLI flags or environment variables. Use your own
addresses, not the placeholders below.

```bash
# One Solana wallet and one Ethereum wallet
python3 nft_puller.py \
  --sol ExAmPLeSoLanaWallet11111111111111111111111 \
  --eth 0x1234567890abcdef1234567890abcdef12345678

# Several wallets on one chain
python3 nft_puller.py \
  --sol ExAmPLeSoLanaWallet11111111111111111111111 \
  --sol AnotherSoLanaWallet2222222222222222222222

# Ethereum only, first 10 items (useful for a test run)
python3 nft_puller.py \
  --eth 0x1234567890abcdef1234567890abcdef12345678 \
  --limit 10

# Also scan other EVM chains via Blockscout
python3 nft_puller.py \
  --eth 0x1234567890abcdef1234567890abcdef12345678 \
  --evm-chain base --evm-chain polygon

# Or every built-in EVM chain
python3 nft_puller.py \
  --eth 0x1234567890abcdef1234567890abcdef12345678 \
  --evm-chain all
```

You can also set `SOLANA_WALLETS` and `ETHEREUM_WALLETS` (comma-separated) in
`.env` instead of passing `--sol` / `--eth`.

### Options

| Flag | Default | Purpose |
|---|---|---|
| `--sol ADDRESS` | (none) | Solana wallet. Repeat the flag for more than one. |
| `--eth ADDRESS` | (none) | Ethereum (or EVM) wallet. Repeat the flag for more than one. |
| `--out DIR` | `output` | Folder for media, metadata, and the manifest. |
| `--zip PATH` | `output.zip` | Zip path. Use `--no-zip` to skip the archive. |
| `--limit N` | (none) | Download at most N items (across all wallets). |
| `--workers N` | 8 | Parallel media downloads. |
| `--evm-chain NAME` | `ethereum` | Extra EVM chains to scan, or `all`. |
| `--no-zip` | off | Write the output folder only. |

### Environment variables

| Variable | Purpose |
|---|---|
| `HELIUS_API_KEY` | Optional. Enables Solana DAS, including compressed NFTs. |
| `SOLANA_RPC_URL` | Optional. Defaults to the public mainnet RPC. |
| `ETH_RPC_URL` | Optional. Defaults to a public Ethereum RPC. |
| `SOLANA_WALLETS` | Optional comma-separated Solana addresses. |
| `ETHEREUM_WALLETS` | Optional comma-separated Ethereum addresses. |

Do not commit `.env`. API keys are secrets. Wallet addresses in `.env` are
optional convenience only; they are still public addresses, never private keys.

## Output structure

```
output/
  manifest.csv
  solana/
    Collection Name/
      NFT Name/
        metadata.json
        chain_record.json
        image.png
        animation.mp4      # only when the metadata has an animation
    _spam/
      Suspicious Drop/
        ...
  ethereum/
    Collection Name/
      NFT Name/
        ...
    _spam/
      ...
output.zip
```

`manifest.csv` columns: `wallet`, `chain`, `collection`, `name`,
`token_id_or_mint`, `contract_or_collection_address`, `metadata_url`,
`media_url`, `animation_url`, `is_spam`, `spam_reason`, `download_status`,
`error`, `note`, `local_path`, `files`.

## Spam detection

Obvious airdrops are moved under `_spam/` rather than mixed in with the rest.
An item is flagged when:

- The name or collection looks like a claim, reward, voucher, redeem, airdrop,
  or free-mint pitch, or it contains a URL or domain.
- The description uses claim/visit/redeem/voucher language **and** includes a
  URL.

This is heuristic, not perfect. Check `_spam/` before deleting anything, and
check the manifest's `spam_reason` column.

## Limits and caveats

- **Read-only public data.** If an indexer or gateway is down, that NFT may
  land in the manifest as `failed` or `partial`. The tool does not hide those.
- **Compressed Solana NFTs** are easy to miss without a Helius DAS key.
  Magic Eden coverage helps, but DAS is the more complete source.
- **Public RPCs and gateways rate-limit.** The script retries and backs off;
  very large wallets can take a while.
- **Other EVM chains** are opt-in. By default, `--eth` only scans Ethereum.
- **Media size.** Each file is capped at 512 MB by default.
- The tool lists tokens the address currently holds according to public
  indexers. It cannot see NFTs in some escrow or staking programs, and a
  marketplace listing may show a different on-chain owner. Those cases are
  noted in the manifest.

## License

MIT, see [LICENSE](LICENSE).
