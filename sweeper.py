from web3 import Web3

# ERC20 ABI for balance, symbol, decimals and transfer
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
]

class Sweeper:
    def __init__(self, rpc_url, native_symbol="ETH", chain_id=None, incoming_wallet=None, private_key=None, destination_wallet=None, gas_fee_wallet=None, gas_fee_private_key=None, token_addresses=None):
        self.rpc_url = rpc_url
        self.native_symbol = native_symbol
        self.chain_id = chain_id
        self.incoming_wallet = incoming_wallet
        self.private_key = private_key
        self.destination_wallet = destination_wallet
        self.gas_fee_wallet = gas_fee_wallet
        self.gas_fee_private_key = gas_fee_private_key
        self.token_addresses = token_addresses or []
        
        self.w3 = None
        if self.rpc_url:
            try:
                self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 15}))
                if not self.chain_id:
                    self.chain_id = self.w3.eth.chain_id
            except Exception as e:
                print(f"Error connecting to RPC: {e}")
                self.w3 = None

        self.last_tx_hash = None
        self.last_status = "Idle"

        self._checksum_addresses()

    def _checksum_addresses(self):
        if not self.w3:
            return
        try:
            if self.incoming_wallet and self.w3.is_address(self.incoming_wallet):
                self.incoming_wallet = self.w3.to_checksum_address(self.incoming_wallet)
            if self.destination_wallet and self.w3.is_address(self.destination_wallet):
                self.destination_wallet = self.w3.to_checksum_address(self.destination_wallet)
            if self.gas_fee_wallet and self.w3.is_address(self.gas_fee_wallet):
                self.gas_fee_wallet = self.w3.to_checksum_address(self.gas_fee_wallet)
            
            self.token_addresses = [self.w3.to_checksum_address(a) for a in self.token_addresses if self.w3.is_address(a)]
        except Exception as e:
            print(f"Address checksum error: {e}")

    def get_eth_balance(self, address=None):
        if not self.w3: return 0.0
        if not address:
            address = self.incoming_wallet
        if not address or "Example" in str(address):
            return 0.0
        try:
            balance_wei = self.w3.eth.get_balance(address)
            return float(self.w3.from_wei(balance_wei, 'ether'))
        except Exception as e:
            print(f"Error fetching {self.native_symbol} balance: {e}")
            return 0.0

    def get_token_data(self, token_address, owner_address=None):
        if not self.w3: return {"symbol": "???", "balance": 0.0, "address": token_address}
        if not owner_address:
            owner_address = self.incoming_wallet
        if not token_address or "Example" in str(token_address):
            return {"symbol": "???", "balance": 0.0, "address": token_address}
        
        try:
            contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
            balance = contract.functions.balanceOf(owner_address).call()
            
            try:
                symbol = contract.functions.symbol().call()
            except:
                symbol = token_address[:6] + "..."
                
            try:
                decimals = contract.functions.decimals().call()
            except:
                decimals = 18
                
            return {
                "symbol": symbol,
                "balance": float(balance / 10**decimals),
                "address": token_address,
                "raw_balance": balance
            }
        except Exception as e:
            return {"symbol": "---", "balance": 0.0, "address": token_address}

    def fund_gas(self, required_gas_wei):
        """Sends exact gas amount from GAS_FEE_WALLET to INCOMING_WALLET."""
        if not self.gas_fee_wallet or not self.gas_fee_private_key:
            return False, "No GAS_FEE_WALLET configured."
        
        try:
            funder_balance = self.w3.eth.get_balance(self.gas_fee_wallet)
            if funder_balance < required_gas_wei:
                return False, f"Gas Fee Wallet has insufficient balance."
            
            gas_price = self.w3.eth.gas_price
            nonce = self.w3.eth.get_transaction_count(self.gas_fee_wallet)
            
            tx = {
                'nonce': nonce,
                'to': self.incoming_wallet,
                'value': required_gas_wei,
                'gas': 21000,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            }
            
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.gas_fee_private_key)
            raw_tx = getattr(signed_tx, 'rawTransaction', getattr(signed_tx, 'raw_transaction', None))
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            return True, "Gas fee delivered"
            
        except Exception as e:
            return False, f"Gas fee delivery failed: {e}"

    def sweep_eth(self, gas_reserve_eth=0.00005):
        if not self.w3: return None, "No RPC"
        if not self.destination_wallet:
            return None, "No Destination Wallet"
        try:
            original_balance = self.w3.eth.get_balance(self.incoming_wallet)
            gas_price = self.w3.eth.gas_price
            gas_limit = 21000
            gas_cost = gas_price * gas_limit
            
            if original_balance <= 1000000:
                return None, f"Insufficient {self.native_symbol} for sweep"

            if self.gas_fee_wallet and self.gas_fee_private_key:
                funded, msg = self.fund_gas(gas_cost)
                if not funded:
                    return None, f"Gas Fee Wallet error: {msg}"

                send_amount_wei = original_balance
            else:
                reserve_wei = self.w3.to_wei(gas_reserve_eth, 'ether')
                send_amount_wei = original_balance - gas_cost - reserve_wei
            
            if send_amount_wei <= 0:
                return None, f"Insufficient {self.native_symbol} after gas limit rules"

            nonce = self.w3.eth.get_transaction_count(self.incoming_wallet)
            tx = {
                'nonce': nonce,
                'to': self.destination_wallet,
                'value': send_amount_wei,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            }
            
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            raw_tx = getattr(signed_tx, 'rawTransaction', getattr(signed_tx, 'raw_transaction', None))
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            return self.w3.to_hex(tx_hash), f"{self.native_symbol} Swept Successfully"
        except Exception as e:
            return None, f"Sweep Error: {str(e)}"

    def sweep_token(self, token_address, nonce_offset=0):
        if not self.w3: return None, "No RPC"
        if not self.destination_wallet:
            return None, "No Destination Wallet"
        try:
            contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
            balance = contract.functions.balanceOf(self.incoming_wallet).call()

            if balance <= 0:
                return None, "No balance"

            gas_price = self.w3.eth.gas_price
            gas_limit = 100000
            total_gas_cost = gas_price * gas_limit

            if self.gas_fee_wallet and self.gas_fee_private_key:
                funded, msg = self.fund_gas(total_gas_cost)
                if not funded:
                    return None, f"Gas fund error: {msg}"
            else:
                native_balance = self.w3.eth.get_balance(self.incoming_wallet)
                if native_balance < total_gas_cost:
                    return None, "No gas and no Gas Fee Wallet"

            nonce = self.w3.eth.get_transaction_count(self.incoming_wallet) + nonce_offset
            
            tx = contract.functions.transfer(self.destination_wallet, balance).build_transaction({
                'from': self.incoming_wallet,
                'nonce': nonce,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            raw_tx = getattr(signed_tx, 'rawTransaction', getattr(signed_tx, 'raw_transaction', None))
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            return self.w3.to_hex(tx_hash), "Tokens Swept"
        except Exception as e:
            return None, f"Token Sweep Error: {str(e)}"
