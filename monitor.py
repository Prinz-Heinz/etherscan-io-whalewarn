from dotenv import load_dotenv
import os, json, time, requests, datetime
from datetime import timezone
from collections import defaultdict, deque

# Load environment variables from .env
load_dotenv()

# ---------------- Config ----------------
STATE_FILE = "etherscan_state.json"
MARKET_CAP_CACHE_FILE = "market_cap_cache.json"

# Sensitive configuration loaded from environment
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL_OLD")
USER_IDS = os.getenv("USER_IDS", "").split(",") if os.getenv("USER_IDS") else []
ROLE_IDS = os.getenv("ROLE_IDS", "").split(",") if os.getenv("ROLE_IDS") else []

# Safety check
if not ETHERSCAN_API_KEY or not WEBHOOK_URL:
    raise ValueError("Missing ETHERSCAN_API_KEY or WEBHOOK_URL in environment variables. Please check your .env file.")

ITERATION_LIMIT = 10
LOOKBACK_BLOCKS = 500
SUMMARY_INTERVAL = 30 * 60  # 30 minutes, actually ~1 hour because polling rate is 10 seconds 30 * 60 * 10
MARKET_CAP_REFRESH_INTERVAL = 900
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
BLOCK_WINDOW = LOOKBACK_BLOCKS
SCAN_INTERVAL = ITERATION_LIMIT
SPIKE_MULTIPLIER = 3.0
MIN_ACTIVITY_USD = 150_000
activity_history = defaultdict(lambda: deque(maxlen=10))

last_summary_time = time.time()
summary_buffer = []

# ---------------- Tokens ----------------
TOKENS = {
    "ZRO":   "0x6985884c4392d348587b19cb9eaaF157f13271CD",
    "ZETA":  "0xf091867ec603a6628ed83d274e835539d82e9cc8",
    "W":     "0xb0ffa8000886e57f86dd5264b9582b2ad87b2b91",
    "DOT":   "0x7083609fce4d1d8dc0c979aab8c869ea2c873402",
    "GRT":   "0xc944e90c64b2c07662a292be6244bdf05cda44a7"
}

COINGECKO_IDS = {
    "ZRO": "layerzero",
    "ZETA": "zetachain",
    "W": "wormhole",
    "DOT": "polkadot",
    "GRT": "the-graph"
}

HOT_WALLETS = {
    w.lower() for w in [
        "0x3f5CE5FBFe3E9af3971dD833D26BA9b5C936f0bE",
        "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
        "0x5c985e89dde482efe97ea9f1950ad149eb73829e",
        "0x564286362092D8e7936f0549571a803B203aAceD",
        "0x689C56Aef474Df92D44A1B70850f808488F9769C",
        "0x503828976D22510aad0201ac7EC88293211D23Da",
        "0xF977814e90dA44bFA03b6295A0616a897441aceC",
        "0xea81f6b5e01d1f8f65e4a20594cc1c291515b0ad",
        "0x1bfeea540a0a01f7a3f86f3b23b46a00d5d4f13b",
        "0xDc76CD25977E0a5Ae17155770273aD58648900D3"
    ]
}

# ---------------- Utilities ----------------
def debug(msg):
    print(f"[DEBUG] {datetime.datetime.now().isoformat()} | {msg}")

if os.path.exists(MARKET_CAP_CACHE_FILE):
    with open(MARKET_CAP_CACHE_FILE, "r") as f:
        market_cap_cache = json.load(f)
else:
    market_cap_cache = {}

def is_external_to_hot(tx, token_contract_address=None):
    try:
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        if not to_addr or not from_addr:
            return False
        if to_addr not in HOT_WALLETS or from_addr in HOT_WALLETS:
            return False
        if from_addr == ZERO_ADDRESS or (token_contract_address and from_addr == token_contract_address.lower()):
            return False
        return True
    except Exception as e:
        debug(f"is_external_to_hot error: {e}")
        return False

def get_latest_block():
    try:
        url = f"https://api.etherscan.io/v2/api?chainid=1&module=proxy&action=eth_blockNumber&apikey={ETHERSCAN_API_KEY}"
        r = requests.get(url, timeout=10).json()
        if "result" in r:
            blk = int(r["result"], 16)
            debug(f"Fetched latest block {blk}")
            return blk
    except Exception as e:
        debug(f"get_latest_block failed: {e}")
    return 0

def calculate_dynamic_threshold(market_cap_usd, symbol=None):
    if market_cap_usd < 10_000_000:
        ratio, label = 0.005, "🔴 Very High"
    elif market_cap_usd < 100_000_000:
        ratio, label = 0.002, "🟠 High"
    elif market_cap_usd < 1_000_000_000:
        ratio, label = 0.001, "🟡 Medium"
    else:
        ratio, label = 0.0005, "🟢 Low"
    threshold_usd = market_cap_usd * ratio
    debug(f"{symbol} threshold {label} — cap ${market_cap_usd:,.0f} → ${threshold_usd:,.2f}")
    return threshold_usd

def get_market_cap(symbol):
    try:
        cid = COINGECKO_IDS.get(symbol)
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cid}", timeout=10)
        cap = r.json()["market_data"]["market_cap"]["usd"]
        debug(f"{symbol} market cap ${cap:,.0f}")
        return cap
    except Exception as e:
        debug(f"get_market_cap failed for {symbol}: {e}")
        return None

def get_safe_market_cap(symbol, fallback=50_000_000):
    try:
        cap = get_market_cap(symbol)
        if not cap or cap <= 0:
            raise ValueError("bad cap")
        market_cap_cache[symbol] = cap
        with open(MARKET_CAP_CACHE_FILE, "w") as f:
            json.dump(market_cap_cache, f)
        return cap
    except Exception as e:
        cached = market_cap_cache.get(symbol, fallback)
        debug(f"Using fallback for {symbol}: ${cached:,.0f} ({e})")
        return cached

def get_txns(address, start_block, end_block):
    try:
        url = (
            f"https://api.etherscan.io/v2/api?chainid=1"
            f"&module=account&action=tokentx&contractaddress={address}"
            f"&startblock={start_block}&endblock={end_block}"
            f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
        )
        r = requests.get(url, timeout=20).json()
        if r.get("status") == "1":
            debug(f"Fetched {len(r['result'])} txns for {address[:8]}")
            return r["result"]
    except Exception as e:
        debug(f"get_txns error {address[:8]}: {e}")
    return []

# ---------------- Alerts ----------------
def send_webhook_alert(alert, user_ids=USER_IDS, role_ids=ROLE_IDS):
    if not WEBHOOK_URL:
        debug(f"[DRY] Would send alert: {alert}")
        return

    tx_hash = alert.get("hash", "")
    etherscan_url = f"https://etherscan.io/tx/{tx_hash}" if tx_hash != "N/A" else None
    reason = alert.get("reason", "")
    sym = alert.get("token", "")
    color = 0x00BFFF
    title = f"ℹ️ Alert: {sym}"
    if "Exchange" in reason: title, color = f"🚨 Exchange Activity: {sym}", 0xFF0000
    elif "Large Transfer" in reason: title, color = f"⚠️ Large Transfer: {sym}", 0xFFA500
    elif "Activity Spike" in reason: title, color = f"📈 Activity Spike: {sym}", 0xFFD700

    fields = [
        {"name": "Token", "value": sym, "inline": True},
        {"name": "USD Value", "value": f"${alert.get('usd_value',0):,.2f}", "inline": True},
        {"name": "Block", "value": str(alert.get("block","N/A")), "inline": True},
        {"name": "Risk Score", "value": str(alert.get("risk_score","?")), "inline": True}
    ]

    extra = alert.get("extra", {})
    if extra.get("pct_change"):
        fields.append({"name": "Change", "value": f"{extra['pct_change']}%", "inline": True})
    if extra.get("top_tx_links_md"):
        fields.append({"name": "Top Transactions", "value": extra["top_tx_links_md"], "inline": False})
    if extra.get("sample_tx_links_md"):
        fields.append({"name": "Sample Tx Links", "value": extra["sample_tx_links_md"], "inline": False})
    if etherscan_url:
        fields.append({"name": "Transaction", "value": f"[View on Etherscan]({etherscan_url})", "inline": False})

    embed = {
        "title": title,
        "description": reason,
        "color": color,
        "fields": fields,
        "footer": {"text": ""},
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    }
    try:
        r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        debug(f"Webhook {r.status_code} {sym}: {r.text[:80]}")
    except Exception as e:
        debug(f"Webhook failed: {e}")

# ------------- Summary Reports -------------
def summarize_cycle(events):
    grouped = defaultdict(list)
    for e in events:
        grouped[e["token"]].append(e)

    if grouped:
        description = ""
        for sym, evs in grouped.items():
            total = sum(e["total_usd"] for e in evs)
            desc = "\n".join([f"• {e['type']}: {e['reason']}" for e in evs])
            description += f"**{sym}** — ${total:,.0f}\n{desc}\n\n"
    else:
        description = "**No alerts or anomalies detected**"

    embed = {
        "title": "📊 Summary Report",
        "description": description.strip(),
        "color": 0x00BFFF,
        "footer": {"text": "Hourly Summary"},
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    }

    try:
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
            debug("Summary report sent (including empty summary).")
        else:
            debug(f"[DRY] Summary would be sent:\n{description}")
    except Exception as e:
        debug(f"Summary send failed: {e}")

# ---------------- Startup ----------------
if not WEBHOOK_URL:
    debug(f"[DRY] Startup message skipped.")
else:
    startup_message = (
        f"✅ System Online.\n"
        f"Tracking: {', '.join(TOKENS.keys())}\n"
        f"Polling interval: {ITERATION_LIMIT}s\n"
        f"Startup: {datetime.datetime.now(timezone.utc).isoformat()}"
    )
    requests.post(WEBHOOK_URL, json={"content": startup_message})
    debug("Startup message sent to Discord.")

# ---------------- Main Loop ----------------
recent_tx_hashes = set()
while True:
    try:
        latest = get_latest_block()
        start_block = max(0, latest - BLOCK_WINDOW)
        end_block = latest

        for sym, addr in TOKENS.items():
            try:
                market_cap = get_safe_market_cap(sym)
                threshold = calculate_dynamic_threshold(market_cap, sym)
                txns = get_txns(addr, start_block, end_block)
                if not txns:
                    continue

                large_transfer_alerts = []
                external_hot_tx_links, external_hot_volume_usd = [], 0
                total_volume_usd = 0

                for tx in txns:
                    tx_hash = tx["hash"]
                    if tx_hash in recent_tx_hashes:
                        continue
                    recent_tx_hashes.add(tx_hash)

                    value_eth = int(tx["value"]) / 1e18
                    usd_value = value_eth * (market_cap / 1_000_000_000)
                    total_volume_usd += usd_value

                    if usd_value > threshold:
                        large_transfer_alerts.append({
                            "token": sym,
                            "type": "Large Transfer",
                            "reason": f"Single large transfer of ${usd_value:,.2f} to {tx['to']}",
                            "usd_value": usd_value,
                            "hash": tx_hash,
                            "block": tx["blockNumber"],
                            "risk_score": 8
                        })

                    if is_external_to_hot(tx, addr):
                        external_hot_tx_links.append((tx_hash, tx["from"], usd_value))
                        external_hot_volume_usd += usd_value

                if external_hot_volume_usd > threshold:
                    reason = f"Exchange inflow detected: ${external_hot_volume_usd:,.2f} > ${threshold:,.2f}"
                    tx_links_md = "\n".join([
                        f"[{h}](https://etherscan.io/tx/{h}) — `{frm}` — ${val:,.2f}"
                        for h, frm, val in external_hot_tx_links[:10]
                    ])
                    send_webhook_alert({
                        "token": sym,
                        "reason": reason,
                        "usd_value": external_hot_volume_usd,
                        "hash": "N/A",
                        "block": f"{start_block}-{end_block}",
                        "risk_score": 9,
                        "extra": {"sample_tx_links_md": tx_links_md}
                    })
                    summary_buffer.append({
                        "token": sym,
                        "type": "Exchange Inflow",
                        "reason": reason,
                        "event_count": len(external_hot_tx_links),
                        "total_usd": external_hot_volume_usd
                    })

                hist = activity_history[sym]
                avg_prev = sum(hist)/len(hist) if hist else 0
                hist.append(total_volume_usd)
                cap_threshold = threshold
                spike_threshold = max(cap_threshold, MIN_ACTIVITY_USD, avg_prev * SPIKE_MULTIPLIER)

                if len(hist) >= 3 and total_volume_usd > spike_threshold:
                    pct_change = (total_volume_usd / max(avg_prev, 1)) * 100 - 100
                    reason = (
                        f"Abnormal activity spike: ${total_volume_usd:,.2f} "
                        f"(avg ${avg_prev:,.2f}, threshold ${spike_threshold:,.2f}, +{pct_change:.1f}%)"
                    )
                    sorted_txns = sorted(txns, key=lambda x: int(x["value"]), reverse=True)
                    top_tx_links = []
                    for tx in sorted_txns[:3]:
                        h = tx["hash"]
                        v = int(tx["value"]) / 1e18
                        usd = v * (market_cap / 1_000_000_000)
                        sh, to = f"{h[:6]}…{h[-4:]}", tx["to"]
                        sto = f"{to[:6]}…{to[-4:]}"
                        top_tx_links.append(f"[{sh}](https://etherscan.io/tx/{h}) → `{sto}` (${usd:,.0f})")
                    top_tx_md = "\n".join(top_tx_links)
                    if len(top_tx_md) > 900:
                        top_tx_md = "; ".join([l.split('→')[0] for l in top_tx_links]) + " …"
                    send_webhook_alert({
                        "token": sym,
                        "reason": reason,
                        "usd_value": total_volume_usd,
                        "hash": "N/A",
                        "block": f"{start_block}-{end_block}",
                        "risk_score": 7,
                        "extra": {"pct_change": round(pct_change, 1), "top_tx_links_md": top_tx_md}
                    })
                    summary_buffer.append({
                        "token": sym,
                        "type": "Activity Spike",
                        "reason": reason,
                        "event_count": len(txns),
                        "total_usd": total_volume_usd
                    })

                for alert in large_transfer_alerts:
                    send_webhook_alert(alert)
                    summary_buffer.append({
                        "token": sym,
                        "type": alert["type"],
                        "reason": alert["reason"],
                        "event_count": 1,
                        "total_usd": alert["usd_value"]
                    })

            except Exception as token_err:
                debug(f"Token {sym} error: {token_err}")

        now_time = time.time()
        if now_time - last_summary_time >= SUMMARY_INTERVAL:
            debug(f"Sending summary ({len(summary_buffer)} events).")
            summarize_cycle(summary_buffer)
            summary_buffer.clear()
            last_summary_time = now_time
            debug("Summary sent.")
        else:
            remaining = int(SUMMARY_INTERVAL - (now_time - last_summary_time))
            debug(f"Next summary in ~{remaining}s")

        debug(f"Sleeping {SCAN_INTERVAL}s...\n")
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        debug(f"Main loop error: {e}")
        time.sleep(30)
