import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import jsonify, request

from keep_alive import app, keep_alive
from sweeper import Sweeper

load_dotenv()

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000/api/sweeper")
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


def run_one_sweep(config):
    """Sweeps tokens then native currency once. Returns the list of tx hashes sent."""
    sw = build_sweeper(config)
    results = []
    if not sw or not sw.w3:
        return results

    nonce_offset = 0
    for t_addr in config.get("token_addresses", []):
        tx, msg = sw.sweep_token(t_addr, nonce_offset=nonce_offset)
        if tx:
            results.append(tx)
            nonce_offset += 1
            log_activity(tx_hash=tx, token_address=t_addr, status="Success", message="Swept tokens")
            print(f"Token sweep TX: {tx}")

    tx, msg = sw.sweep_eth(gas_reserve_eth=config.get("gas_reserve", 0.00005))
    if tx:
        results.append(tx)
        log_activity(tx_hash=tx, status="Success", message=f"Swept {sw.native_symbol}")
        print(f"Native sweep TX: {tx}")

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

        if config.get("mode") == "auto" and config.get("network"):
            sw = build_sweeper(config)
            if sw and sw.w3 and sw.w3.is_connected():
                try:
                    network_id = config["network"]["id"]
                    if network_id != last_network_id:
                        last_scanned_block = None
                        last_network_id = network_id

                    current_block = sw.w3.eth.block_number
                    if last_scanned_block is None:
                        last_scanned_block = current_block - 1

                    if current_block > last_scanned_block:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Block {current_block} scanned")
                        run_one_sweep(config)
                        last_scanned_block = current_block
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for new blocks...")
                except Exception as e:
                    print(f"Background scan error: {e}")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Web3 provider failed to connect: {config['network'].get('rpc_url')}")

        time.sleep(interval)


if __name__ == "__main__":
    print("Launching sweeper worker...")
    keep_alive()
    auto_loop()
