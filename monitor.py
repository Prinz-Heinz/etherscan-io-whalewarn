#!/usr/bin/env python3
"""
monitor.py
Etherscan-based early warning monitor with:
- inflow-only large transfer detection (pings)
- exchange inflow detection (pings)
- bounce/round-trip suppression
- activity spike detection on inflows only
- normal spikes are quiet; extreme spikes ping
- summary always posted (even if no events)
- environment-driven config (.env) with comma-separated USER_IDS and ROLE_IDS
"""

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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# Comma-separated lists supported
USER_IDS = [s.strip() for s in os.getenv("USER_IDS", "").split(",") if s.strip()]
ROLE_IDS = [s.strip() for s in os.getenv("ROLE_IDS", "").split(",") if s.strip()]

# Safety check (we allow DRY run with no webhook)
if not ETHERSCAN_API_KEY:
    raise ValueError("Missing ETHERSCAN_API_KEY in environment variables. Please check your .env file.")

ITERATION_LIMIT = 10
LOOKBACK_BLOCKS = 500
SUMMARY_INTERVAL = 12 * 60 * 60  # Summary every 12 hours
MARKET_CAP_REFRESH_INTERVAL = 900
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

BLOCK_WINDOW = LOOKBACK_BLOCKS
SCAN_INTERVAL = ITERATION_LIMIT

# Spike multipliers
SPIKE_MULTIPLIER = 3.0  # historical average multiplier used for history-based baseline (kept for compatibility)
MIN_ACTIVITY_USD = 150_000
activity_history = defaultdict(lambda: deque(maxlen=10))

# New thresholds for tiered spike behavior
SPIKE_THRESHOLD_MULTIPLIER = 3.0   # normal spike must exceed dynamic threshold * 3
SPIKE_PING_MULTIPLIER = 6.0        # extreme spike must exceed dynamic threshold * 6 to ping

# Bounce tolerance (percent)
BOUNCE_TOLERANCE_PCT = 1.0  # 1% by default

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
    try:
        with open(MARKET_CAP_CACHE_FILE, "r") as f:
            market_cap_cache = json.load(f)
    except (json.JSONDecodeError, ValueError):
        debug("market_cap_cache.json was empty or corrupt — resetting.")
        market_cap_cache = {}
else:
    market_cap_cache = {}

def is_external_to_hot(tx, token_contract_address=None):
    try:
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        if not to_addr or not from_addr:
            return False
        # External -> hot: to is hot, from is not hot, and not genesis
        if to_addr not in HOT_WALLETS:
            return False
        if from_addr in HOT_WALLETS:
            return False
        if from_addr == ZERO_ADDRESS:
            return False
        if token_contract_address and from_addr == token_contract_address.lower():
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
        if not cid:
            return None
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
    """
    Send an alert to the configured webhook. Provide per-call user_ids / role_ids
    to control mentions (passed as content).
    """
    if not WEBHOOK_URL:
        debug(f"[DRY] Would send alert: {json.dumps(alert, default=str)}")
        return

    tx_hash = alert.get("hash", "")
    etherscan_url = f"https://etherscan.io/tx/{tx_hash}" if tx_hash and tx_hash != "N/A" else None
    reason = alert.get("reason", "")
    sym = alert.get("token", "")
    color = 0x00BFFF
    title = f"ℹ️ Alert: {sym}"
    if "Exchange" in reason:
        title, color = f"🚨 Exchange Activity: {sym}", 0xFF0000
    elif "Large Transfer" in reason:
        title, color = f"⚠️ Large Transfer: {sym}", 0xFFA500
    elif "Activity Spike" in reason or "spike" in reason.lower():
        title, color = f"📈 Activity Spike: {sym}", 0xFFD700

    # --- Build embed ---
    fields = [
        {"name": "Token", "value": sym or "Unknown", "inline": True},
        {"name": "USD Value", "value": f"${alert.get('usd_value',0):,.2f}", "inline": True},
        {"name": "Block", "value": str(alert.get("block","N/A")), "inline": True},
        {"name": "Risk Score", "value": str(alert.get("risk_score","?")), "inline": True}
    ]

    extra = alert.get("extra", {})
    if extra.get("pct_change") is not None:
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
        "footer": {"text": "Etherscan Early Warning Monitor"},
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    }

    # Build mention content (Discord will expand these identifiers)
    mentions = []
    if user_ids:
        mentions += [f"<@{uid}>" for uid in user_ids if uid and uid.strip()]
    if role_ids:
        mentions += [f"<@&{rid}>" for rid in role_ids if rid and rid.strip()]

    payload = {"embeds": [embed]}
    if mentions:
        payload["content"] = " ".join(mentions)

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        debug(f"Webhook {r.status_code} {sym}: {r.text[:120]}")
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
        "footer": {"text": "Summary of Activities"},
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
    try:
        requests.post(WEBHOOK_URL, json={"content": startup_message}, timeout=10)
        debug("Startup message sent to Discord.")
    except Exception as e:
        debug(f"Startup webhook failed: {e}")

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

                # ---- Parse txns and dedupe by hash ----
                parsed = []
                for tx in txns:
                    try:
                        tx_hash = tx.get("hash")
                        if not tx_hash:
                            continue
                        if tx_hash in recent_tx_hashes:
                            continue
                        # mark seen
                        recent_tx_hashes.add(tx_hash)

                        from_addr = tx.get("from", "").lower()
                        to_addr = tx.get("to", "").lower()
                        blocknum = int(tx.get("blockNumber", 0))
                        # Some token transfer endpoints put token amount in "value" (works here)
                        value_eth = int(tx.get("value", 0)) / 1e18
                        usd_value = value_eth * (market_cap / 1_000_000_000)

                        parsed.append({
                            "hash": tx_hash,
                            "from": from_addr,
                            "to": to_addr,
                            "block": blocknum,
                            "value_eth": value_eth,
                            "usd": usd_value,
                            "raw": tx
                        })
                    except Exception as e:
                        debug(f"TX parse error: {e}")
                        continue

                # ---- Partition inflows (external -> hot) and outflows (hot -> external) ----
                inflows = []
                outflows = []
                for p in parsed:
                    if p["to"] in HOT_WALLETS and p["from"] not in HOT_WALLETS and p["from"] != ZERO_ADDRESS:
                        inflows.append(p)
                    elif p["from"] in HOT_WALLETS and p["to"] not in HOT_WALLETS:
                        outflows.append(p)
                    # other directions ignored for inflow accounting

                # ---- Bounce / round-trip suppression within this scan ----
                bounce_ignored_hashes = set()
                out_by_target = defaultdict(list)
                for o in outflows:
                    out_by_target[o["to"]].append(o)

                for inf in inflows:
                    origin = inf["from"]
                    candidates = out_by_target.get(origin, [])
                    matched = None
                    for o in candidates:
                        # return should be at or after inflow block
                        if o["block"] < inf["block"]:
                            continue
                        # only consider returns within block window to avoid long-term matches
                        if o["block"] - inf["block"] > BLOCK_WINDOW:
                            continue
                        # amount similarity tolerance
                        if abs(o["usd"] - inf["usd"]) / max(inf["usd"], 1) * 100 <= BOUNCE_TOLERANCE_PCT:
                            matched = o
                            break
                    if matched:
                        bounce_ignored_hashes.add(inf["hash"])
                        bounce_ignored_hashes.add(matched["hash"])
                        debug(f"[BOUNCE] Matched bounce: IN {inf['hash']} and OUT {matched['hash']} (~${inf['usd']:,.2f})")

                # ---- Compute inflow totals excluding bounces ----
                external_hot_tx_links = []
                external_hot_volume_usd = 0.0
                for inf in inflows:
                    if inf["hash"] in bounce_ignored_hashes:
                        debug(f"[BOUNCE] Ignoring inflow {inf['hash']} from {inf['from']} (${inf['usd']:,.2f})")
                        continue
                    external_hot_tx_links.append((inf["hash"], inf["from"], inf["usd"]))
                    external_hot_volume_usd += inf["usd"]

                total_inflow_usd = external_hot_volume_usd
                # keep total churn for debugging if needed
                total_volume_usd = sum(p["usd"] for p in parsed)

                # ---- Large transfer alerts (INFLOW ONLY) ----
                large_transfer_alerts = []
                for inf in inflows:
                    if inf["hash"] in bounce_ignored_hashes:
                        continue
                    if inf["usd"] > threshold:
                        large_transfer_alerts.append({
                            "token": sym,
                            "type": "Large Transfer",
                            "reason": f"Single large transfer of ${inf['usd']:,.2f} to {inf['to']}",
                            "usd_value": inf['usd'],
                            "hash": inf['hash'],
                            "block": inf['block'],
                            "risk_score": 8
                        })

                # ---- Exchange inflow detection (inflow-only) ----
                if external_hot_volume_usd > threshold:
                    reason = f"Exchange inflow detected: ${external_hot_volume_usd:,.2f} > ${threshold:,.2f}"
                    tx_links_md = "\n".join([
                        f"[{h}](https://etherscan.io/tx/{h}) — `{frm}` — ${val:,.2f}"
                        for h, frm, val in external_hot_tx_links[:10]
                    ])
                    # Exchange inflow is considered suspicious — ping
                    send_webhook_alert({
                        "token": sym,
                        "reason": reason,
                        "usd_value": external_hot_volume_usd,
                        "hash": "N/A",
                        "block": f"{start_block}-{end_block}",
                        "risk_score": 9,
                        "extra": {"sample_tx_links_md": tx_links_md}
                    }, user_ids=USER_IDS, role_ids=ROLE_IDS)

                    summary_buffer.append({
                        "token": sym,
                        "type": "Exchange Inflow",
                        "reason": reason,
                        "event_count": len(external_hot_tx_links),
                        "total_usd": external_hot_volume_usd
                    })

                # ---- Activity spike detection (inflow-only; tiered pings) ----
                hist = activity_history[sym]
                avg_prev = sum(hist)/len(hist) if hist else 0
                hist.append(total_inflow_usd)

                cap_threshold = threshold
                # Dynamic tiered thresholds
                normal_spike_threshold = max(cap_threshold * SPIKE_THRESHOLD_MULTIPLIER, MIN_ACTIVITY_USD, avg_prev * SPIKE_MULTIPLIER)
                extreme_spike_threshold = cap_threshold * SPIKE_PING_MULTIPLIER

                spike_type = None
                if total_inflow_usd > extreme_spike_threshold:
                    spike_type = "extreme"
                elif total_inflow_usd > normal_spike_threshold:
                    spike_type = "normal"

                if len(hist) >= 3 and spike_type:
                    pct_change = (total_inflow_usd / max(avg_prev, 1)) * 100 - 100
                    reason = (
                        f"Abnormal activity spike: ${total_inflow_usd:,.2f} "
                        f"(avg ${avg_prev:,.2f}, threshold ${normal_spike_threshold:,.2f}, +{pct_change:.1f}%)"
                    )

                    # create top tx md from inflows only
                    sorted_inflows = sorted([i for i in inflows if i["hash"] not in bounce_ignored_hashes], key=lambda x: x["usd"], reverse=True)
                    top_tx_links = []
                    for tx in sorted_inflows[:5]:
                        tx_hash = tx["hash"]
                        usd_val = tx["usd"]
                        to_addr = tx["to"]
                        top_tx_links.append(f"[{tx_hash}](https://etherscan.io/tx/{tx_hash}) — ${usd_val:,.2f} → {to_addr[:10]}")

                    top_tx_md = "\n".join(top_tx_links)
                    if len(top_tx_md) > 900:
                        top_tx_md = "; ".join([l.split('—')[0] for l in top_tx_links]) + " …"

                    # Normal spikes: silent (no mention), Extreme spikes: ping
                    spike_user_ids = USER_IDS if spike_type == "extreme" else []
                    spike_role_ids = ROLE_IDS if spike_type == "extreme" else []

                    send_webhook_alert({
                        "token": sym,
                        "reason": reason,
                        "usd_value": total_inflow_usd,
                        "hash": "N/A",
                        "block": f"{start_block}-{end_block}",
                        "risk_score": 7 if spike_type == "normal" else 9,
                        "extra": {"pct_change": round(pct_change, 1), "top_tx_links_md": top_tx_md}
                    }, user_ids=spike_user_ids, role_ids=spike_role_ids)

                    summary_buffer.append({
                        "token": sym,
                        "type": "Activity Spike",
                        "reason": reason,
                        "event_count": len(inflows),
                        "total_usd": total_inflow_usd
                    })

                # ---- Send large-transfer alerts (inflow-only) ----
                for alert in large_transfer_alerts:
                    # Large transfer is worthy of a ping (per your request)
                    send_webhook_alert({
                        "token": alert["token"],
                        "reason": alert["reason"],
                        "usd_value": alert["usd_value"],
                        "hash": alert["hash"],
                        "block": alert["block"],
                        "risk_score": alert.get("risk_score", 8),
                        "extra": {}
                    }, user_ids=USER_IDS, role_ids=ROLE_IDS)
                    summary_buffer.append({
                        "token": alert["token"],
                        "type": alert["type"],
                        "reason": alert["reason"],
                        "event_count": 1,
                        "total_usd": alert["usd_value"]
                    })

            except Exception as token_err:
                debug(f"Token {sym} error: {token_err}")

        # --- Summary dispatch ---
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
