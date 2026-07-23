import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import jsonify, request

from keep_alive import app, keep_alive
from sweeper import Sweeper


load_dotenv()

API_URL = os.getenv("API_URL", "https://sweeper.pythonanywhere.com/api/sweeper")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "GDimTpje7kNTkrISGNPj4Nu_tzmuPNkjheDpV1ptgLM")
HEADERS = {"X-Worker-Key": WORKER_API_KEY}


def fetch_config():
    resp = requests.get(f"{API_URL}/worker-config/", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def log_activity(tx_hash=None, token_address=None, amount=None, status="Failed", message=""):
    try:
        requests.post(
            f"{API_URL}/log-activity/",
            headers=HEADERS,
            json={
                "tx_hash": tx_hash,
                "token_address": token_address,
                "amount": amount,
                "status": status,
                "message": message,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Failed to log activity to Django API: {e}")


def report_manual_sweep_complete(status, txs=None, error=None):
    """Clears the manual_sweep_requested flag Django set from the dashboard's
    Sweep Now button — Django can't call the worker directly (PythonAnywhere's
    free tier blocks outbound requests), so it just flags the request and we
    pick it up on our next regular poll instead. See core.views.sweep_now."""
    try:
        requests.post(
            f"{API_URL}/manual-sweep-complete/",
            headers=HEADERS,
            json={"status": status, "txs": txs or [], "error": error},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Failed to report manual sweep completion to Django API: {e}")


def build_sweeper(config):
    network = config.get("network")
    if not network:
        return None
    return Sweeper(
        rpc_url=network["rpc_url"],
        native_symbol=network["native_symbol"],
        chain_id=network["chain_id"],
        incoming_wallet=config.get("incoming_wallet"),
        private_key=config.get("incoming_private_key"),
        destination_wallet=config.get("destination_wallet"),
        gas_fee_wallet=config.get("gas_fee_wallet"),
        gas_fee_private_key=config.get("gas_fee_private_key"),
        token_addresses=config.get("token_addresses") or [],
    )


def _announce_incoming_native(sw, block_number):
    """Prints a heads-up line for any native-currency transfer into the
    incoming wallet in this block, before the sweep attempt runs."""
    if not sw.incoming_wallet:
        return
    try:
        block = sw.w3.eth.get_block(block_number, full_transactions=True)
    except Exception:
        return
    for tx in block.transactions:
        if tx.get("to") == sw.incoming_wallet and tx.get("value", 0) > 0:
            amount = sw.w3.from_wei(tx["value"], "ether")
            print(f"📥 Incoming transaction detected: {tx['hash'].hex()} ({amount} {sw.native_symbol})")


def run_one_sweep(config):
    """Sweeps tokens then native currency once. Returns the list of tx hashes sent."""
    sw = build_sweeper(config)
    results = []
    if not sw or not sw.w3:
        return results

    nonce_offset = 0
    for t_addr in config.get("token_addresses", []):
        token_balance = sw.get_token_data(t_addr).get("balance", 0)
        if token_balance > 0:
            print(f"📥 Incoming token detected: {token_balance} at {t_addr}")
            print(f"🔄 Sweeping token {t_addr}...")

        tx, msg = sw.sweep_token(t_addr, nonce_offset=nonce_offset)
        if tx:
            results.append(tx)
            nonce_offset += 1
            log_activity(tx_hash=tx, token_address=t_addr, status="Success", message="Swept tokens")
            print(f"✅ Swapped! Token sweep TX: {tx}")
        elif msg != "No balance":
            print(f"Token sweep skipped ({t_addr}): {msg}")

    if sw.w3.eth.get_balance(sw.incoming_wallet) > 1_000_000:
        print(f"🔄 Sweeping {sw.native_symbol}...")

    tx, msg = sw.sweep_eth(gas_reserve_eth=config.get("gas_reserve", 0.00005))
    if tx:
        results.append(tx)
        log_activity(tx_hash=tx, status="Success", message=f"Swept {sw.native_symbol}")
        print(f"✅ Swapped! Native sweep TX: {tx}")
    elif msg != f"Insufficient {sw.native_symbol} for sweep":
        print(f"Native sweep skipped: {msg}")

    return results


@app.route("/sweep-now", methods=["POST"])
def sweep_now_endpoint():
    if request.headers.get("X-Worker-Key") != WORKER_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    try:
        config = fetch_config()
    except requests.RequestException as e:
        return jsonify({"error": "Could not reach Django API", "details": str(e)}), 502

    results = run_one_sweep(config)
    if not results:
        return jsonify({"error": "No funds to sweep or failed checks"})
    return jsonify({"status": "success", "txs": results})


def auto_loop():
    last_scanned_block = None
    last_network_id = None

    while True:
        try:
            config = fetch_config()
        except requests.RequestException as e:
            print(f"Could not reach Django API: {e}")
            time.sleep(10)
            continue

        interval = config.get("sweep_interval", 30)

        if config.get("manual_sweep_requested"):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Manual sweep requested, running now...")
            try:
                results = run_one_sweep(config)
                if results:
                    report_manual_sweep_complete("done", txs=results)
                else:
                    report_manual_sweep_complete("error", error="No funds to sweep or failed checks")
            except Exception as e:
                print(f"Manual sweep error: {e}")
                report_manual_sweep_complete("error", error=str(e))

        elif config.get("mode") == "auto" and config.get("network"):
            sw = build_sweeper(config)
            try:
                if sw and sw.w3 and sw.w3.is_connected():
                    network_id = config["network"]["id"]
                    if network_id != last_network_id:
                        last_scanned_block = None
                        last_network_id = network_id

                    current_block = sw.w3.eth.block_number
                    if last_scanned_block is None:
                        last_scanned_block = current_block - 1

                    if current_block > last_scanned_block:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Block {current_block} scanned")
                        _announce_incoming_native(sw, current_block)
                        run_one_sweep(config)
                        last_scanned_block = current_block
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for new blocks...")
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Web3 provider failed to connect: {config['network'].get('rpc_url')}")
            except Exception as e:
                print(f"Background scan error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    print("Launching sweeper worker...")
    keep_alive()
    auto_loop()
